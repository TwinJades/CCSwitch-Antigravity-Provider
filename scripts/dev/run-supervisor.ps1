$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '..\..\.venv\Scripts\python.exe'
& $python -m uvicorn ai_provider_gateway.control.runtime_app:create_runtime_app --factory --host 127.0.0.1 --port 8010
