$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location (Join-Path $root 'frontend')
npm run dev

