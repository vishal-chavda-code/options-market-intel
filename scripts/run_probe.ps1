<#
.SYNOPSIS
  Run `omi probe` from the repo's .venv. Any arguments are passed through.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\run_probe.ps1
  powershell -ExecutionPolicy Bypass -File scripts\run_probe.ps1 --source occ --source cftc_cot -v
#>
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$omi = Join-Path $root ".venv\Scripts\omi.exe"
if (-not (Test-Path $omi)) { throw "omi is not installed; run scripts\setup.ps1 first." }

& $omi probe @args
exit $LASTEXITCODE
