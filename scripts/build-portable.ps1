[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SidecarExecutable,
    [Parameter(Mandatory = $true)]
    [string]$SidecarManifest,
    [Parameter(Mandatory = $true)]
    [string]$SidecarLicense
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $root ".venv\Scripts\python.exe"
$spec = Join-Path $root "packaging\start.spec"
$projectLicense = Join-Path $root "LICENSE"

foreach ($required in @($python, $spec, $projectLicense, $SidecarExecutable, $SidecarManifest, $SidecarLicense)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "A required portable-build input is missing."
    }
}

$manifest = Get-Content -Raw -LiteralPath $SidecarManifest | ConvertFrom-Json
foreach ($field in @("name", "version", "commit", "sha256", "download_source", "installed_at", "tested")) {
    if ($null -eq $manifest.$field -or [string]::IsNullOrWhiteSpace([string]$manifest.$field)) {
        throw "The Sidecar manifest is incomplete."
    }
}
if ([string]$manifest.version -match "(?i)latest") {
    throw "A fixed Sidecar version is required."
}
foreach ($field in @("name", "version", "commit")) {
    if ([string]$manifest.$field -notmatch "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$" -or [string]$manifest.$field -match "\.\.") {
        throw "The Sidecar manifest contains an unsafe identifier."
    }
}
if ([string]$manifest.sha256 -notmatch "^[0-9a-fA-F]{64}$") {
    throw "The manifest must contain a trusted SHA256 value."
}
if ($manifest.tested -isnot [bool] -or -not $manifest.tested) {
    throw "The Sidecar manifest must describe an explicitly tested build."
}
$sourceUri = $null
if (-not [Uri]::TryCreate([string]$manifest.download_source, [UriKind]::Absolute, [ref]$sourceUri) -or $sourceUri.Scheme -ne "https" -or -not [string]::IsNullOrEmpty($sourceUri.UserInfo)) {
    throw "The Sidecar manifest download source must be a credential-free HTTPS URL."
}
$actualSha256 = (Get-FileHash -LiteralPath $SidecarExecutable -Algorithm SHA256).Hash
if (-not [string]::Equals($actualSha256, [string]$manifest.sha256, [StringComparison]::OrdinalIgnoreCase)) {
    throw "The Sidecar executable does not match the trusted manifest."
}

$stamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
$buildRoot = Join-Path $root "runtime\package-build\$stamp"
$distRoot = Join-Path $buildRoot "dist"
$workRoot = Join-Path $buildRoot "work"
New-Item -ItemType Directory -Path $distRoot, $workRoot | Out-Null

& $python -m PyInstaller --distpath $distRoot --workpath $workRoot $spec
if ($LASTEXITCODE -ne 0) {
    throw "Portable application build failed."
}

$bundle = Join-Path $distRoot "AIProviderGateway"
$sidecarDir = Join-Path $bundle "sidecar"
foreach ($directory in @("app", "config", "data", "logs", "runtime", "sidecar")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $bundle $directory) | Out-Null
}
Copy-Item -LiteralPath $SidecarExecutable -Destination (Join-Path $sidecarDir "cli-proxy-api.exe")
Copy-Item -LiteralPath $SidecarManifest -Destination (Join-Path $sidecarDir "manifest.json")
Copy-Item -LiteralPath $SidecarLicense -Destination (Join-Path $sidecarDir "LICENSE")
Copy-Item -LiteralPath (Join-Path $root "README.md") -Destination $bundle
Copy-Item -LiteralPath (Join-Path $root "THIRD_PARTY_NOTICES.md") -Destination $bundle
Copy-Item -LiteralPath $projectLicense -Destination $bundle

$archive = Join-Path $buildRoot "AIProviderGateway-portable.zip"
Compress-Archive -Path (Join-Path $bundle "*") -DestinationPath $archive -CompressionLevel Optimal
Write-Output $archive
