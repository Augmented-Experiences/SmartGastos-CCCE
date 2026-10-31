#!/usr/bin/env bash
INSTALLER=0
if [[ "${1:-}" == "--installer" ]]; then
  INSTALLER=1
fi

# ============================================================
# build-backend.sh — Empaqueta el backend FastAPI (sidecar Tauri)
# en un binario único (sidecar de Tauri) con PyInstaller y lo
# coloca en desktop/src-tauri/binaries con el sufijo del target.
# ============================================================
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DESKTOP="$(dirname "$HERE")"
ROOT="$(dirname "$DESKTOP")"
cd "$ROOT"

# --- Verificar Rust (se usa para el target triple y luego para 'npm run build') ---
if ! command -v rustc >/dev/null 2>&1; then
  echo "ERROR: no se encontró 'rustc' (Rust) en el PATH." >&2
  echo "       Instálalo con rustup (https://rustup.rs) y reabre la terminal:" >&2
  echo "         curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh" >&2
  echo "       Rust también es necesario para 'npm run build'." >&2
  exit 1
fi

# --- Seleccionar intérprete de Python (preferir 3.12; soportado 3.10–3.12) ---
if [ -n "${PYTHON:-}" ]; then
  PY="$PYTHON"
elif [ -x "venv/bin/python" ]; then
  PY="venv/bin/python"
elif command -v python3.12 >/dev/null 2>&1; then
  PY="python3.12"
else
  PY="python3"
fi

VER="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
MAJOR="${VER%%.*}"; MINOR="${VER##*.}"
if [ "$MAJOR" != "3" ] || [ "$MINOR" -lt 10 ] || [ "$MINOR" -gt 12 ]; then
  echo "ERROR: Python $VER no es compatible para empaquetar el backend." >&2
  echo "       numpy 1.26.4 (pinneado) solo tiene wheels para Python 3.10-3.12;" >&2
  echo "       con 3.13/3.14 pip compila desde fuente y falla." >&2
  echo "       Crea el venv con Python 3.12:  python3.12 -m venv venv" >&2
  exit 1
fi
echo "==> Usando Python $VER ($PY)"

# Usa requirements-desktop.txt (subconjunto liviano, sin deps pesadas opcionales
# como torch/easyocr) si existe; si no, cae a requirements.txt.
REQ="requirements.txt"
[ -f "requirements-desktop.txt" ] && REQ="requirements-desktop.txt"
echo "==> Instalando dependencias de build desde $REQ (+ PyInstaller)"
"$PY" -m pip install --quiet -r "$REQ" pyinstaller

echo "==> Preparing RapidOCR ONNX models in data/ocr_models"
export PYTHONPATH="$ROOT/server"
export DATA_DIR="${DATA_DIR:-$ROOT/data}"
if ! "$PY" -c "from agents.rapid_ocr import _build_engine; _build_engine()"; then
  echo "WARNING: no se pudieron preparar modelos OCR (el sidecar intentará bajarlos al usar fotos)"
fi

echo "==> Empaquetando backend con PyInstaller"
"$PY" -m PyInstaller --clean --noconfirm \
  --distpath desktop/backend/dist --workpath desktop/backend/build \
  desktop/backend/backend.spec

TRIPLE="$(rustc -vV | sed -n 's/host: //p')"
mkdir -p desktop/src-tauri/binaries
cp "desktop/backend/dist/backend" "desktop/src-tauri/binaries/backend-${TRIPLE}"
chmod +x "desktop/src-tauri/binaries/backend-${TRIPLE}"
if [[ "$(uname -s)" == "Darwin" && -z "${APPLE_CERTIFICATE:-}" ]]; then
  codesign --force --sign - "desktop/src-tauri/binaries/backend-${TRIPLE}"
  echo "==> Sidecar ad-hoc (sin certificado Developer ID)"
fi
echo "==> Sidecar listo: desktop/src-tauri/binaries/backend-${TRIPLE}"
echo ""

sign_bundled_sidecar() {
  if [[ "$(uname -s)" != "Darwin" ]]; then
    return
  fi
  if [[ -z "${APPLE_CERTIFICATE:-}" || -z "${APPLE_SIGNING_IDENTITY:-}" || -z "${APPLE_CERTIFICATE_PASSWORD:-}" ]]; then
    return
  fi
  local bin="$ROOT/desktop/src-tauri/binaries/backend-${TRIPLE}"
  if [[ ! -f "$bin" ]]; then
    return
  fi
  echo "==> Firmando sidecar con el certificado de Developer ID"
  local keychain="$HOME/Library/Keychains/smartgastos-sign.keychain-db"
  local pass p12
  pass="$(openssl rand -base64 24)"
  p12="$(mktemp)"
  security delete-keychain "$keychain" >/dev/null 2>&1 || true
  security create-keychain -p "$pass" "$keychain"
  security set-keychain-settings -lut 21600 "$keychain"
  security unlock-keychain -p "$pass" "$keychain"
  security default-keychain -s "$keychain"
  printf '%s' "$APPLE_CERTIFICATE" | base64 --decode > "$p12"
  security import "$p12" -f pkcs12 -P "$APPLE_CERTIFICATE_PASSWORD" -T /usr/bin/codesign -k "$keychain"
  rm -f "$p12"
  security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$pass" "$keychain" >/dev/null
  codesign --force --options runtime --timestamp \
    --entitlements "$ROOT/desktop/src-tauri/entitlements.plist" \
    --sign "$APPLE_SIGNING_IDENTITY" \
    --keychain "$keychain" \
    "$bin"
  security delete-keychain "$keychain" >/dev/null
}

if [[ "$INSTALLER" == "1" ]]; then
  sign_bundled_sidecar
  echo "==> Compilando instalador Tauri…"
  cd desktop
  if [[ ! -d node_modules ]]; then npm install; fi
  if [[ ! -f src-tauri/icons/icon.ico ]]; then npm run icon; fi
  npm run build
  echo "Instaladores en desktop/src-tauri/target/release/bundle/"
else
  echo "Siguiente paso (instalador, no solo el sidecar):"
  echo "  cd desktop && npm install && npm run icon && npm run build"
  echo "O: bash desktop/scripts/build-backend.sh --installer"
fi
