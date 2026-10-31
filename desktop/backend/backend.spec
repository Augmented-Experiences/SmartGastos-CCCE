# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec — backend FastAPI sidecar for Tauri desktop.
# MUST bundle repo app/ (UI) into _MEIPASS/app for frozen StaticFiles.
#
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(SPECPATH).resolve()
DESKTOP = BACKEND_DIR.parent
ROOT = DESKTOP.parent
SERVER = ROOT / "server"

_pyi = {}
try:
    with open(DESKTOP / "smartsuite.config.json", encoding="utf-8") as _f:
        _pyi = (json.load(_f) or {}).get("pyinstaller", {}) or {}
except Exception:
    _pyi = {}

APP_DIR = ROOT / "app"
if not APP_DIR.is_dir() or not (APP_DIR / "index.html").is_file():
    print(f"FATAL: {APP_DIR}/index.html missing — desktop sidecar cannot serve UI.", file=sys.stderr)
    raise SystemExit(1)

datas = [(str(APP_DIR), "app")]
SPLASH_UI = DESKTOP / "ui"
if SPLASH_UI.is_dir() and (SPLASH_UI / "index.html").is_file():
    datas.append((str(SPLASH_UI), "splash_ui"))
else:
    print(f"WARNING: splash ui missing at {SPLASH_UI}", file=sys.stderr)

_defaults = ROOT / "defaults"
if _defaults.is_dir():
    datas.append((str(_defaults), "defaults"))

_ocr_models = ROOT / "data" / "ocr_models"
if _ocr_models.is_dir() and any(_ocr_models.glob("*.onnx")):
    datas.append((str(_ocr_models), "ocr_models"))
else:
    print(
        "WARNING: data/ocr_models sin ONNX — el sidecar los bajará en el primer OCR",
        file=sys.stderr,
    )

for _d in _pyi.get("extraDatas", []):
    _p = ROOT / _d
    if _p.exists():
        datas.append((str(_p), _d))

hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "database",
    "models",
    "orchestration",
    "pipeline_agent",
    "analytics",
    "analytics.recommender",
    "analytics.exporter",
    "default_categories",
    "security",
    "hardware",
    "hardware_profile",
    "ollama_client",
    "agents",
    "agents.ocr_agent",
    "agents.ocr_quality",
    "agents.rapid_ocr",
    "agents.extractor_agent",
    "agents.classifier_agent",
    "agents.auditor_agent",
    "rapidocr",
    "onnxruntime",
    "cv2",
    "shapely",
    "pyclipper",
    "omegaconf",
]

binaries = []
try:
    from PyInstaller.utils.hooks import collect_all

    for _pkg in ("onnxruntime", "rapidocr", "cv2", "shapely", "pyclipper"):
        try:
            _d, _b, _h = collect_all(_pkg)
            datas += _d
            binaries += _b
            hiddenimports += _h
        except Exception as _exc:
            print(f"WARNING: collect_all({_pkg}): {_exc}", file=sys.stderr)
except Exception as _exc:
    print(f"WARNING: PyInstaller hooks: {_exc}", file=sys.stderr)

a = Analysis(
    [str(SERVER / "app.py")],
    pathex=[str(SERVER)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest", "verify_best_practices"] + list(_pyi.get("excludes", [])),
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
