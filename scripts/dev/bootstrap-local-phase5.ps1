[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$live = Join-Path $root "runtime\phase5-live"
$sidecarExe = Join-Path $root "runtime\poc\cliproxyapi-7.2.153\cli-proxy-api.exe"
$authDir = Join-Path $root "runtime\poc\auths"
$python = Join-Path $root ".venv\Scripts\python.exe"

foreach ($requiredPath in @($sidecarExe, $authDir, $python)) {
    if (-not (Test-Path -LiteralPath $requiredPath)) { throw "Required local runtime path is missing." }
}
foreach ($port in @(8317, 8020)) {
    $listener = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($listener) { throw "Refusing to start because a local service already listens on port $port." }
}
New-Item -ItemType Directory -Force -Path $live | Out-Null

function New-LocalSecret {
    $bytes = New-Object byte[] 32
    $generator = New-Object System.Security.Cryptography.RNGCryptoServiceProvider
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }
    return ([BitConverter]::ToString($bytes)).Replace("-", "").ToLowerInvariant()
}

$sidecarApiKey = New-LocalSecret
$managementKey = New-LocalSecret
$authDirYaml = $authDir.Replace("\", "/")
$sidecarConfig = @"
host: "127.0.0.1"
port: 8317
remote-management:
  allow-remote: false
  secret-key: "$managementKey"
  disable-control-panel: true
auth-dir: "$authDirYaml"
api-keys:
  - "$sidecarApiKey"
debug: false
logging-to-file: false
usage-statistics-enabled: false
request-retry: 0
max-retry-credentials: 1
quota-exceeded:
  switch-project: false
  switch-preview-model: false
  antigravity-credits: false
routing:
  strategy: "fill-first"
  session-affinity: false
"@
$configPath = Join-Path $live "cliproxy.local.yaml"
Set-Content -LiteralPath $configPath -Value $sidecarConfig -Encoding UTF8

& $python -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw "Database migration failed." }
$gatewayKeyId = "phase5-manual-$([Guid]::NewGuid().ToString('N').Substring(0, 8))"
$gatewayKey = (& $python -m ai_provider_gateway.gateway.bootstrap --database-url sqlite:///./data/gateway.db --key-id $gatewayKeyId | Select-Object -Last 1).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($gatewayKey)) { throw "Gateway API-key bootstrap failed." }

$envFile = @'
$env:AIPG_DATABASE_URL = 'sqlite:///./data/gateway.db'
$env:AIPG_SIDECAR_BASE_URL = 'http://127.0.0.1:8317'
$env:AIPG_SIDECAR_API_KEY = '{0}'
$env:AIPG_SIDECAR_MANAGEMENT_KEY = '{1}'
$env:AIPG_GATEWAY_API_KEY = '{2}'
$env:AIPG_GATEWAY_BASE_URL = 'http://127.0.0.1:8020/v1'
'@ -f $sidecarApiKey, $managementKey, $gatewayKey
Set-Content -LiteralPath (Join-Path $live "env.ps1") -Value $envFile -Encoding UTF8

$sidecarStart = @{
    FilePath = $sidecarExe
    ArgumentList = @("-config", "cliproxy.local.yaml")
    WorkingDirectory = $live
    WindowStyle = "Hidden"
    RedirectStandardOutput = (Join-Path $live "sidecar.stdout.log")
    RedirectStandardError = (Join-Path $live "sidecar.stderr.log")
    PassThru = $true
}
$sidecar = Start-Process @sidecarStart

$sidecarResponse = $null
for ($attempt = 0; $attempt -lt 60 -and $null -eq $sidecarResponse; $attempt++) {
    Start-Sleep -Milliseconds 500
    try {
        $sidecarResponse = Invoke-RestMethod "http://127.0.0.1:8317/v1/models" -Headers @{ Authorization = "Bearer $sidecarApiKey" } -TimeoutSec 2
    } catch {
        if ($sidecar.HasExited) { throw "Sidecar exited during startup." }
    }
}
if ($null -eq $sidecarResponse) {
    if (-not $sidecar.HasExited) { Stop-Process -Id $sidecar.Id -Force -ErrorAction SilentlyContinue }
    throw "Sidecar did not become ready."
}
$sidecarModelCount = @($sidecarResponse.data).Count

$env:AIPG_DATABASE_URL = "sqlite:///./data/gateway.db"
$env:AIPG_SIDECAR_BASE_URL = "http://127.0.0.1:8317"
$env:AIPG_SIDECAR_API_KEY = $sidecarApiKey
$env:AIPG_GATEWAY_API_KEY = $gatewayKey
$env:AIPG_GATEWAY_BASE_URL = "http://127.0.0.1:8020/v1"

$gatewayStart = @{
    FilePath = $python
    ArgumentList = @("-m", "uvicorn", "ai_provider_gateway.gateway.runtime_app:create_runtime_app", "--factory", "--host", "127.0.0.1", "--port", "8020")
    WorkingDirectory = $root
    WindowStyle = "Hidden"
    RedirectStandardOutput = (Join-Path $live "gateway.stdout.log")
    RedirectStandardError = (Join-Path $live "gateway.stderr.log")
    PassThru = $true
}
$gateway = Start-Process @gatewayStart

$gatewayHealth = $null
for ($attempt = 0; $attempt -lt 60 -and $null -eq $gatewayHealth; $attempt++) {
    Start-Sleep -Milliseconds 500
    try {
        $gatewayHealth = Invoke-RestMethod "http://127.0.0.1:8020/healthz" -TimeoutSec 2
    } catch {
        if ($gateway.HasExited) {
            Stop-Process -Id $sidecar.Id -Force -ErrorAction SilentlyContinue
            throw "Gateway exited during startup."
        }
    }
}
if ($null -eq $gatewayHealth) {
    if (-not $gateway.HasExited) { Stop-Process -Id $gateway.Id -Force -ErrorAction SilentlyContinue }
    if (-not $sidecar.HasExited) { Stop-Process -Id $sidecar.Id -Force -ErrorAction SilentlyContinue }
    throw "Gateway did not become ready."
}

$gatewayResponse = Invoke-RestMethod "http://127.0.0.1:8020/v1/models" -Headers @{ Authorization = "Bearer $gatewayKey" } -TimeoutSec 5
$gatewayModelCount = @($gatewayResponse.data).Count

$status = [ordered]@{
    sidecar_pid = $sidecar.Id
    gateway_pid = $gateway.Id
    sidecar_model_count = $sidecarModelCount
    gateway_model_count = $gatewayModelCount
    sidecar_base_url = "http://127.0.0.1:8317"
    gateway_base_url = "http://127.0.0.1:8020/v1"
    config_path = $configPath
    env_file = (Join-Path $live "env.ps1")
}
$status | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $live "status.json") -Encoding UTF8

if ($gatewayModelCount -gt 0) {
    & $python -m ai_provider_gateway.integrations.export opencode --database-url sqlite:///./data/gateway.db --output (Join-Path $live "opencode.json")
    if ($LASTEXITCODE -ne 0) { throw "OpenCode config export failed." }
}

Write-Output "LOCAL_SERVICES_STARTED"
Write-Output "SIDECAR_PID=$($sidecar.Id)"
Write-Output "GATEWAY_PID=$($gateway.Id)"
Write-Output "SIDECAR_MODEL_COUNT=$sidecarModelCount"
Write-Output "GATEWAY_MODEL_COUNT=$gatewayModelCount"
Write-Output "RUNTIME_DIR=$live"
