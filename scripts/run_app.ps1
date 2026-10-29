<#
.SYNOPSIS
  Start the Streamlit dashboard from the repo's .venv (read-only; it never calls external sources).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\run_app.ps1
  powershell -ExecutionPolicy Bypass -File scripts\run_app.ps1 -Port 8502
#>
param([int]$Port = 8501)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$streamlit = Join-Path $root ".venv\Scripts\streamlit.exe"
if (-not (Test-Path $streamlit)) { throw "Run scripts\setup.ps1 first (.venv not found)." }

& $streamlit run (Join-Path $root "app\Home.py") --server.port $Port --server.headless true `
    --browser.gatherUsageStats false
exit $LASTEXITCODE
