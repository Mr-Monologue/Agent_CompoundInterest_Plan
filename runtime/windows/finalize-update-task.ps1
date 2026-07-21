[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallDir,
    [string]$Repository = "Mr-Monologue/Agent_CompoundInterest_Plan",
    [string]$CoreTaskName = "ValueDCAInvestorCore",
    [string]$UpdateTaskName = "ValueDCAAgentUpdate",
    [string]$HermesProfile = "investor"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$InstallDir = [IO.Path]::GetFullPath($InstallDir).TrimEnd("\")
$LogDirectory = Join-Path $InstallDir "logs"
$LogPath = Join-Path $LogDirectory "updater.log"
New-Item -ItemType Directory -Force $LogDirectory | Out-Null

function Write-FinalizerLog([string]$Message) {
    Add-Content -LiteralPath $LogPath -Value "$(Get-Date -Format o) finalizer: $Message"
}

try {
    for ($Attempt = 0; $Attempt -lt 120; $Attempt++) {
        $CurrentTask = Get-ScheduledTask -TaskName $UpdateTaskName `
            -ErrorAction SilentlyContinue
        if ($null -eq $CurrentTask -or $CurrentTask.State -ne "Running") {
            break
        }
        Start-Sleep -Seconds 1
    }
    if ($null -ne $CurrentTask -and $CurrentTask.State -eq "Running") {
        throw "Updater task remained active for 120 seconds."
    }

    $HiddenLauncher = Join-Path $InstallDir "runtime\windows\run-powershell-hidden.vbs"
    $UpdaterScript = Join-Path $InstallDir "runtime\windows\update-value-dca.ps1"
    if (-not (Test-Path $HiddenLauncher) -or -not (Test-Path $UpdaterScript)) {
        throw "Hidden launcher or updater script is missing."
    }
    $WScriptExecutable = Join-Path $env:SystemRoot "System32\wscript.exe"
    $UpdateArguments = (
        "`"$HiddenLauncher`" `"$UpdaterScript`" " +
        "-InstallDir `"$InstallDir`" " +
        "-Repository `"$Repository`" -CoreTaskName `"$CoreTaskName`" " +
        "-UpdateTaskName `"$UpdateTaskName`" " +
        "-HermesProfile `"$HermesProfile`""
    )
    $UpdateAction = New-ScheduledTaskAction `
        -Execute $WScriptExecutable `
        -Argument $UpdateArguments `
        -WorkingDirectory $InstallDir
    $UpdateTrigger = New-ScheduledTaskTrigger -Daily -At "04:00"
    $CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $TaskPrincipal = New-ScheduledTaskPrincipal `
        -UserId $CurrentUser `
        -LogonType Interactive `
        -RunLevel Limited
    $UpdateSettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RunOnlyIfNetworkAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
        -MultipleInstances IgnoreNew
    $UpdateDefinition = New-ScheduledTask `
        -Action $UpdateAction `
        -Trigger $UpdateTrigger `
        -Principal $TaskPrincipal `
        -Settings $UpdateSettings `
        -Description (
            "Checks stable GitHub releases for Value DCA, backs up the database, " +
            "and rolls back failed updates."
        )
    Register-ScheduledTask -TaskName $UpdateTaskName `
        -InputObject $UpdateDefinition -Force | Out-Null
    Write-FinalizerLog "Installed console-free updater task definition."
}
catch {
    Write-FinalizerLog "FAILED: $($_.Exception.Message)"
    exit 1
}
