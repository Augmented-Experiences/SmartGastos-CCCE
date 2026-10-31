param(
  [switch]$Installer  # If set, runs 'npm run build' in desktop/ (MSI + setup.exe)
)

# ============================================================
# build-backend.ps1 - Package FastAPI backend (Tauri sidecar)
# with PyInstaller (Windows) into desktop/src-tauri/binaries.
#
# Requires Python 3.10-3.12 (3.12 recommended). On 3.13/3.14 pinned
# deps (numpy 1.26.4) may lack wheels and pip will try to compile.
# ============================================================
$ErrorActionPreference = "Stop"

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Desktop = Split-Path -Parent $Here
$Root = Split-Path -Parent $Desktop
Set-Location $Root

# --- Rust (target triple + later 'npm run build') ---
if (-not (Get-Command rustc -ErrorAction SilentlyContinue)) {
    Write-Host ""
    Write-Host "ERROR: 'rustc' (Rust) not found on PATH." -ForegroundColor Red
    Write-Host "       Rust is required for the sidecar name and Tauri build." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Fix:" -ForegroundColor Cyan
    Write-Host "    winget install -e --id Rustlang.Rustup" -ForegroundColor Cyan
    Write-Host "    # restart PowerShell, then:" -ForegroundColor Cyan
    Write-Host "    rustup default stable-msvc" -ForegroundColor Cyan
    throw "Rust is not installed or not on PATH."
}

# --- Python interpreter ---
$Py = $null
if (Test-Path "venv\Scripts\python.exe") {
    $Py = "venv\Scripts\python.exe"
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    try { & py -3.12 --version *> $null; if ($LASTEXITCODE -eq 0) { $Py = "py -3.12" } } catch {}
    if (-not $Py) { $Py = "py" }
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $Py = "python"
} else {
    throw "Python not found. Install Python 3.12 (https://www.python.org/downloads/)."
}

# --- Supported Python 3.10-3.12 ---
$verRaw = & cmd /c "$Py -c ""import sys;print('%d.%d'%sys.version_info[:2])"""
$parts = $verRaw.Trim().Split('.')
$major = [int]$parts[0]; $minor = [int]$parts[1]
if ($major -ne 3 -or $minor -lt 10 -or $minor -gt 12) {
    Write-Host ""
    Write-Host "ERROR: Python $verRaw is not supported for packaging the backend." -ForegroundColor Red
    Write-Host "       Pinned deps (numpy 1.26.4) only have wheels for 3.10-3.12." -ForegroundColor Yellow
    Write-Host "       On 3.13/3.14 pip may compile from source and fail." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Fix:" -ForegroundColor Cyan
    Write-Host "    winget install -e --id Python.Python.3.12" -ForegroundColor Cyan
    Write-Host "    Remove-Item -Recurse -Force venv" -ForegroundColor Cyan
    Write-Host "    py -3.12 -m venv venv" -ForegroundColor Cyan
    Write-Host "    venv\Scripts\python -m pip install -r requirements.txt pyinstaller" -ForegroundColor Cyan
    Write-Host "    powershell -ExecutionPolicy Bypass -File desktop\scripts\build-backend.ps1" -ForegroundColor Cyan
    throw "Unsupported Python version: $verRaw (use 3.12)."
}
Write-Host "==> Using Python $verRaw ($Py)"

$Req = "requirements.txt"
if (Test-Path "requirements-desktop.txt") { $Req = "requirements-desktop.txt" }
Write-Host "==> Installing build deps from $Req (+ PyInstaller)"
& cmd /c "$Py -m pip install --quiet -r $Req pyinstaller"

Write-Host "==> Ensuring RapidOCR ONNX models in data/ocr_models"
$env:PYTHONPATH = "$Root\server"
$env:DATA_DIR = "$Root\data"
& cmd /c "$Py -c ""from agents.rapid_ocr import _build_engine; _build_engine()"""
if ($LASTEXITCODE -ne 0) {
    Write-Host "WARNING: no se pudieron preparar modelos OCR (el sidecar intentará bajarlos al usar fotos)" -ForegroundColor Yellow
}

Write-Host "==> Packaging backend with PyInstaller"
& cmd /c "$Py -m PyInstaller --clean --noconfirm --distpath desktop/backend/dist --workpath desktop/backend/build desktop/backend/backend.spec"

$Triple = ((rustc -vV | Select-String "host: ") -replace "host: ", "").Trim()
New-Item -ItemType Directory -Force -Path desktop/src-tauri/binaries | Out-Null
Copy-Item "desktop/backend/dist/backend.exe" "desktop/src-tauri/binaries/backend-$Triple.exe" -Force
Write-Host "==> Sidecar ready: desktop/src-tauri/binaries/backend-$Triple.exe"
Write-Host ""
if ($Installer) {
  Write-Host "==> Building Tauri installer (MSI + NSIS setup.exe)..." -ForegroundColor Cyan
  Set-Location $Desktop
  if (-not (Test-Path "node_modules")) {
    Write-Host "==> npm install (desktop)"
    npm install
  }
  if (-not (Test-Path "src-tauri/icons/icon.ico")) {
    Write-Host "==> Generating icons (npm run icon) - first time only"
    npm run icon
  }
  npm run build
  Write-Host ""
  Write-Host "Installers:" -ForegroundColor Green
  Write-Host "  $Desktop\src-tauri\target\release\bundle\msi\*.msi"
  Write-Host "  $Desktop\src-tauri\target\release\bundle\nsis\*-setup.exe"
} else {
  Write-Host "Next step (.msi / *-setup.exe, not just the sidecar):" -ForegroundColor Yellow
  Write-Host "  cd desktop"
  Write-Host "  npm install"
  Write-Host "  npm run icon    # once"
  Write-Host "  npm run build"
  Write-Host ""
  Write-Host "Or one step from repo root:" -ForegroundColor Yellow
  Write-Host "  powershell -ExecutionPolicy Bypass -File desktop\scripts\build-backend.ps1 -Installer"
}
