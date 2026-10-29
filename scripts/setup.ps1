<#
.SYNOPSIS
  Create .venv in the repo and install omi into it (editable), then copy the example config.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -Dev       # also pytest + ruff
  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -Python "C:\Python311\python.exe"
#>
param(
    [string]$Python = "",
    [switch]$Dev
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root ".venv"
$py = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "Creating virtual environment in $venv"
    if ($Python) {
        & $Python -m venv $venv
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m venv $venv
    } else {
        & python -m venv $venv
    }
    if ($LASTEXITCODE -ne 0) { throw "Could not create the virtual environment." }
}

& $py -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "omi needs Python 3.11+. Delete .venv and re-run with -Python <path to python 3.11+>."
}

# pip honours HTTPS_PROXY. Behind SSL inspection, pip >= 24.2 already uses the Windows
# certificate store; older pips need --use-feature=truststore (handled below).
& $py -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    Write-Warning "pip upgrade failed; retrying with the Windows certificate store."
    & $py -m pip install --use-feature=truststore --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "pip could not reach PyPI. Check proxy settings (HTTPS_PROXY)." }
}

$target = if ($Dev) { "$root[dev]" } else { "$root" }
& $py -m pip install -e $target
if ($LASTEXITCODE -ne 0) { throw "pip install failed." }

$cfg = Join-Path $root "config\config.yaml"
if (-not (Test-Path $cfg)) {
    Copy-Item (Join-Path $root "config\config.example.yaml") $cfg
    Write-Host "Created config\config.yaml from the example. Edit user_agent_contact (and proxy / CA settings if needed)."
}

Write-Host ""
Write-Host "Done. Next: scripts\run_probe.ps1   (or .venv\Scripts\omi.exe probe)"
