<#
.SYNOPSIS
  The nightly job: `omi daily` (all enabled sources + features), then optionally the slides.
  Called by the scheduled tasks that scripts\register_tasks.ps1 creates; safe to run by hand.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\run_daily.ps1
  powershell -ExecutionPolicy Bypass -File scripts\run_daily.ps1 -Report
#>
param([switch]$Report)
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$omi = Join-Path $root ".venv\Scripts\omi.exe"
if (-not (Test-Path $omi)) { throw "Run scripts\setup.ps1 first (.venv not found)." }
Set-Location $root

& $omi daily
$code = $LASTEXITCODE   # 1 = at least one source failed (others still ran)

if ($Report) {
    & $omi report
    if ($LASTEXITCODE -ne 0 -and $code -eq 0) { $code = $LASTEXITCODE }
}
exit $code
