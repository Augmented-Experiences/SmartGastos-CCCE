"""Perfiles de hardware para elegir modelo Ollama según la RAM."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional

# Tramos alineados con desktop/smartsuite.config.json (max_ram_gb exclusivo: RAM < umbral).
# 8 GB típico (~7.8–8.5) entra en Estándar: llama3.2:3b sin moondream.
TIERS: List[Dict] = [
    {
        "max_ram_gb": 7,
        "id": "liviano",
        "label": "Liviano",
        "model": "llama3.2:1b",
        "extra_models": [],
    },
    {
        "max_ram_gb": 13,
        "id": "estandar",
        "label": "Estándar",
        "model": "llama3.2:3b",
        "extra_models": [],
    },
    {
        "max_ram_gb": 24,
        "id": "completo",
        "label": "Completo",
        "model": "llama3.2:3b",
        "extra_models": ["moondream"],
    },
    {
        "max_ram_gb": 0,
        "id": "maximo",
        "label": "Máximo",
        "model": "llama3.1:8b",
        "extra_models": ["moondream"],
    },
]


def select_profile(ram_gb: float) -> Dict:
    """Elige el perfil cuyo umbral es el primero mayor que la RAM detectada."""
    try:
        gb = float(ram_gb)
    except (TypeError, ValueError):
        gb = 0.0
    for tier in TIERS:
        max_gb = float(tier.get("max_ram_gb") or 0)
        if max_gb > 0 and gb < max_gb:
            return dict(tier)
    return dict(TIERS[-1])


def profile_marker_path(data_dir: Optional[str] = None) -> Path:
    root = Path(data_dir or os.environ.get("DATA_DIR") or "data")
    return root / "ollama" / "active_profile.json"


def load_active_profile(data_dir: Optional[str] = None) -> Optional[Dict]:
    path = profile_marker_path(data_dir)
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("model"):
                return data
    except (OSError, json.JSONDecodeError):
        pass
    return None


BLOCK_BELOW_GB = 7.0
WARN_BELOW_GB = 13.0


def _truthy_env(*names: str) -> bool:
    for name in names:
        raw = (os.environ.get(name) or "").strip().lower()
        if raw in ("1", "true", "yes", "on"):
            return True
    return False


def access_for_ram(ram_gb: float) -> Dict:
    """block < 7 GB, warn 8–12 GB, ok 16 GB+. RAM 0 = fail open."""
    try:
        gb = float(ram_gb)
    except (TypeError, ValueError):
        gb = 0.0
    override = _truthy_env("SMARTSUITE_ALLOW_LOW_RAM", "SMARTGASTOS_ALLOW_LOW_RAM")
    if override or gb <= 0:
        level = "ok"
    elif gb < BLOCK_BELOW_GB:
        level = "block"
    elif gb < WARN_BELOW_GB:
        level = "warn"
    else:
        level = "ok"
    return {
        "level": level,
        "ram_gb": gb,
        "block_below_gb": BLOCK_BELOW_GB,
        "warn_below_gb": WARN_BELOW_GB,
        "override": override,
    }


def describe_profile(profile: Dict, ram_gb: Optional[float] = None) -> str:
    label = profile.get("label") or profile.get("id") or "Auto"
    model = profile.get("model") or ""
    extras = profile.get("extra_models") or profile.get("extraModels") or []
    vision = "RapidOCR + moondream" if extras else "RapidOCR"
    ram = f"{ram_gb:g} GB · " if ram_gb else ""
    return f"{ram}perfil {label} · {model} · {vision}"
