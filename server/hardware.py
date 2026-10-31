"""
hardware.py — Detección de hardware y semáforo de rendimiento de modelos
=========================================================================
Implementa:
  - Detección de hardware (RAM, GPU, CPU, Apple Silicon)
  - Estimación de tokens/segundo por modelo
  - Grado de rendimiento estilo canirun.ai (S/A/B/C/D/F)
  - Cache en disco con refrescar manual
  - Endpoint de readiness del sistema
"""

import json
import logging
import os
import platform
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")


# ============================================================
# Detección de hardware
# ============================================================

def detect_hardware() -> Dict:
    """Detecta hardware del sistema: RAM, GPU, CPU, Apple Silicon."""
    hw = {
        "ram_gb": 0,
        "gpu_name": "No detectada",
        "gpu_vram_gb": 0,
        "cpu_cores": os.cpu_count() or 1,
        "cpu_name": platform.processor() or "Desconocido",
        "is_apple_silicon": False,
        "os": platform.system(),
        "os_version": platform.version(),
        "arch": platform.machine()
    }

    # RAM
    try:
        if platform.system() == "Darwin":
            out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip()
            hw["ram_gb"] = round(int(out) / (1024**3), 1)
        elif platform.system() == "Linux":
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        hw["ram_gb"] = round(kb / (1024**2), 1)
                        break
        elif platform.system() == "Windows":
            out = subprocess.check_output(
                ["wmic", "ComputerSystem", "get", "TotalPhysicalMemory", "/value"],
                text=True
            ).strip()
            for line in out.split("\n"):
                if "TotalPhysicalMemory" in line:
                    val = line.split("=")[1].strip()
                    hw["ram_gb"] = round(int(val) / (1024**3), 1)
    except Exception as e:
        logger.warning(f"Error detectando RAM: {e}")

    # Apple Silicon
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        hw["is_apple_silicon"] = True
        hw["gpu_name"] = "Apple Silicon (Unified Memory)"
        hw["gpu_vram_gb"] = hw["ram_gb"]  # Memoria unificada

    # GPU NVIDIA (Linux/Windows)
    if not hw["is_apple_silicon"]:
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                text=True, timeout=5
            ).strip()
            if out:
                parts = out.split(",")
                hw["gpu_name"] = parts[0].strip()
                hw["gpu_vram_gb"] = round(int(parts[1].strip()) / 1024, 1) if len(parts) > 1 else 0
        except Exception:
            pass

    return hw


# ============================================================
# Estimación de rendimiento por modelo
# ============================================================

# Parámetros estimados por modelo
_MODEL_PARAMS = {
    "llama3.2:1b": 1.0,
    "llama3.2:3b": 3.0,
    "llama3.1:8b": 8.0,
    "llama3:8b": 8.0,
    "qwen3:0.6b": 0.6,
    "qwen3:1.7b": 1.7,
    "qwen3:4b": 4.0,
    "qwen3:8b": 8.0,
    "minicpm-v": 3.0,
    "qwen2.5:7b": 7.0,
    "llava:7b": 7.0,
    "llava:13b": 13.0,
    "moondream": 1.6,
    "phi3:mini": 3.8,
    "mistral:7b": 7.0,
    "gemma:7b": 7.0,
}


def _estimate_params(model: str) -> float:
    """Estima parámetros en billones para un modelo."""
    # Buscar exacto
    if model in _MODEL_PARAMS:
        return _MODEL_PARAMS[model]
    # Buscar por base
    base = model.split(":")[0].lower()
    for key, val in _MODEL_PARAMS.items():
        if key.startswith(base):
            return val
    # Intentar extraer del nombre (ej: "llama3.1:8b" → 8.0)
    m = re.search(r'(\d+\.?\d*)b', model.lower())
    if m:
        return float(m.group(1))
    return 7.0  # Default


def _estimate_tps(hw: Dict, params_b: float) -> float:
    """Estima tokens/segundo basado en hardware y tamaño del modelo."""
    ram = hw.get("ram_gb", 8)
    vram = hw.get("gpu_vram_gb", 0)
    is_apple = hw.get("is_apple_silicon", False)
    cpu_cores = hw.get("cpu_cores", 4)

    # Memoria disponible efectiva para el modelo
    if is_apple:
        available_mem = ram * 0.7  # Apple Silicon usa RAM unificada
        base_tps = 25  # Metal es eficiente
    elif vram > 0:
        available_mem = vram
        base_tps = 30  # GPU NVIDIA
    else:
        available_mem = ram * 0.5  # CPU only
        base_tps = 8  # CPU es más lento

    # El modelo necesita ~1.2GB por cada 1B de parámetros (Q4 quantization)
    model_mem_needed = params_b * 1.2

    if model_mem_needed > available_mem:
        # Modelo no cabe completamente → degradación severa
        ratio = available_mem / model_mem_needed
        tps = base_tps * ratio * 0.3
    else:
        # Modelo cabe → rendimiento proporcional al tamaño
        tps = base_tps * (1.0 / (params_b ** 0.5))

    # Ajuste por cores de CPU
    if not is_apple and vram == 0:
        tps *= min(cpu_cores / 4.0, 2.0)

    return round(max(tps, 0.5), 1)


def _compute_grade(tps: float, params_b: float, ram_gb: float) -> Dict:
    """Calcula grado de rendimiento estilo canirun.ai."""
    # Verificar si el modelo cabe en RAM
    model_mem = params_b * 1.2
    if model_mem > ram_gb * 0.8:
        return {"grade": "F", "color": "#dc2626", "label": "No recomendado", "description": "RAM insuficiente para este modelo"}

    if tps > 30:
        return {"grade": "S", "color": "#22c55e", "label": "Excelente", "description": f"{tps} tok/s — Respuestas instantáneas"}
    elif tps > 20:
        return {"grade": "A", "color": "#4ade80", "label": "Muy bueno", "description": f"{tps} tok/s — Respuestas rápidas"}
    elif tps > 10:
        return {"grade": "B", "color": "#facc15", "label": "Bueno", "description": f"{tps} tok/s — Velocidad aceptable"}
    elif tps > 5:
        return {"grade": "C", "color": "#f59e0b", "label": "Aceptable", "description": f"{tps} tok/s — Algo lento pero funcional"}
    elif tps > 2:
        return {"grade": "D", "color": "#f97316", "label": "Lento", "description": f"{tps} tok/s — Esperas notables"}
    else:
        return {"grade": "F", "color": "#dc2626", "label": "No recomendado", "description": f"{tps} tok/s — Demasiado lento"}


# ============================================================
# API pública
# ============================================================

def get_available_models() -> List[str]:
    """Lista modelos disponibles en Ollama."""
    try:
        import requests
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        if r.status_code == 200:
            return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        pass
    return []


def check_ollama() -> bool:
    """Verifica si Ollama está corriendo."""
    try:
        import requests
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def _attach_profile(result: Dict, data_dir: Path) -> Dict:
    try:
        from hardware_profile import load_active_profile, select_profile, describe_profile, access_for_ram
        hw = result.get("hardware") or {}
        profile = load_active_profile(str(data_dir)) or select_profile(hw.get("ram_gb") or 0)
        result["profile"] = {
            "id": profile.get("id"),
            "label": profile.get("label"),
            "model": profile.get("model"),
            "extra_models": profile.get("extra_models") or profile.get("extraModels") or [],
            "ram_gb": hw.get("ram_gb"),
            "summary": describe_profile(profile, hw.get("ram_gb")),
        }
        result["access"] = access_for_ram(hw.get("ram_gb") or 0)
    except Exception as e:
        logger.debug("No se pudo adjuntar perfil de RAM: %s", e)
    return result


def get_hardware_performance(data_dir: Path, force: bool = False) -> Dict:
    """
    Retorna rendimiento estimado de cada modelo disponible.
    Usa cache en disco para evitar recálculos innecesarios.
    """
    cache_file = data_dir / "hardware_perf_cache.json"

    # Retornar cache si existe y no se fuerza recálculo
    if not force and cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if cached:
                return _attach_profile(cached, data_dir)
        except Exception:
            pass

    hw = detect_hardware()
    models = get_available_models()
    model_results = []

    for model in models:
        params_b = _estimate_params(model)
        tps = _estimate_tps(hw, params_b)
        grade_info = _compute_grade(tps, params_b, hw["ram_gb"])
        model_results.append({
            "model": model,
            "params_b": params_b,
            "tps": tps,
            "grade": grade_info["grade"],
            "color": grade_info["color"],
            "label": grade_info["label"],
            "description": grade_info["description"]
        })

    result = {
        "hardware": hw,
        "models": model_results,
        "total_models": len(models)
    }
    _attach_profile(result, data_dir)

    # Guardar en cache
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Error guardando cache de hardware: {e}")

    return result


def get_readiness(data_dir: Path) -> Dict:
    """
    Verifica si el sistema está listo para usar.
    Retorna estado de Ollama, modelos y sistema general.
    """
    ollama_ok = check_ollama()
    models = get_available_models() if ollama_ok else []

    from hardware_profile import load_active_profile, select_profile, describe_profile, access_for_ram
    from ollama_client import _launcher_model

    hw = detect_hardware()
    profile = load_active_profile(str(data_dir)) or select_profile(hw.get("ram_gb") or 0)
    default_model = _launcher_model() or profile.get("model") or "llama3.2:3b"

    # Verificar si el modelo por defecto está disponible
    model_ready = any(
        m == default_model or m.startswith(default_model.split(":")[0])
        for m in models
    )

    ready = ollama_ok and model_ready

    # Determinar mensaje descriptivo
    if ready:
        message = "Sistema listo para usar"
        status = "ready"
    elif not ollama_ok:
        message = "Ollama no está ejecutándose. Reinicia el plugin."
        status = "ollama_offline"
    elif not model_ready:
        message = f"Modelo '{default_model}' no encontrado. Descargando..."
        status = "model_missing"
    else:
        message = "Preparando el sistema..."
        status = "preparing"

    access = access_for_ram(hw.get("ram_gb") or 0)
    if access.get("level") == "block":
        ready = False
        status = "blocked"
        message = (
            f"Este equipo no cumple el mínimo. Se midieron {float(access.get('ram_gb') or 0):.1f} GB "
            "de RAM y se necesitan al menos 8 GB."
        )

    return {
        "ready": ready,
        "status": status,
        "ollama": ollama_ok,
        "model_ready": model_ready,
        "default_model": default_model,
        "available_models": models,
        "models_count": len(models),
        "message": message,
        "access": access,
        "profile": {
            "id": profile.get("id"),
            "label": profile.get("label"),
            "model": profile.get("model") or default_model,
            "extra_models": profile.get("extra_models") or profile.get("extraModels") or [],
            "ram_gb": hw.get("ram_gb"),
            "summary": describe_profile(profile, hw.get("ram_gb")),
        },
    }
