[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ProjectRoot,
    [ValidateSet("Preview", "Install", "Switch", "Pause", "Rollback")][string]$Action = "Preview",
    [string]$CoreUrl = "http://127.0.0.1:8710",
    [string]$TaskName = "ValueDCABackgroundScheduler",
    [switch]$Apply
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd("\")
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Runner = Join-Path $ProjectRoot "runtime\windows\run-background-scheduler.ps1"
$Launcher = Join-Path $ProjectRoot "runtime\windows\run-powershell-hidden.vbs"
$StatePath = Join-Path $ProjectRoot "data\background-scheduler.sqlite3"
$Target = "WINDOWS"
if ($Action -eq "Pause") { $Target = "PAUSED" }
if ($Action -eq "Rollback") { $Target = "HERMES" }
Write-Output "Action=$Action; Task=$TaskName; Target=$Target; Apply=$Apply"
Write-Output "Worker: one-minute wake-up, StartWhenAvailable, no forced wake from sleep."
Write-Output "Approved Core policies determine business times and retries. Install stays DISABLED."
Write-Output "Hermes notification delivery and all other jobs are retained."
$env:PYTHONUTF8 = "1"
if (-not $Apply -or $Action -eq "Preview") {
    # GET-only; an old Core returns 404 until the separately approved upgrade.
    & $Python -m investor_core.scheduler_admin --core-url $CoreUrl --target $Target
    exit $LASTEXITCODE
}
if ($Action -eq "Install") {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        throw "Task already exists. Inspect it; installation never overwrites an existing task."
    }
    foreach ($File in @($Python, $Runner, $Launcher)) {
        if (-not (Test-Path -LiteralPath $File)) { throw "Missing file: $File" }
        if ($File.Contains('"')) { throw "Unsupported quote in path." }
    }
    $TaskArgs = '"{0}" "{1}" -ProjectRoot "{2}" -StatePath "{3}" -CoreUrl "{4}"' -f $Launcher, $Runner, $ProjectRoot, $StatePath, $CoreUrl
    $TaskAction = New-ScheduledTaskAction -Execute (Join-Path $env:SystemRoot "System32\wscript.exe") -Argument $TaskArgs -WorkingDirectory $ProjectRoot
    $Triggers = @(
        (New-ScheduledTaskTrigger -AtStartup),
        (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1))
    )
    $Settings = New-ScheduledTaskSettingsSet -Disable -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $User = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $TaskAction -Trigger $Triggers -Settings $Settings -Principal $Principal -Description "Investor Core governed daily scheduler; installed disabled" | Out-Null
    Write-Output "Installed disabled. No source switch or business execution occurred."
    exit 0
}
$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
$ExpectedExecutable = Join-Path $env:SystemRoot "System32\wscript.exe"
if (@($Task.Actions).Count -ne 1 -or $Task.Actions[0].Execute -ne $ExpectedExecutable -or -not $Task.Actions[0].Arguments.Contains($Runner)) {
    throw "Existing task does not match this runtime; refusing to control it."
}
# Preview + short-lived governed draft + explicit confirmation; token stays in memory.
& $Python -m investor_core.scheduler_admin --core-url $CoreUrl --target $Target --apply
if ($LASTEXITCODE -ne 0) { throw "Source switch was not committed. Task remains unchanged." }
if ($Action -eq "Switch") {
    Enable-ScheduledTask -TaskName $TaskName | Out-Null
    Start-ScheduledTask -TaskName $TaskName
} else {
    Disable-ScheduledTask -TaskName $TaskName | Out-Null
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}
Write-Output "Inspect Core scheduler status, local journal and actual next occurrence. Notification receipt is separate."
