"""
ollama_client.py — Cliente centralizado para Ollama con seguridad integrada
=============================================================================
Capa 3 de defensa: TODA llamada al LLM pasa por este módulo.
Incluye:
  - Sanitización automática de inputs (anti-inyección)
  - Endurecimiento automático de system prompts
  - Fix de encoding UTF-8
  - Timeouts diferenciados por tipo de tarea
  - Descarga automática de modelos faltantes
  - Tracking de tokens y ahorro
"""

import json
import logging
import os
import sys
import threading
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")

# Timeouts diferenciados por tipo de tarea
TIMEOUT_DEFAULT = int(os.environ.get("OLLAMA_TIMEOUT", "300"))
TIMEOUT_CLASSIFICATION = int(os.environ.get("OLLAMA_TIMEOUT_CLASSIFICATION", "120"))
TIMEOUT_EXTRACTION = int(os.environ.get("OLLAMA_TIMEOUT_EXTRACTION", "180"))
TIMEOUT_AUDIT = int(os.environ.get("OLLAMA_TIMEOUT_AUDIT", "180"))
TIMEOUT_CHAT = int(os.environ.get("OLLAMA_TIMEOUT_CHAT", "60"))

# Tracking global de uso (protegido por lock para thread safety)
_stats_lock = threading.Lock()
_STATS_DEFAULT = {
    "total_calls": 0,
    "successful_calls": 0,
    "failed_calls": 0,
    "total_input_tokens": 0,
    "total_output_tokens": 0,
    "total_savings_usd": 0.0,
    "avg_latency_ms": 0,
    "injection_attempts_blocked": 0
}

# Ruta del archivo de persistencia
_DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"))
_STATS_FILE = os.path.join(_DATA_DIR, "usage_stats.json")


def _load_stats_from_disk() -> Dict:
    """Carga estadísticas desde disco si existen."""
    try:
        if os.path.exists(_STATS_FILE):
            with open(_STATS_FILE, 'r') as f:
                saved = json.load(f)
            # Merge con defaults para campos nuevos
            merged = dict(_STATS_DEFAULT)
            merged.update(saved)
            return merged
    except (json.JSONDecodeError, IOError, OSError) as e:
        logger.warning(f"No se pudieron cargar stats desde disco: {e}")
    return dict(_STATS_DEFAULT)


def _save_stats_to_disk():
    """Persiste estadísticas a disco (llamar dentro del lock)."""
    try:
        os.makedirs(os.path.dirname(_STATS_FILE), exist_ok=True)
        with open(_STATS_FILE, 'w') as f:
            json.dump(_usage_stats, f, indent=2)
    except (IOError, OSError) as e:
        logger.warning(f"No se pudieron guardar stats en disco: {e}")


# Inicializar desde disco
_usage_stats = _load_stats_from_disk()

# Estado de descarga de modelos (protegido por lock)
_pull_lock = threading.Lock()
_pull_status: Dict = {}


def get_usage_stats() -> Dict:
    """Retorna estadísticas de uso del LLM (thread-safe)."""
    with _stats_lock:
        return dict(_usage_stats)


_VISION_MARKERS = (
    "moondream",
    "llava",
    "minicpm-v",
    "llama3.2-vision",
    "granite3.2-vision",
    "bakllava",
    "llama3-vision",
)


def _ollama_model_names() -> list:
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        if r.status_code != 200:
            logger.warning("/api/tags HTTP %s", r.status_code)
            return []
        return [m.get("name") for m in r.json().get("models", []) if m.get("name")]
    except Exception as e:
        logger.warning("No se pudo leer %s/api/tags: %s", OLLAMA_URL, e)
        return []


def _model_name_matches(have: str, want: str) -> bool:
    if not have or not want:
        return False
    if have == want:
        return True
    if have.startswith(want + "-") or have.startswith(want + ":"):
        return True
    if want.startswith(have + "-"):
        return True
    return False


def _is_vision_name(name: str) -> bool:
    n = (name or "").lower()
    return any(marker in n for marker in _VISION_MARKERS)


def _launcher_model() -> str:
    env_model = (
        os.environ.get("OLLAMA_MODEL")
        or os.environ.get("OLLAMA_DEFAULT_MODEL")
        or ""
    ).strip()
    if env_model:
        return env_model
    data_dir = os.environ.get("DATA_DIR") or _DATA_DIR
    marker = os.path.join(data_dir, "ollama", "active_model.txt")
    try:
        if os.path.isfile(marker):
            with open(marker, encoding="utf-8") as fh:
                return fh.read().strip()
    except Exception:
        pass
    return ""


def resolve_ollama_model(requested: str) -> str:
    """Usa el modelo pedido si está en /api/tags; si no, el que el launcher ya bajó.

    En el sidecar desktop el perfil de RAM manda: un PC de 8 GB no debe
    terminar en llama3.1:8b solo porque quedó de una instalación anterior.
    """
    names = _ollama_model_names()
    env_model = _launcher_model()
    want = (requested or "").strip()
    if want.lower() in ("perfil", "auto"):
        want = ""
    desktop = os.environ.get("RUN_BY_TAURI") == "1" or bool(getattr(sys, "frozen", False))

    if desktop and env_model and not _is_vision_name(want):
        for n in names:
            if n == env_model or _model_name_matches(n, env_model):
                if want and want != n:
                    logger.info(
                        "Perfil de RAM: usando %s en lugar de %s",
                        n,
                        want,
                    )
                return n
        logger.warning(
            "Perfil de RAM pide %s y aún no está en /api/tags; se usará igual",
            env_model,
        )
        return env_model

    if want:
        for n in names:
            if n == want:
                return n
        for n in names:
            if _model_name_matches(n, want):
                logger.info("Usando modelo %s (pedido %s)", n, want)
                return n

    if env_model:
        for n in names:
            if n == env_model or _model_name_matches(n, env_model):
                if want and want != n:
                    logger.warning(
                        "Modelo de agente %s no esta en /api/tags; usando %s (OLLAMA_MODEL)",
                        want,
                        n,
                    )
                return n
        if not names:
            logger.warning("/api/tags vacio; intentando modelo del launcher %s", env_model)
            return env_model

    if names:
        logger.warning(
            "Modelo de agente %s no esta; usando %s (presente en /api/tags)",
            want or "(vacio)",
            names[0],
        )
        return names[0]

    return want or env_model or "llama3.2:3b"


def resolve_ollama_vision_model(requested: str) -> str:
    """Elige un modelo de visión presente en /api/tags (moondream). Nunca un LLM de texto."""
    names = _ollama_model_names()
    vision_names = [n for n in names if _is_vision_name(n)]
    want = (requested or "").strip() or "moondream"

    if want:
        for n in vision_names:
            if n == want or _model_name_matches(n, want):
                return n

    for n in vision_names:
        if "moondream" in n.lower():
            if want and want != n:
                logger.warning(
                    "Modelo de vision %s no esta en /api/tags; usando %s",
                    want,
                    n,
                )
            return n

    if vision_names:
        logger.warning(
            "Modelo de vision %s no esta; usando %s (presente en /api/tags)",
            want,
            vision_names[0],
        )
        return vision_names[0]

    logger.warning(
        "No hay modelo de vision en /api/tags (esperado moondream); pedido %s",
        want,
    )
    return want


def call_ollama(
    model: str,
    system_prompt: str,
    user_message: str,
    temperature: float = 0.7,
    timeout: int = None,
    max_tokens: int = 1024,
    images: list = None,
    skip_sanitization: bool = False
) -> str:
    """
    Llamada centralizada a Ollama con seguridad integrada.
    
    Capa 3: Aplica sanitización de input y endurecimiento de prompt
    automáticamente en TODA llamada al LLM.
    
    Args:
        model: Nombre del modelo Ollama
        system_prompt: Prompt del sistema
        user_message: Mensaje del usuario (se sanitiza automáticamente)
        temperature: Temperatura de generación
        timeout: Timeout en segundos (None = default)
        max_tokens: Máximo de tokens a generar
        images: Lista de imágenes en base64 (para modelos multimodales)
        skip_sanitization: Solo para inputs internos del pipeline (no del usuario)
    
    Returns:
        Respuesta limpia del LLM
    """
    import time
    from security import (
        sanitize_user_input, harden_system_prompt,
        fix_encoding, sanitize_llm_response,
        detect_injection_attempt, estimate_savings
    )

    if timeout is None:
        timeout = TIMEOUT_DEFAULT

    if images:
        model = resolve_ollama_vision_model(model)
    else:
        model = resolve_ollama_model(model)
    logger.info("call_ollama model=%s url=%s", model, OLLAMA_URL)

    # Capa 2: Sanitizar input del usuario
    if not skip_sanitization:
        if detect_injection_attempt(user_message):
            with _stats_lock:
                _usage_stats["injection_attempts_blocked"] += 1
                _save_stats_to_disk()
            logger.warning(f"Intento de inyección detectado y neutralizado")
        user_message = sanitize_user_input(user_message)

    # Capa 1: Endurecer system prompt
    hardened_prompt = harden_system_prompt(system_prompt)

    # Construir payload
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": hardened_prompt},
            {"role": "user", "content": user_message}
        ],
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens
        },
        "stream": False
    }

    if images:
        payload["messages"][-1]["images"] = images

    start_time = time.time()
    with _stats_lock:
        _usage_stats["total_calls"] += 1
        _save_stats_to_disk()

    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json=payload,
            timeout=timeout
        )
        resp.encoding = "utf-8"  # Forzar UTF-8 (evita latin-1 en Windows)

        if resp.status_code == 404:
            # Modelo no encontrado — iniciar descarga automática
            logger.warning(
                "Ollama /api/chat 404 model=%s tags=%s",
                model,
                _ollama_model_names(),
            )
            _start_pull_background(model)
            with _stats_lock:
                _usage_stats["failed_calls"] += 1
                _save_stats_to_disk()
            raise Exception(f"Modelo {model} no disponible. Descarga iniciada automáticamente.")

        resp.raise_for_status()
        content = resp.json()["message"]["content"]

        # Sanitizar respuesta
        content = sanitize_llm_response(content)
        content = fix_encoding(content)

        # Tracking (thread-safe)
        elapsed_ms = int((time.time() - start_time) * 1000)
        with _stats_lock:
            _usage_stats["successful_calls"] += 1
            savings = estimate_savings(user_message, content)
            _usage_stats["total_input_tokens"] += savings["input_tokens"]
            _usage_stats["total_output_tokens"] += savings["output_tokens"]
            _usage_stats["total_savings_usd"] += savings["savings_usd"]

            # Promedio móvil de latencia
            n = _usage_stats["successful_calls"]
            _usage_stats["avg_latency_ms"] = int(
                (_usage_stats["avg_latency_ms"] * (n - 1) + elapsed_ms) / n
            )
            _save_stats_to_disk()

        return content

    except Exception as e:
        with _stats_lock:
            _usage_stats["failed_calls"] += 1
            _save_stats_to_disk()
        logger.error(f"Error en call_ollama: {e}")
        raise


def call_ollama_generate(
    model: str,
    prompt: str,
    system: str = None,
    temperature: float = 0.1,
    timeout: int = None,
    max_tokens: int = 1000,
    images: list = None,
    think: bool = False,
    skip_sanitization: bool = False
) -> Dict:
    """
    Llamada a /api/generate con seguridad integrada.
    Retorna dict con {response, thinking, raw}.
    """
    import time
    from security import (
        sanitize_user_input, harden_system_prompt,
        fix_encoding, sanitize_llm_response,
        detect_injection_attempt
    )

    if timeout is None:
        timeout = TIMEOUT_DEFAULT

    if images:
        model = resolve_ollama_vision_model(model)
    else:
        model = resolve_ollama_model(model)
    logger.info("call_ollama_generate model=%s url=%s", model, OLLAMA_URL)

    # Sanitizar
    if not skip_sanitization:
        if detect_injection_attempt(prompt):
            with _stats_lock:
                _usage_stats["injection_attempts_blocked"] += 1
                _save_stats_to_disk()
            logger.warning(f"Intento de inyección detectado en generate")
        prompt = sanitize_user_input(prompt)

    if system:
        system = harden_system_prompt(system)

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
            "top_p": 0.9
        }
    }

    if system:
        payload["system"] = system
    if images:
        payload["images"] = images
    if think:
        payload["think"] = True

    with _stats_lock:
        _usage_stats["total_calls"] += 1
        _save_stats_to_disk()
    start_time = time.time()

    try:
        resp = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=timeout)
        resp.encoding = "utf-8"

        if resp.status_code == 404:
            logger.warning(
                "Ollama /api/generate 404 model=%s tags=%s",
                model,
                _ollama_model_names(),
            )
            _start_pull_background(model)
            with _stats_lock:
                _usage_stats["failed_calls"] += 1
                _save_stats_to_disk()
            raise Exception(f"Modelo {model} no disponible. Descarga iniciada.")

        resp.raise_for_status()
        data = resp.json()

        raw_response = data.get("response", "").strip()
        thinking_text = data.get("thinking", "")
        clean_response = sanitize_llm_response(raw_response)
        clean_response = fix_encoding(clean_response)

        with _stats_lock:
            _usage_stats["successful_calls"] += 1
            _save_stats_to_disk()
        return {
            "ok": True,
            "response": clean_response,
            "thinking": thinking_text,
            "raw": raw_response
        }

    except Exception as e:
        with _stats_lock:
            _usage_stats["failed_calls"] += 1
            _save_stats_to_disk()
        logger.error(f"Error en call_ollama_generate: {e}")
        return {
            "ok": False,
            "error": str(e),
            "response": "",
            "thinking": "",
            "raw": ""
        }


# ============================================================
# Descarga automática de modelos
# ============================================================

def is_model_available(model: str) -> bool:
    """Verifica si un modelo está disponible en Ollama."""
    try:
        models = _ollama_model_names()
        return model in models or any(
            _model_name_matches(m, model) or m.startswith(model.split(":")[0])
            for m in models
        )
    except Exception:
        return False


def _start_pull_background(model: str):
    """Inicia descarga de modelo en background (thread-safe)."""
    with _pull_lock:
        if model in _pull_status and _pull_status[model].get("status") in ("pulling", "queued"):
            return
        _pull_status[model] = {"status": "queued", "progress": 0, "error": None}
    threading.Thread(target=_do_pull, args=(model,), daemon=True).start()


def _do_pull(model: str):
    """Descarga un modelo de Ollama en background (thread-safe)."""
    with _pull_lock:
        _pull_status[model]["status"] = "pulling"
    try:
        with requests.post(
            f"{OLLAMA_URL}/api/pull",
            json={"name": model, "stream": True},
            stream=True, timeout=3600
        ) as r:
            for line in r.iter_lines():
                if line:
                    data = json.loads(line)
                    if "completed" in data and "total" in data and data["total"] > 0:
                        with _pull_lock:
                            _pull_status[model]["progress"] = int(data["completed"] / data["total"] * 100)
        with _pull_lock:
            _pull_status[model] = {"status": "done", "progress": 100, "error": None}
    except Exception as e:
        with _pull_lock:
            _pull_status[model] = {"status": "error", "progress": 0, "error": str(e)}


def get_pull_status(model: str = None) -> Dict:
    """Retorna estado de descarga de modelos (thread-safe)."""
    with _pull_lock:
        if model:
            return _pull_status.get(model, {"status": "unknown", "progress": 0, "error": None})
        return dict(_pull_status)
