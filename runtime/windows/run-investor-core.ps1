[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd("\")
$CoreExecutable = Join-Path $ProjectRoot ".venv\Scripts\investor-core.exe"
$LogDirectory = Join-Path $ProjectRoot "logs"
$SupervisorLogPath = Join-Path $LogDirectory "investor-core-supervisor.log"

New-Item -ItemType Directory -Force $LogDirectory | Out-Null
Set-Location -LiteralPath $ProjectRoot
$env:INVESTOR_ENVIRONMENT = "production"
$env:PYTHONUNBUFFERED = "1"

$ConsecutiveFastFailures = 0
while ($true) {
    if (-not (Test-Path $CoreExecutable)) {
        Add-Content -LiteralPath $SupervisorLogPath -Value (
            "$(Get-Date -Format o) Core executable is missing: $CoreExecutable"
        )
        Start-Sleep -Seconds 5
        continue
    }

    Add-Content -LiteralPath $SupervisorLogPath -Value "$(Get-Date -Format o) Starting Investor Core"
    $Uptime = [Diagnostics.Stopwatch]::StartNew()
    try {
        & $CoreExecutable
        $ExitCode = $LASTEXITCODE
    }
    catch {
        $ExitCode = 1
        Add-Content -LiteralPath $SupervisorLogPath -Value (
            "$(Get-Date -Format o) $($_.Exception.Message)"
        )
    }
    $Uptime.Stop()
    if ($Uptime.Elapsed.TotalSeconds -lt 30) {
        $ConsecutiveFastFailures++
    }
    else {
        $ConsecutiveFastFailures = 0
    }
    Add-Content -LiteralPath $SupervisorLogPath -Value (
        "$(Get-Date -Format o) Investor Core exited with code $ExitCode after " +
        "$([Math]::Round($Uptime.Elapsed.TotalSeconds, 1)) seconds"
    )
    if ($ConsecutiveFastFailures -ge 3) {
        Add-Content -LiteralPath $SupervisorLogPath -Value (
            "$(Get-Date -Format o) Three fast failures; returning control to Task Scheduler"
        )
        exit 1
    }
    Start-Sleep -Seconds 5
}
