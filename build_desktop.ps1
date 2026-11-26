$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    python -m venv (Join-Path $Root ".venv")
}

& $Python -m pip install --upgrade pip
& $Python -m pip install -r (Join-Path $Root "requirements-desktop.txt")
& $Python -m PyInstaller --noconfirm --clean (Join-Path $Root "VortexAI.spec")

Write-Host "Vortex compilado en: $Root\dist\VortexAI\VortexAI.exe"
