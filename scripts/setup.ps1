$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if (-not (Test-Path '.venv')) { python -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -e '.[dev,mt5]'
Set-Location (Join-Path $root 'frontend')
npm install
Write-Host 'GoldSentinel dependencies installed.' -ForegroundColor Green

