[CmdletBinding()]
param(
    [string]$InstallDir = "C:\investor\value-dca-agent",
    [string]$Repository = "Mr-Monologue/Agent_CompoundInterest_Plan",
    [string]$CoreTaskName = "ValueDCAInvestorCore",
    [string]$UpdateTaskName = "ValueDCAAgentUpdate",
    [string]$HermesProfile = "investor",
    [switch]$SkipHermes,
    [switch]$CheckOnly,
    [switch]$Force,
    [switch]$AllowManualUpdate
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "update-safety.ps1")
Set-StrictMode -Version Latest

$InstallDir = [IO.Path]::GetFullPath($InstallDir).TrimEnd("\")
$LogDirectory = Join-Path $InstallDir "logs"
$BackupDirectory = Join-Path $InstallDir "backups\updates"
$StatePath = Join-Path $InstallDir "data\update-state.json"
New-Item -ItemType Directory -Force $LogDirectory, $BackupDirectory | Out-Null
$LogPath = Join-Path $LogDirectory "updater.log"

function Write-UpdateLog([string]$Message) {
    $Line = "$(Get-Date -Format o) $Message"
    Add-Content -LiteralPath $LogPath -Value $Line
    if (-not $CheckOnly) {
        Write-Host $Message
    }
}

function Quote-NativeArgument([string]$Value) {
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Get-InstalledVersion {
    $VersionFile = Join-Path $InstallDir "src\investor_core\version.py"
    if (-not (Test-Path $VersionFile)) {
        throw "Installed version file is missing: $VersionFile"
    }
    $VersionText = Get-Content -LiteralPath $VersionFile -Raw
    $Match = [regex]::Match($VersionText, '__version__\s*=\s*"(?<version>\d+\.\d+\.\d+)"')
    if (-not $Match.Success) {
        throw "Could not read the installed version from $VersionFile"
    }
    return [version]$Match.Groups["version"].Value
}

function Get-InvestorSupervisorProcesses {
    $RunnerScript = [IO.Path]::GetFullPath(
        (Join-Path $InstallDir "runtime\windows\run-investor-core.ps1")
    )
    $RunnerArgument = '-File "' + $RunnerScript + '"'
    return @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $_.Name -in @("powershell.exe", "pwsh.exe") -and
                $null -ne $_.CommandLine -and
                $_.CommandLine.IndexOf(
                    $RunnerArgument,
                    [StringComparison]::OrdinalIgnoreCase
                ) -ge 0
            } |
            ForEach-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }
    )
}

function Stop-InvestorRuntime {
    $Task = Get-ScheduledTask -TaskName $CoreTaskName -ErrorAction SilentlyContinue
    if ($null -ne $Task -and $Task.State -eq "Running") {
        Stop-ScheduledTask -TaskName $CoreTaskName
    }

    $Supervisors = @(Get-InvestorSupervisorProcesses)
    if ($Supervisors.Count -gt 0) {
        Write-UpdateLog (
            "Stopping managed Core supervisors for update: " +
            (($Supervisors | ForEach-Object { "powershell:$($_.Id)" }) -join ", ")
        )
        $Supervisors | Stop-Process -Force
    }

    $NormalizedRoot = [IO.Path]::GetFullPath($InstallDir).TrimEnd("\")
    $Processes = @(
        Get-Process -Name "investor-mcp", "investor-core" -ErrorAction SilentlyContinue |
            Where-Object {
                $null -ne $_.Path -and
                $_.Path.StartsWith($NormalizedRoot, [StringComparison]::OrdinalIgnoreCase)
            }
    )
    if ($Processes.Count -gt 0) {
        Write-UpdateLog (
            "Stopping managed runtime processes for update: " +
            (($Processes | ForEach-Object { "$($_.ProcessName):$($_.Id)" }) -join ", ")
        )
        $Processes | Stop-Process -Force
    }

    for ($Attempt = 0; $Attempt -lt 20; $Attempt++) {
        $RemainingSupervisors = @(Get-InvestorSupervisorProcesses)
        $Remaining = @(
            Get-Process -Name "investor-mcp", "investor-core" -ErrorAction SilentlyContinue |
                Where-Object {
                    $null -ne $_.Path -and
                    $_.Path.StartsWith($NormalizedRoot, [StringComparison]::OrdinalIgnoreCase)
                }
        )
        if ($RemainingSupervisors.Count -eq 0 -and $Remaining.Count -eq 0) {
            return
        }
        Start-Sleep -Milliseconds 500
    }
    throw "Investor supervisor or runtime processes did not stop within 10 seconds."
}

function Assert-Robocopy([string]$Operation) {
    if ($LASTEXITCODE -ge 8) {
        throw "$Operation failed with Robocopy exit code $LASTEXITCODE"
    }
}

$Mutex = [Threading.Mutex]::new($false, "Local\ValueDCAAgentUpdate")
if (-not $Mutex.WaitOne(0)) {
    Write-UpdateLog "Another update check is already running; exiting."
    exit 0
}

$WorkingDirectory = Join-Path $env:TEMP "value-dca-update-$([guid]::NewGuid())"
$RollbackRoot = $null
$RollbackReady = $false
$RuntimeStopped = $false
$WasRunning = $false
$InstallationStarted = $false
$Paths = $null
$OriginalDatabaseEnv = $env:INVESTOR_DB_PATH
$RecoveryStatus = "NOT_NEEDED"
$CurrentVersion = $null
$CoreTaskXml = $null
$UpdateTaskXml = $null
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $Headers = @{
        Accept = "application/vnd.github+json"
        "User-Agent" = "value-dca-agent-windows-updater"
        "X-GitHub-Api-Version" = "2022-11-28"
    }
    $ReleaseUri = "https://api.github.com/repos/$Repository/releases/latest"
    Write-UpdateLog "Checking the latest stable GitHub release from $Repository"
    $Release = Invoke-RestMethod -Uri $ReleaseUri -Headers $Headers -TimeoutSec 30
    if ($Release.draft -or $Release.prerelease) {
        throw "GitHub returned a draft or prerelease as the latest stable release."
    }

    $CurrentVersion = Get-InstalledVersion
    $Tag = [string]$Release.tag_name
    if ($Tag -notmatch '^v(?<version>\d+\.\d+\.\d+)$') {
        throw "Latest release tag is not a supported semantic version: $Tag"
    }
    $AvailableVersion = [version]$Matches["version"]
    if (-not $Force -and $AvailableVersion -le $CurrentVersion) {
        Write-UpdateLog "Already current: v$CurrentVersion"
        exit 0
    }
    if ($CheckOnly) {
        Write-Host "Update available: v$CurrentVersion -> v$AvailableVersion"
        exit 0
    }

    New-Item -ItemType Directory -Force $WorkingDirectory | Out-Null
    $ArchivePath = Join-Path $WorkingDirectory "release.zip"
    $ExtractPath = Join-Path $WorkingDirectory "source"
    Write-UpdateLog "Downloading the tagged GitHub release source for $Tag over HTTPS"
    Invoke-WebRequest -Uri $Release.zipball_url -Headers $Headers -OutFile $ArchivePath `
        -TimeoutSec 120
    Expand-Archive -LiteralPath $ArchivePath -DestinationPath $ExtractPath -Force

    $ManifestFile = Get-ChildItem -LiteralPath $ExtractPath -Filter "release-manifest.json" `
        -File -Recurse | Select-Object -First 1
    if ($null -eq $ManifestFile) {
        throw "Release manifest is missing; refusing an ungoverned update."
    }
    $SourceRoot = $ManifestFile.Directory.FullName
    $Manifest = Get-Content -LiteralPath $ManifestFile.FullName -Raw | ConvertFrom-Json
    if ([int]$Manifest.schema_version -ne 1) {
        throw "Unsupported release manifest schema: $($Manifest.schema_version)"
    }
    if ([string]$Manifest.channel -ne "stable") {
        throw "Release channel is not stable: $($Manifest.channel)"
    }
    if ([version]([string]$Manifest.version) -ne $AvailableVersion) {
        throw "Release tag and manifest version do not match."
    }
    if (-not [bool]$Manifest.auto_update -and -not $Force) {
        Write-UpdateLog "Release $Tag is not approved for automatic installation."
        exit 0
    }
    if ([bool]$Manifest.requires_manual_approval -and -not $AllowManualUpdate -and -not $Force) {
        Write-UpdateLog "Release $Tag requires explicit manual approval; no changes were made."
        exit 0
    }
    if ($CurrentVersion -lt [version]([string]$Manifest.minimum_current_version)) {
        throw (
            "Installed v$CurrentVersion is older than the supported upgrade floor " +
            "v$($Manifest.minimum_current_version)."
        )
    }

    $ExistingCoreTask = Get-ScheduledTask -TaskName $CoreTaskName -ErrorAction SilentlyContinue
    $WasRunning = ($null -ne $ExistingCoreTask -and $ExistingCoreTask.State -eq "Running") -or (@(Get-InvestorSupervisorProcesses).Count -gt 0)
    if ($null -ne $ExistingCoreTask) {
        $CoreTaskXml = Export-ScheduledTask -TaskName $CoreTaskName
    }
    $ExistingUpdateTask = Get-ScheduledTask -TaskName $UpdateTaskName `
        -ErrorAction SilentlyContinue
    if ($null -ne $ExistingUpdateTask) {
        $UpdateTaskXml = Export-ScheduledTask -TaskName $UpdateTaskName
    }

    $Paths = Get-UpdatePaths $InstallDir
    $env:INVESTOR_DB_PATH = $Paths.Database
    $Installer = Join-Path $SourceRoot "install-windows.ps1"
    if (-not (Test-Path -LiteralPath $Installer)) { throw "Release installer missing" }
    $Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $RollbackRoot = Join-Path $BackupDirectory "$Timestamp-v$CurrentVersion"
    $CodeBackup = Join-Path $RollbackRoot "code"
    $DatabaseBackup = Join-Path $RollbackRoot "investor.db"
    New-Item -ItemType Directory -Force $CodeBackup | Out-Null
    # Private snapshots contain configuration and task definitions.
    $CurrentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & icacls $RollbackRoot /inheritance:r /grant:r "*${CurrentSid}:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Cannot secure backup directory" }
    Copy-Item -LiteralPath (Join-Path $InstallDir ".env") -Destination (Join-Path $RollbackRoot "config.env")
    $TaskRecords = @(Get-ScheduledTask | Where-Object { $_.TaskName -like "ValueDCA*" } | ForEach-Object {
        @{ name=$_.TaskName; xml=(Export-ScheduledTask -TaskName $_.TaskName) }
    })
    $TaskRecords | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $RollbackRoot "tasks.json") -Encoding UTF8

    Write-UpdateLog "Creating rollback snapshot at $RollbackRoot"
    & robocopy $InstallDir $CodeBackup /E /R:2 /W:1 `
        /XD .git .venv .mypy_cache .pytest_cache .ruff_cache __pycache__ data logs backups `
        /XF .env *.pyc *.db *.db-wal *.db-shm
    Assert-Robocopy "code backup"

    # All existence/write preflight and code/config backups complete before downtime.
    $RuntimeStopped = $true
    Stop-InvestorRuntime
    $DatabasePath = $Paths.Database
    $InvestorCli = $Paths.Cli
    Push-Location $InstallDir
    try {
        & $InvestorCli db backup --output $DatabaseBackup
        if ($LASTEXITCODE -ne 0) { throw "Verified database backup failed with exit code $LASTEXITCODE" }
    } finally { Pop-Location }
    Complete-RecoverySnapshot $RollbackRoot $Paths.Python
    $RollbackReady = $true
    $InstallationStarted = $true

    Write-UpdateLog "Installing v$AvailableVersion"
    $InstallerStdoutPath = Join-Path $WorkingDirectory "installer.stdout.log"
    $InstallerStderrPath = Join-Path $WorkingDirectory "installer.stderr.log"
    $PowerShellExecutable = Join-Path $env:SystemRoot `
        "System32\WindowsPowerShell\v1.0\powershell.exe"
    $InstallerArguments = @(
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        (Quote-NativeArgument $Installer),
        "-InstallDir",
        (Quote-NativeArgument $InstallDir),
        "-Repository",
        (Quote-NativeArgument $Repository),
        "-CoreTaskName",
        (Quote-NativeArgument $CoreTaskName),
        "-HermesProfile",
        (Quote-NativeArgument $HermesProfile),
        "-UpdateTaskName",
        (Quote-NativeArgument $UpdateTaskName),
        "-NonInteractive",
        "-SkipMcpTest"
    )
    if ($SkipHermes) {
        $InstallerArguments += "-SkipHermes"
    }
    $InstallerProcess = Start-Process `
        -FilePath $PowerShellExecutable `
        -ArgumentList $InstallerArguments `
        -RedirectStandardOutput $InstallerStdoutPath `
        -RedirectStandardError $InstallerStderrPath `
        -WindowStyle Hidden `
        -Wait `
        -PassThru
    $InstallerExitCode = $InstallerProcess.ExitCode
    foreach ($InstallerLine in @(Get-Content $InstallerStdoutPath -ErrorAction SilentlyContinue)) {
        Write-UpdateLog "installer stdout: $InstallerLine"
    }
    foreach ($InstallerLine in @(Get-Content $InstallerStderrPath -ErrorAction SilentlyContinue)) {
        Write-UpdateLog "installer stderr: $InstallerLine"
    }
    if ($InstallerExitCode -ne 0) {
        throw "Release installer failed with exit code $InstallerExitCode"
    }

    $FinalizerScript = Join-Path $InstallDir `
        "runtime\windows\finalize-update-task.ps1"
    $HiddenLauncher = Join-Path $InstallDir `
        "runtime\windows\run-powershell-hidden.vbs"
    $WScriptExecutable = Join-Path $env:SystemRoot "System32\wscript.exe"
    if (-not (Test-Path $FinalizerScript) -or -not (Test-Path $HiddenLauncher)) {
        throw "Installed update-task finalizer or hidden launcher is missing."
    }
    $FinalizerArguments = @(
        (Quote-NativeArgument $HiddenLauncher),
        (Quote-NativeArgument $FinalizerScript),
        "-InstallDir",
        (Quote-NativeArgument $InstallDir),
        "-Repository",
        (Quote-NativeArgument $Repository),
        "-CoreTaskName",
        (Quote-NativeArgument $CoreTaskName),
        "-UpdateTaskName",
        (Quote-NativeArgument $UpdateTaskName),
        "-HermesProfile",
        (Quote-NativeArgument $HermesProfile)
    )
    if ($SkipHermes) {
        $FinalizerArguments += "-SkipHermes"
    }
    Start-Process -FilePath $WScriptExecutable -ArgumentList $FinalizerArguments `
        -WindowStyle Hidden | Out-Null

    $State = [ordered]@{
        status = "UPDATED"
        previous_version = [string]$CurrentVersion
        installed_version = [string]$AvailableVersion
        release_tag = $Tag
        checked_at = (Get-Date -Format o)
        rollback_snapshot = $RollbackRoot
    }
    $State | ConvertTo-Json | Set-Content -LiteralPath $StatePath -Encoding UTF8
    Write-UpdateLog "Update completed: v$CurrentVersion -> v$AvailableVersion"
}
catch {
    $Failure = $_.Exception.Message
    Write-UpdateLog "Update failed: $Failure"
    if ($InstallationStarted -and $RollbackReady -and $null -ne $RollbackRoot -and (Test-Path $RollbackRoot)) {
        Write-UpdateLog "Restoring v$CurrentVersion from $RollbackRoot"
        try {
            Assert-RecoverySnapshot $RollbackRoot $Paths.Python
            Stop-InvestorRuntime
            $CodeBackup = Join-Path $RollbackRoot "code"
            & robocopy $CodeBackup $InstallDir /MIR /R:2 /W:1 `
                /XD .git .venv .mypy_cache .pytest_cache .ruff_cache __pycache__ data logs backups `
                /XF .env *.pyc *.db *.db-wal *.db-shm
            Assert-Robocopy "code rollback"

            Copy-Item -LiteralPath (Join-Path $RollbackRoot "config.env") -Destination (Join-Path $InstallDir ".env") -Force
            $DatabaseBackup = Join-Path $RollbackRoot "investor.db"
            $DatabasePath = Join-Path $InstallDir "data\investor.db"
            if (Test-Path $DatabaseBackup) {
                Copy-Item -LiteralPath $DatabaseBackup -Destination $DatabasePath -Force
                foreach ($Suffix in @("-wal", "-shm")) {
                    $Sidecar = "$DatabasePath$Suffix"
                    if (Test-Path $Sidecar) {
                        Remove-Item -LiteralPath $Sidecar -Force
                    }
                }
            }

            Push-Location $InstallDir
            try {
                & uv sync --python 3.11 --reinstall-package value-dca-agent
                if ($LASTEXITCODE -ne 0) { throw "rollback dependency restore failed" }
                & uv run investor db migrate
                if ($LASTEXITCODE -ne 0) { throw "rollback migration restore failed" }
                & uv run investor doctor
                if ($LASTEXITCODE -ne 0) { throw "rollback diagnostics failed" }

                $ProfilePath = Join-Path $env:LOCALAPPDATA "hermes\profiles\$HermesProfile"
                $SkillNames = @("value-dca-investor", "investor-core-operations")
                foreach ($SkillName in $SkillNames) {
                    $SkillSource = Join-Path $InstallDir "skills\$SkillName"
                    $SkillTarget = Join-Path $ProfilePath "skills\$SkillName"
                    if (-not $SkipHermes -and (Test-Path $ProfilePath) -and
                        (Test-Path $SkillSource)) {
                        New-Item -ItemType Directory -Force $SkillTarget | Out-Null
                        Copy-Item (Join-Path $SkillSource "*") $SkillTarget -Recurse -Force
                    }
                }
            }
            finally {
                Pop-Location
            }
            if ($null -ne $CoreTaskXml) {
                Register-ScheduledTask -TaskName $CoreTaskName -Xml $CoreTaskXml `
                    -Force | Out-Null
            }
            if ($null -ne $UpdateTaskXml) {
                try {
                    Register-ScheduledTask -TaskName $UpdateTaskName `
                        -Xml $UpdateTaskXml -Force | Out-Null
                }
                catch {
                    Write-UpdateLog (
                        "Could not restore the updater task while its current run is active: " +
                        "$($_.Exception.Message)"
                    )
                }
            }
            Start-ScheduledTask -TaskName $CoreTaskName
            $RecoveryStatus = Get-CoreRecoveryStatus ([string]$CurrentVersion)
            Write-UpdateLog "Rollback data/code restored to v$CurrentVersion; Core status: $RecoveryStatus"
        }
        catch {
            $RecoveryStatus = "ROLLBACK_FAILED_DATA_STATE_REQUIRES_INSPECTION"
            Write-UpdateLog "ROLLBACK FAILED: $($_.Exception.Message)"
        }
    }
    elseif ($RuntimeStopped -and -not $InstallationStarted) {
        try {
            if ($WasRunning) {
                Start-ScheduledTask -TaskName $CoreTaskName
                $RecoveryStatus = Get-CoreRecoveryStatus ([string]$CurrentVersion)
            } else { $RecoveryStatus = "ORIGINAL_STOPPED_STATE_PRESERVED" }
            Write-UpdateLog "Installation never started; original data/code unchanged. Core: $RecoveryStatus"
        } catch { $RecoveryStatus = "ORIGINAL_DATA_UNCHANGED_CORE_RESTART_FAILED" }
    }
    @{ status="FAILED"; failure=$Failure; installation_started=$InstallationStarted;
       snapshot_verified=$RollbackReady; recovery=$RecoveryStatus; rollback_snapshot=$RollbackRoot } |
        ConvertTo-Json | Set-Content -LiteralPath $StatePath -Encoding UTF8
    throw
}
finally {
    $env:INVESTOR_DB_PATH = $OriginalDatabaseEnv
    $ResolvedTemporary = [IO.Path]::GetFullPath($WorkingDirectory)
    $TemporaryRoot = [IO.Path]::GetFullPath($env:TEMP).TrimEnd("\") + "\"
    if (-not $ResolvedTemporary.StartsWith($TemporaryRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing cleanup outside temporary directory"
    }
    if (Test-Path $WorkingDirectory) {
        Remove-Item -LiteralPath $WorkingDirectory -Recurse -Force
    }
    $Mutex.ReleaseMutex()
    $Mutex.Dispose()
}
