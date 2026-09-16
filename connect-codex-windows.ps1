[CmdletBinding()]
param(
    [string]$InstallDir = "C:\investor\value-dca-agent",
    [string]$CoreUrl = "http://127.0.0.1:8710",
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Connect to the installed runtime, never to a development checkout implicitly.
$ProjectRoot = (Resolve-Path -LiteralPath $InstallDir).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$McpExecutable = Join-Path $ProjectRoot ".venv\Scripts\investor-mcp.exe"
foreach ($Executable in @($Python, $McpExecutable)) {
    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
        throw "Installed runtime is missing: $Executable. Complete installation first."
    }
}
$CoreAddress = [Uri]$CoreUrl
if (-not $CoreAddress.IsAbsoluteUri -or -not $CoreAddress.IsLoopback -or
    $CoreAddress.Scheme -ne "http" -or $CoreAddress.UserInfo -or
    $CoreAddress.Query -or $CoreAddress.Fragment -or $CoreAddress.AbsolutePath -ne "/") {
    throw "CoreUrl must be a local HTTP origin, for example http://127.0.0.1:8710."
}
$CoreUrl = $CoreAddress.GetLeftPart([UriPartial]::Authority)

Write-Host "Checking the installed MCP connection (no business writes)..."
Push-Location $ProjectRoot
try {
    & $Python -m investor_mcp.check --project-root $ProjectRoot --core-url $CoreUrl
    if ($LASTEXITCODE -ne 0) {
        throw "MCP connection check failed. Codex configuration was not changed."
    }
}
finally {
    Pop-Location
}
if ($CheckOnly) {
    return
}

$CodexCommand = Get-Command codex -ErrorAction Stop
$CodexDirectory = if ($env:CODEX_HOME) {
    $env:CODEX_HOME
} else {
    Join-Path $env:USERPROFILE ".codex"
}
$ConfigPath = Join-Path $CodexDirectory "config.toml"
if (Test-Path -LiteralPath $ConfigPath) {
    $BackupPath = "$ConfigPath.investor-$([guid]::NewGuid().ToString('N')).bak"
    Copy-Item -LiteralPath $ConfigPath -Destination $BackupPath
    Write-Host "Existing Codex configuration backed up at: $BackupPath"
}

# Let Codex own TOML editing. No custom parser and no shell command string.
# Autostart stays disabled: reading financial state must not reinstall runtime files.
$CodexArguments = @(
    "mcp", "add", "investor_core",
    "--env", "INVESTOR_CORE_BASE_URL=$CoreUrl",
    "--env", "INVESTOR_PROJECT_ROOT=$ProjectRoot",
    "--env", "INVESTOR_MCP_ACTOR_REF=codex",
    "--env", "INVESTOR_CORE_AUTOSTART=false",
    "--", $McpExecutable
)
& $CodexCommand @CodexArguments
if ($LASTEXITCODE -ne 0) {
    throw "Codex MCP registration failed. Inspect config.toml and its backup before retrying."
}
Write-Host "Registered investor_core. Restart the local Codex session and check /mcp."
Write-Host "No ledger, strategy, Hermes configuration or scheduled tasks were changed."
