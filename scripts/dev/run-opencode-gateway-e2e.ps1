param(
    [Parameter(Mandatory = $true)]
    [string]$OpenCodeExe,
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,
    [Parameter(Mandatory = $true)]
    [string]$ProviderId,
    [Parameter(Mandatory = $true)]
    [string]$ModelId,
    [string]$WorkingDirectory = (Get-Location).Path,
    [string]$Prompt = "Reply with exactly: phase5-ok"
)

$ErrorActionPreference = "Stop"

$gatewayApiKey = $env:AIPG_GATEWAY_API_KEY
if ([string]::IsNullOrWhiteSpace($gatewayApiKey)) {
    throw "AIPG_GATEWAY_API_KEY must be set in the parent process environment."
}

foreach ($path in @($OpenCodeExe, $ConfigPath, $WorkingDirectory)) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Required E2E path does not exist."
    }
}

$previousConfig = $env:OPENCODE_CONFIG
$previousKey = $env:AIPG_GATEWAY_API_KEY

try {
    $env:OPENCODE_CONFIG = (Resolve-Path -LiteralPath $ConfigPath).Path
    $env:AIPG_GATEWAY_API_KEY = $gatewayApiKey

    & $OpenCodeExe models $ProviderId --pure
    if ($LASTEXITCODE -ne 0) {
        throw "OpenCode could not load the generated provider configuration."
    }

    & $OpenCodeExe run $Prompt --model "$ProviderId/$ModelId" --format json --pure --dir $WorkingDirectory
    if ($LASTEXITCODE -ne 0) {
        throw "OpenCode Gateway E2E failed."
    }
}
finally {
    $env:OPENCODE_CONFIG = $previousConfig
    $env:AIPG_GATEWAY_API_KEY = $previousKey
}
