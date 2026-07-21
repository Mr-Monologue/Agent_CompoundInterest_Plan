from __future__ import annotations

import json
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_windows_installer_uses_hidden_managed_core() -> None:
    installer = (PROJECT_ROOT / "install-windows.ps1").read_text(encoding="utf-8-sig")
    hermes_config = (PROJECT_ROOT / "src/investor_core/hermes_config.py").read_text(
        encoding="utf-8-sig"
    )

    assert "Register-ScheduledTask" in installer
    assert "wscript.exe" in installer
    assert "run-powershell-hidden.vbs" in installer
    assert "-Execute $WScriptExecutable" in installer
    assert "investor_core.hermes_config" in installer
    assert "INVESTOR_CORE_AUTOSTART" in hermes_config
    assert "INVESTOR_CORE_WINDOWS_TASK_NAME" in hermes_config
    assert "Start-ScheduledTask" in installer
    assert "ValueDCAAgentUpdate" in installer
    assert "update-value-dca.ps1" in installer
    assert "-RunOnlyIfNetworkAvailable" in installer
    assert "investor db backup" in installer
    assert "hermes mcp remove" not in installer
    assert "-NoExit" not in installer


def test_windows_hidden_launcher_uses_gui_host_without_a_console() -> None:
    launcher = (PROJECT_ROOT / "runtime/windows/run-powershell-hidden.vbs").read_text(
        encoding="utf-8-sig"
    )

    assert 'CreateObject("WScript.Shell")' in launcher
    assert "shell.Run(command, 0, True)" in launcher
    assert "-NonInteractive" in launcher
    assert "powershell.exe" in launcher


def test_windows_core_runner_is_supervised_and_noninteractive() -> None:
    runner = (PROJECT_ROOT / "runtime/windows/run-investor-core.ps1").read_text(
        encoding="utf-8-sig"
    )

    assert "while ($true)" in runner
    assert ".venv\\Scripts\\investor-core.exe" in runner
    assert "Three fast failures" in runner
    assert "Core executable remained missing" in runner
    assert "*>>" not in runner
    assert "Read-Host" not in runner


def test_windows_runtime_locks_portable_timezone_data() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())

    assert "tzdata>=2026.3,<2027" in project["project"]["dependencies"]


def test_release_manifest_matches_project_version() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    manifest = json.loads((PROJECT_ROOT / "release-manifest.json").read_text())

    assert manifest["schema_version"] == 1
    assert manifest["channel"] == "stable"
    assert manifest["version"] == project["project"]["version"]
    assert manifest["database_revision"] == "0003_opening_position"


def test_release_workflow_publishes_only_from_the_long_lived_release_branch() -> None:
    workflow = (PROJECT_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "branches: [release]" in workflow
    assert "paths: [release-manifest.json]" in workflow
    assert "RELEASE_TAG: ${{ steps.version.outputs.tag }}" in workflow
    assert "release-manifest.json does not match pyproject.toml" in workflow
    assert '--target "$GITHUB_SHA"' in workflow
    assert "branches: [main]" not in workflow


def test_windows_updater_is_release_only_backup_first_and_rollback_capable() -> None:
    updater = (PROJECT_ROOT / "runtime/windows/update-value-dca.ps1").read_text(
        encoding="utf-8-sig"
    )

    assert "releases/latest" in updater
    assert "release-manifest.json" in updater
    assert "refs/heads/main" not in updater
    assert "git pull" not in updater
    assert "& $InvestorCli db backup --output $DatabaseBackup" in updater
    assert updater.index("db backup") < updater.index("Installing v$AvailableVersion")
    assert "Restoring v$CurrentVersion" in updater
    assert "code rollback" in updater
    assert "rollback diagnostics" in updater
    assert "--reinstall-package value-dca-agent" in updater
    assert "installer: $([string]$InstallerLine)" in updater
    assert "Export-ScheduledTask" in updater
    assert "Register-ScheduledTask" in updater
    assert "update-state.json" in updater


def test_windows_bootstrap_uses_latest_stable_release() -> None:
    bootstrap = (PROJECT_ROOT / "bootstrap-windows.ps1").read_text(encoding="utf-8-sig")

    assert "releases/latest" in bootstrap
    assert "release-manifest.json" in bootstrap
    assert "raw.githubusercontent.com" not in bootstrap
    assert "-NonInteractive" in bootstrap
    assert "-SkipMcpTest" in bootstrap


def test_windows_ci_parses_powershell_scripts() -> None:
    workflow = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "Parse Windows PowerShell scripts" in workflow
    assert "System.Management.Automation.Language.Parser" in workflow
    assert "runner.os == 'Windows'" in workflow
