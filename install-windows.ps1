[CmdletBinding()]
param(
    [string]$InstallDir = "C:\investor\value-dca-agent",
    [string]$HermesProfile = "investor",
    [string]$CoreTaskName = "ValueDCAInvestorCore",
    [string]$UpdateTaskName = "ValueDCAAgentUpdate",
    [string]$Repository = "Mr-Monologue/Agent_CompoundInterest_Plan",
    [switch]$SkipHermes,
    [switch]$SkipStart,
    [switch]$SkipMcpTest,
    [switch]$NonInteractive,
    [switch]$DisableAutoUpdate
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$SourceRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$CoreUrl = "http://127.0.0.1:8710"

function Write-Step([string]$Message) {
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Assert-LastExit([string]$Operation) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Operation failed with exit code $LASTEXITCODE"
    }
}

function Get-InvestorRuntimeProcesses([string]$Root) {
    $NormalizedRoot = [IO.Path]::GetFullPath($Root).TrimEnd("\")
    return @(
        Get-Process -Name "investor-mcp", "investor-core" -ErrorAction SilentlyContinue |
            Where-Object {
                $null -ne $_.Path -and
                $_.Path.StartsWith($NormalizedRoot, [StringComparison]::OrdinalIgnoreCase)
            }
    )
}

Write-Host "Value DCA Agent Windows installer / upgrader" -ForegroundColor Green
Write-Host "Package source: $SourceRoot"
Write-Host "Install directory: $InstallDir"

$NormalizedSource = [IO.Path]::GetFullPath($SourceRoot).TrimEnd("\")
$NormalizedTarget = [IO.Path]::GetFullPath($InstallDir).TrimEnd("\")

if (-not (Get-Command Get-ScheduledTask -ErrorAction SilentlyContinue)) {
    throw "Windows Task Scheduler cmdlets are unavailable on this host."
}
$ManagedTask = Get-ScheduledTask -TaskName $CoreTaskName -ErrorAction SilentlyContinue
if ($null -ne $ManagedTask -and $ManagedTask.State -eq "Running") {
    Write-Step "Stopping the managed Investor Core for a safe upgrade"
    Stop-ScheduledTask -TaskName $CoreTaskName
    for ($Attempt = 0; $Attempt -lt 20; $Attempt++) {
        if (@(Get-InvestorRuntimeProcesses $NormalizedTarget).Count -eq 0) {
            break
        }
        Start-Sleep -Milliseconds 500
    }
}

$RuntimeProcesses = @(Get-InvestorRuntimeProcesses $NormalizedTarget)
if ($RuntimeProcesses.Count -gt 0) {
    $ProcessSummary = ($RuntimeProcesses | ForEach-Object { "$($_.ProcessName) (PID $($_.Id))" }) `
        -join ", "
    if ($NonInteractive) {
        Write-Warning "Stopping managed runtime processes for unattended upgrade: $ProcessSummary"
        $RuntimeProcesses | Stop-Process -Force
    }
    else {
        Write-Warning (
            "Hermes or a legacy Investor Core is using files that must be upgraded: " +
            "$ProcessSummary. Fully exit Hermes Desktop from its system-tray icon. " +
            "Close the old Core window if present."
        )
        Read-Host "Press Enter after Hermes Desktop and the old Core have stopped" | Out-Null
    }

    for ($Attempt = 0; $Attempt -lt 20; $Attempt++) {
        $RuntimeProcesses = @(Get-InvestorRuntimeProcesses $NormalizedTarget)
        if ($RuntimeProcesses.Count -eq 0) {
            break
        }
        Start-Sleep -Milliseconds 500
    }
    if ($RuntimeProcesses.Count -gt 0) {
        $Remaining = ($RuntimeProcesses | ForEach-Object { "$($_.ProcessName) PID $($_.Id)" }) `
            -join ", "
        throw (
            "Investor runtime files are still in use by $Remaining. " +
            "Close Hermes Desktop from the system tray, then run the installer again."
        )
    }
}

$ExistingHealth = $null
try {
    $ExistingHealth = Invoke-RestMethod "$CoreUrl/health" -TimeoutSec 2
}
catch {
    $ExistingHealth = $null
}
if ($null -ne $ExistingHealth) {
    throw (
        "Port 8710 is still occupied after the managed task stopped. " +
        "Stop that process, then run the installer again."
    )
}

Write-Step "Installing or upgrading project files"
if ($NormalizedSource -ne $NormalizedTarget) {
    New-Item -ItemType Directory -Force $NormalizedTarget | Out-Null
    & robocopy $NormalizedSource $NormalizedTarget /E /R:2 /W:1 `
        /XD .git .venv .mypy_cache .pytest_cache .ruff_cache __pycache__ data logs backups `
        /XF .env *.pyc *.db *.db-wal *.db-shm
    if ($LASTEXITCODE -ge 8) {
        throw "Project file installation failed with Robocopy exit code $LASTEXITCODE"
    }
}
$ProjectRoot = $NormalizedTarget
foreach ($DataDirectory in @("data", "logs", "backups")) {
    New-Item -ItemType Directory -Force (Join-Path $ProjectRoot $DataDirectory) | Out-Null
}

Write-Step "Checking uv"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "uv is missing and WinGet is unavailable. Install uv, then run this script again."
    }
    & winget install --id=astral-sh.uv -e --accept-package-agreements --accept-source-agreements
    Assert-LastExit "uv installation"

    $UvCandidates = @(
        "$env:USERPROFILE\.local\bin\uv.exe",
        "$env:LOCALAPPDATA\Microsoft\WinGet\Links\uv.exe"
    )
    foreach ($Candidate in $UvCandidates) {
        if (Test-Path $Candidate) {
            $env:Path = "$(Split-Path -Parent $Candidate);$env:Path"
            break
        }
    }
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv was installed but is not visible yet. Reopen PowerShell and run the installer again."
}

Push-Location $ProjectRoot
$DatabaseBackup = $null
try {
    Write-Step "Installing Python 3.11 and locked dependencies"
    & uv sync --python 3.11
    Assert-LastExit "dependency installation"

    $DatabasePath = Join-Path $ProjectRoot "data\investor.db"
    if (Test-Path $DatabasePath) {
        $BackupTimestamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $DatabaseBackup = Join-Path $ProjectRoot "backups\preinstall-$BackupTimestamp.db"
        Write-Step "Creating a verified pre-migration database backup"
        & uv run investor db backup --output $DatabaseBackup
        Assert-LastExit "database backup"
    }

    Write-Step "Applying idempotent database migrations"
    & uv run investor db migrate
    Assert-LastExit "database migration"

    Write-Step "Running Core diagnostics"
    & uv run investor doctor
    Assert-LastExit "Core diagnostics"

    Write-Step "Installing the hidden Windows Core supervisor"
    $RunnerScript = Join-Path $ProjectRoot "runtime\windows\run-investor-core.ps1"
    if (-not (Test-Path $RunnerScript)) {
        throw "Windows Core supervisor script is missing: $RunnerScript"
    }
    $HiddenLauncher = Join-Path $ProjectRoot "runtime\windows\run-powershell-hidden.vbs"
    if (-not (Test-Path $HiddenLauncher)) {
        throw "Windows hidden process launcher is missing: $HiddenLauncher"
    }
    $WScriptExecutable = Join-Path $env:SystemRoot "System32\wscript.exe"
    $ActionArguments = (
        "`"$HiddenLauncher`" `"$RunnerScript`" " +
        "-ProjectRoot `"$ProjectRoot`""
    )
    $TaskAction = New-ScheduledTaskAction `
        -Execute $WScriptExecutable `
        -Argument $ActionArguments `
        -WorkingDirectory $ProjectRoot
    $CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $TaskTrigger = New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser
    $TaskPrincipal = New-ScheduledTaskPrincipal `
        -UserId $CurrentUser `
        -LogonType Interactive `
        -RunLevel Limited
    $TaskSettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew
    $TaskDefinition = New-ScheduledTask `
        -Action $TaskAction `
        -Trigger $TaskTrigger `
        -Principal $TaskPrincipal `
        -Settings $TaskSettings `
        -Description "Hidden local runtime supervisor for Value DCA Investor Core."
    Register-ScheduledTask `
        -TaskName $CoreTaskName `
        -InputObject $TaskDefinition `
        -Force | Out-Null

    if (-not $DisableAutoUpdate) {
        Write-Step "Installing the guarded GitHub release updater"
        $UpdaterScript = Join-Path $ProjectRoot "runtime\windows\update-value-dca.ps1"
        if (-not (Test-Path $UpdaterScript)) {
            throw "Windows updater script is missing: $UpdaterScript"
        }
        $ExistingUpdateTask = Get-ScheduledTask `
            -TaskName $UpdateTaskName `
            -ErrorAction SilentlyContinue
        if ($null -eq $ExistingUpdateTask -or $ExistingUpdateTask.State -ne "Running") {
            $UpdateArguments = (
                "`"$HiddenLauncher`" `"$UpdaterScript`" " +
                "-InstallDir `"$ProjectRoot`" " +
                "-Repository `"$Repository`" -CoreTaskName `"$CoreTaskName`" " +
                "-HermesProfile `"$HermesProfile`""
            )
            $UpdateAction = New-ScheduledTaskAction `
                -Execute $WScriptExecutable `
                -Argument $UpdateArguments `
                -WorkingDirectory $ProjectRoot
            $UpdateTrigger = New-ScheduledTaskTrigger -Daily -At "04:00"
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
            Register-ScheduledTask `
                -TaskName $UpdateTaskName `
                -InputObject $UpdateDefinition `
                -Force | Out-Null
        }
    }

    if (-not $SkipHermes) {
        Write-Step "Configuring the Hermes profile, Skill and MCP server"
        if (-not (Get-Command hermes -ErrorAction SilentlyContinue)) {
            throw "Hermes CLI is not available in PATH. Install Hermes or rerun with -SkipHermes."
        }

        $ProfilePath = Join-Path $env:LOCALAPPDATA "hermes\profiles\$HermesProfile"
        $CreatedProfile = $false
        if (-not (Test-Path $ProfilePath)) {
            & hermes profile create $HermesProfile --clone-from default
            Assert-LastExit "Hermes profile creation"
            $CreatedProfile = $true
        }
        & hermes profile use $HermesProfile
        Assert-LastExit "Hermes profile selection"

        $SkillSource = Join-Path $ProjectRoot "skills\value-dca-investor"
        $SkillTarget = Join-Path $ProfilePath "skills\value-dca-investor"
        New-Item -ItemType Directory -Force $SkillTarget | Out-Null
        Copy-Item (Join-Path $SkillSource "*") $SkillTarget -Recurse -Force

        $SoulTarget = Join-Path $ProfilePath "SOUL.md"
        if ($CreatedProfile -or -not (Test-Path $SoulTarget)) {
            Copy-Item (Join-Path $ProjectRoot "SOUL.md") $SoulTarget
        }

        $ProfileConfig = Join-Path $ProfilePath "config.yaml"
        & uv run python -m investor_core.hermes_config `
            --config $ProfileConfig `
            --project-root $ProjectRoot `
            --core-url $CoreUrl `
            --task-name $CoreTaskName
        Assert-LastExit "Investor MCP profile configuration"
    }

    if (-not $SkipStart) {
        Write-Step "Starting the hidden Investor Core supervisor"
        $ExpectedVersion = (& uv run investor version).Trim()
        Assert-LastExit "version check"
        Start-ScheduledTask -TaskName $CoreTaskName

        $Ready = $false
        for ($Attempt = 0; $Attempt -lt 30; $Attempt++) {
            Start-Sleep -Milliseconds 500
            try {
                $Report = Invoke-RestMethod "$CoreUrl/ready" -TimeoutSec 2
                if ($Report.status -eq "PASS") {
                    $Health = Invoke-RestMethod "$CoreUrl/health" -TimeoutSec 2
                    if ($Health.version -ne $ExpectedVersion) {
                        throw (
                            "Managed Core version $($Health.version) does not match $ExpectedVersion."
                        )
                    }
                    $Ready = $true
                    break
                }
            }
            catch {
                if ($_.Exception.Message -like "Managed Core version*") {
                    throw
                }
                # The scheduled task may still be starting.
            }
        }
        if (-not $Ready) {
            throw (
                "Managed Investor Core did not become ready within 15 seconds. " +
                "See logs\investor-core.log and logs\investor-core-supervisor.log."
            )
        }
    }

    if (-not $SkipHermes -and -not $SkipMcpTest) {
        Write-Step "Testing the Investor MCP connection"
        & hermes mcp test investor_core
        Assert-LastExit "Investor MCP test"
    }

    Write-Host "`nInstallation / upgrade completed successfully." -ForegroundColor Green
    Write-Host "Installed at: $ProjectRoot"
    Write-Host "Hermes profile: $HermesProfile"
    Write-Host "Windows task: $CoreTaskName (hidden, starts at logon, self-restarts)"
    if (-not $DisableAutoUpdate) {
        Write-Host "Updater task: $UpdateTaskName (stable GitHub releases, daily at 04:00)"
    }
    Write-Host "Core readiness: $CoreUrl/ready"
    Write-Host "Cron, Weixin and broker connections remain disabled."
}
catch {
    if ($null -ne $DatabaseBackup -and (Test-Path $DatabaseBackup)) {
        Write-Warning "Installation failed; restoring the pre-migration database backup."
        $Task = Get-ScheduledTask -TaskName $CoreTaskName -ErrorAction SilentlyContinue
        if ($null -ne $Task -and $Task.State -eq "Running") {
            Stop-ScheduledTask -TaskName $CoreTaskName
        }
        @(Get-InvestorRuntimeProcesses $ProjectRoot) | Stop-Process -Force
        $DatabasePath = Join-Path $ProjectRoot "data\investor.db"
        Copy-Item -LiteralPath $DatabaseBackup -Destination $DatabasePath -Force
        foreach ($Suffix in @("-wal", "-shm")) {
            $Sidecar = "$DatabasePath$Suffix"
            if (Test-Path $Sidecar) {
                Remove-Item -LiteralPath $Sidecar -Force
            }
        }
    }
    throw
}
finally {
    Pop-Location
}
