[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ProjectRoot,
    [Parameter(Mandatory=$true)][string]$StatePath,
    [string]$CoreUrl = "http://127.0.0.1:8710"
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
Set-Location -LiteralPath $ProjectRoot
$env:PYTHONUTF8 = "1"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
# No import-time migrations, autostart, Hermes or chat process. Journal owns errors.
& $Python -m investor_core.background_worker --core-url $CoreUrl --state $StatePath
exit $LASTEXITCODE
