[CmdletBinding()]
param(
    [string]$InstallDir = "C:\investor\value-dca-agent",
    [string]$Repository = "Mr-Monologue/Agent_CompoundInterest_Plan"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Headers = @{
    Accept = "application/vnd.github+json"
    "User-Agent" = "value-dca-agent-windows-bootstrap"
    "X-GitHub-Api-Version" = "2022-11-28"
}
$WorkingDirectory = Join-Path $env:TEMP "value-dca-bootstrap-$([guid]::NewGuid())"

try {
    Write-Host "Fetching the latest stable Value DCA release..." -ForegroundColor Cyan
    $Release = Invoke-RestMethod `
        -Uri "https://api.github.com/repos/$Repository/releases/latest" `
        -Headers $Headers `
        -TimeoutSec 30
    if ($Release.draft -or $Release.prerelease) {
        throw "GitHub returned a draft or prerelease as the latest stable release."
    }
    $Tag = [string]$Release.tag_name
    if ($Tag -notmatch '^v(?<version>\d+\.\d+\.\d+)$') {
        throw "Latest release tag is not a supported semantic version: $Tag"
    }
    $ReleaseVersion = [version]$Matches["version"]

    New-Item -ItemType Directory -Force $WorkingDirectory | Out-Null
    $ArchivePath = Join-Path $WorkingDirectory "release.zip"
    $ExtractPath = Join-Path $WorkingDirectory "source"
    Invoke-WebRequest -Uri $Release.zipball_url -Headers $Headers -OutFile $ArchivePath `
        -TimeoutSec 120
    Expand-Archive -LiteralPath $ArchivePath -DestinationPath $ExtractPath -Force

    $ManifestFile = Get-ChildItem -LiteralPath $ExtractPath -Filter "release-manifest.json" `
        -File -Recurse | Select-Object -First 1
    if ($null -eq $ManifestFile) {
        throw "Release manifest is missing; installation was stopped."
    }
    $Manifest = Get-Content -LiteralPath $ManifestFile.FullName -Raw | ConvertFrom-Json
    if (
        [int]$Manifest.schema_version -ne 1 -or
        [string]$Manifest.channel -ne "stable" -or
        [version]([string]$Manifest.version) -ne $ReleaseVersion
    ) {
        throw "Release manifest validation failed."
    }

    $Installer = Join-Path $ManifestFile.Directory.FullName "install-windows.ps1"
    Write-Host "Installing $Tag from GitHub..." -ForegroundColor Cyan
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Installer `
        -InstallDir $InstallDir `
        -Repository $Repository `
        -NonInteractive `
        -SkipMcpTest
    if ($LASTEXITCODE -ne 0) {
        throw "Installation failed with exit code $LASTEXITCODE"
    }

    Write-Host "Value DCA is now managed by GitHub releases." -ForegroundColor Green
}
finally {
    if (Test-Path $WorkingDirectory) {
        Remove-Item -LiteralPath $WorkingDirectory -Recurse -Force
    }
}
