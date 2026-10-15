$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
& .\.venv\Scripts\python.exe -m uvicorn goldsentinel.api:app --host 127.0.0.1 --port 8000 --reload

