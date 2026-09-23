function Get-SnapshotHash([string]$Path) {
    $Stream = [IO.File]::OpenRead($Path)
    $Hasher = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($Hasher.ComputeHash($Stream))).Replace("-", "") }
    finally { $Stream.Dispose(); $Hasher.Dispose() }
}

# Testable safety helpers. All production paths are anchored to InstallDir.
function Get-UpdatePaths([string]$InstallDir) {
    $Root = [IO.Path]::GetFullPath($InstallDir).TrimEnd("\")
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) { throw "Install directory missing: $Root" }
    $Python = Join-Path $Root ".venv\Scripts\python.exe"
    $Cli = Join-Path $Root ".venv\Scripts\investor.exe"
    foreach ($Required in @($Python, $Cli, (Join-Path $Root ".env"))) {
        if (-not (Test-Path -LiteralPath $Required -PathType Leaf)) { throw "Preflight required file missing: $Required" }
    }
    Push-Location $Root
    try {
        $Resolved = & $Python -c "from investor_core.config import Settings; print(Settings().db_path.resolve())"
        if ($LASTEXITCODE -ne 0) { throw "Could not resolve configured database" }
        $Database = [IO.Path]::GetFullPath([string]$Resolved)
    } finally { Pop-Location }
    if (-not $Database.StartsWith($Root + "\", [StringComparison]::OrdinalIgnoreCase)) {
        throw "Configured database is outside installation; explicit migration review required: $Database"
    }
    # Installer and rollback must agree with the existing deployment layout.
    if ($Database -ne (Join-Path $Root "data\investor.db")) {
        throw "Unsupported custom database layout; refusing to stop services: $Database"
    }
    if (-not (Test-Path -LiteralPath $Database -PathType Leaf)) { throw "Production database missing: $Database" }
    $Backups = Join-Path $Root "backups\updates"
    New-Item -ItemType Directory -Force $Backups | Out-Null
    $Probe = Join-Path $Backups (".probe-" + [guid]::NewGuid())
    try { [IO.File]::WriteAllText($Probe, "preflight"); [IO.File]::ReadAllText($Probe) | Out-Null }
    finally { if (Test-Path -LiteralPath $Probe) { Remove-Item -LiteralPath $Probe -Force } }
    return @{ Root=$Root; Python=$Python; Cli=$Cli; Database=$Database; Backups=$Backups }
}

function Assert-BackupDatabase([string]$Python, [string]$Database) {
    & $Python -c "import sqlite3,sys,pathlib; p=pathlib.Path(sys.argv[1]); c=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True); assert c.execute('PRAGMA quick_check').fetchone()==('ok',); assert c.execute('SELECT version_num FROM alembic_version').fetchone(); c.close()" $Database
    if ($LASTEXITCODE -ne 0) { throw "Backup database verification failed" }
}

function Complete-RecoverySnapshot([string]$Root, [string]$Python) {
    foreach ($Required in @("investor.db", "config.env", "code\release-manifest.json", "tasks.json")) {
        if (-not (Test-Path -LiteralPath (Join-Path $Root $Required) -PathType Leaf)) {
            throw "Incomplete recovery snapshot: $Required"
        }
    }
    Assert-BackupDatabase $Python (Join-Path $Root "investor.db")
    $Files = @(Get-ChildItem -LiteralPath $Root -File -Recurse | Where-Object { $_.Name -ne "recovery.json" } | ForEach-Object {
        @{ path=$_.FullName.Substring($Root.Length + 1); sha256=(Get-SnapshotHash $_.FullName) }
    })
    @{ status="VERIFIED"; verified_at=(Get-Date -Format o); files=$Files } |
        ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $Root "recovery.json") -Encoding UTF8
    try { Assert-RecoverySnapshot $Root $Python }
    catch {
        Remove-Item -LiteralPath (Join-Path $Root "recovery.json") -Force -ErrorAction SilentlyContinue
        throw
    }
}

function Assert-RecoverySnapshot([string]$Root, [string]$Python) {
    $Manifest = Join-Path $Root "recovery.json"
    if (-not (Test-Path -LiteralPath $Manifest)) { throw "Snapshot has no verified completion marker" }
    $Record = Get-Content -LiteralPath $Manifest -Raw | ConvertFrom-Json
    if ($Record.status -ne "VERIFIED" -or @($Record.files).Count -lt 4) { throw "Snapshot incomplete" }
    foreach ($File in $Record.files) {
        $Path = [IO.Path]::GetFullPath((Join-Path $Root $File.path))
        if (-not $Path.StartsWith($Root + "\", [StringComparison]::OrdinalIgnoreCase)) { throw "Unsafe snapshot member" }
        if (-not (Test-Path -LiteralPath $Path) -or (Get-SnapshotHash $Path) -ne $File.sha256) {
            throw "Snapshot integrity mismatch: $($File.path)"
        }
    }
    Assert-BackupDatabase $Python (Join-Path $Root "investor.db")
}

function Get-CoreRecoveryStatus([string]$ExpectedVersion) {
    for ($Attempt=0; $Attempt -lt 15; $Attempt++) {
        try {
            $Health = Invoke-RestMethod -Uri "http://127.0.0.1:8710/health" -TimeoutSec 2
            $Ready = Invoke-RestMethod -Uri "http://127.0.0.1:8710/ready" -TimeoutSec 2
            if ($Health.version -eq $ExpectedVersion -and $Ready.status -ne "FAIL") { return "HEALTHY" }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    return "RECOVERY_UNCONFIRMED"
}
