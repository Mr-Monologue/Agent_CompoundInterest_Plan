from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PS = shutil.which("powershell") or shutil.which("pwsh")
pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or not PS, reason="Windows executable updater test"
)


def call(script, cwd, env=None):
    return subprocess.run(
        [PS, "-NoProfile", "-NonInteractive", "-Command", script],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )


def installed(tmp_path):
    root = tmp_path / "installation with spaces"
    (root / ".venv/Scripts").mkdir(parents=True)
    (root / "data").mkdir()
    shutil.copy2(sys.executable, root / ".venv/Scripts/python.exe")
    shutil.copy2(Path(sys.prefix) / "pyvenv.cfg", root / ".venv/pyvenv.cfg")
    (root / ".venv/Scripts/investor.exe").write_bytes(b"preflight only")
    (root / ".env").write_text("INVESTOR_DB_PATH=data/investor.db\n")
    with sqlite3.connect(root / "data/investor.db") as db:
        db.execute("CREATE TABLE alembic_version(version_num TEXT)")
        db.execute("INSERT INTO alembic_version VALUES ('isolated')")
    db.close()
    env = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join(
            [str(ROOT / "src"), str(Path(sys.prefix) / "Lib/site-packages")]
        ),
    )
    env.pop("INVESTOR_DB_PATH", None)
    return root, env


def test_preflight_from_unrelated_directory_and_missing_database(tmp_path):
    root, env = installed(tmp_path)
    helper = ROOT / "runtime/windows/update-safety.ps1"
    script = f". '{helper}'; Get-UpdatePaths '{root}' | ConvertTo-Json -Compress"
    result = call(script, tmp_path, env)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["Database"] == str(root / "data/investor.db")
    (root / "data/investor.db").unlink()
    assert call(script, tmp_path, env).returncode != 0


def test_only_complete_untampered_backups_are_recoverable(tmp_path):
    root, env = installed(tmp_path)
    helper = ROOT / "runtime/windows/update-safety.ps1"
    backup = root / "backups/isolated"
    (backup / "code").mkdir(parents=True)
    prefix = f". '{helper}'; "
    verify = f"Assert-RecoverySnapshot '{backup}' '{sys.executable}'"
    assert call(prefix + verify, tmp_path, env).returncode != 0
    for name in ("config.env", "tasks.json", "code/release-manifest.json"):
        (backup / name).write_text("{}")
    shutil.copy2(root / "data/investor.db", backup / "investor.db")
    result = call(
        prefix + f"Complete-RecoverySnapshot '{backup}' '{sys.executable}'", tmp_path, env
    )
    assert result.returncode == 0, result.stderr
    (backup / "config.env").write_text("tampered")
    assert call(prefix + verify, tmp_path, env).returncode != 0


def test_full_updater_backup_failure_restarts_without_install_or_restore(tmp_path):
    # Run the real updater script with isolated OS/network boundaries; fail actual db CLI.
    root, env = installed(tmp_path)
    (root / "src/investor_core").mkdir(parents=True)
    (root / "src/investor_core/version.py").write_text('__version__ = "0.33.1"')
    (root / "release-manifest.json").write_text("{}")
    script = ROOT / "runtime/windows/update-value-dca.ps1"
    log = tmp_path / "events.txt"
    wrapper = tmp_path / "run.ps1"
    wrapper.write_text(
        f"""
$ErrorActionPreference='Stop'
$global:Events='{log}'
function global:Invoke-RestMethod {{
 param($Uri)
 if ($Uri -like '*releases/latest') {{
  return @{{draft=$false;prerelease=$false;tag_name='v0.34.0';
            zipball_url='https://isolated.invalid/archive'}}
 }}
 if ($Uri -like '*/health') {{ return @{{version='0.33.1'}} }}
 return @{{status='PASS'}}
}}
function global:Invoke-WebRequest {{
 param($OutFile) Set-Content -LiteralPath $OutFile -Value 'mock'
}}
function global:Expand-Archive {{ param($DestinationPath)
 New-Item -ItemType Directory -Force $DestinationPath | Out-Null
 @{{schema_version=1;channel='stable';version='0.34.0';auto_update=$true;
    requires_manual_approval=$true;minimum_current_version='0.5.3'}} |
  ConvertTo-Json | Set-Content (Join-Path $DestinationPath 'release-manifest.json')
 Set-Content (Join-Path $DestinationPath 'install-windows.ps1') 'throw "INSTALL MUST NOT RUN"'
}}
function global:Get-ScheduledTask {{
 return [pscustomobject]@{{TaskName='ValueDCAInvestorCore';State='Running'}}
}}
function global:Export-ScheduledTask {{ return '<Task />' }}
function global:Stop-ScheduledTask {{ Add-Content $global:Events 'STOP' }}
function global:Start-ScheduledTask {{ Add-Content $global:Events 'RESTART' }}
function global:Get-CimInstance {{ return @() }}
function global:Get-Process {{ return @() }}
function global:icacls {{ $global:LASTEXITCODE=0 }}
function global:robocopy {{
 param($Source,$Destination)
 New-Item -ItemType Directory -Force $Destination | Out-Null
 Copy-Item -LiteralPath (Join-Path $Source 'release-manifest.json') -Destination $Destination
 $global:LASTEXITCODE=0
}}
try {{ & '{script}' -InstallDir '{root}' -AllowManualUpdate -SkipHermes; exit 99 }}
catch {{ Write-Output $_.Exception.Message; exit 0 }}
""",
        encoding="utf-8-sig",
    )
    before = (root / "data/investor.db").read_bytes()
    result = subprocess.run(
        [PS, "-NoProfile", "-File", str(wrapper)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr
    assert log.exists(), result.stdout + result.stderr
    assert log.read_text().splitlines() == ["STOP", "RESTART"]
    state = json.loads((root / "data/update-state.json").read_text(encoding="utf-8-sig"))
    assert not state["installation_started"] and not state["snapshot_verified"]
    assert state["recovery"] == "HEALTHY"
    assert (root / "data/investor.db").read_bytes() == before
    assert not list((root / "backups/updates").glob("*/recovery.json"))
