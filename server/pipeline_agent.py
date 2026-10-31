"""
pipeline_agent.py — Pipeline Agéntico de Procesamiento de Documentos
======================================================================
Arquitectura de 6 pasos con streaming SSE y contexto acumulado:

  Paso 1: OCR         — Tesseract multi-PSM + preprocesamiento de imagen
                        → Produce: texto OCR bruto

  Paso 2: Visión IA   — Modelo de visión (moondream/llava con imagen en base64)
                        → Recibe: IMAGEN + prompt + texto OCR del paso 1
                        → Produce: campos visuales + texto adicional

  Paso 3: Extractor   — Modelo de texto + regex
                        → Recibe: IMAGEN + prompt + texto OCR + campos visión
                        → Produce: campos estructurados JSON

  Paso 4: Clasificador — Modelo de texto
                        → Recibe: todos los campos extraídos + texto combinado
                        → Produce: categoría contable + confianza

  Paso 5: Auditor     — Reglas + modelo de texto
                        → Recibe: todos los campos + clasificación + imagen
                        → Produce: alertas, duplicados, anomalías

  Paso 6: Guardado    — Persiste en SQLite

Principio de contexto acumulado:
  - Cada agente recibe la imagen (si es imagen) + su system prompt + 
    TODA la información recuperada por los pasos anteriores
  - Los agentes finales (Clasificador, Auditor) consolidan la verdad
    a partir de todas las salidas anteriores

Streaming via SSE: cada paso emite eventos JSON al cliente en tiempo real.
Compatible con macOS, Windows y Linux.
"""

import base64
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Generator, List, Optional, Tuple

from agents.ocr_agent import get_ocr_agent, render_pdf_page

logger = logging.getLogger(__name__)
from security import sanitize_log

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")

## ── Modelos soportados por agente ──────────────────────────────────────────────────────
# NOTA: qwen3 es solo texto. Para visión usar moondream/llava/minicpm-v.
# IMPORTANTE: El agente visual DEBE usar un modelo con soporte de imagen (multimodal).
# qwen3:0.6b NO tiene capacidad visual — enviar imágenes a él falla silenciosamente en Windows.
QWEN_MODEL = "qwen3:0.6b"        # Agente de texto pequeño (extracción, clasificación)
LLAMA_MODEL = "llama3.2:3b"      # Agentes de razonamiento (Clasificador+Auditor+Recomendador)
VISION_DEFAULT_MODEL = "moondream"  # Default para agente visual (DEBE ser multimodal)

# Listas de preferencia por tipo de tarea
# Para visión: moondream es el más ligero con soporte de imagen
VISION_MODELS = ["moondream", "llava:7b", "minicpm-v", "llava:13b"]  # Modelos con soporte de imagen
TEXT_MODELS   = ["qwen3:0.6b", "qwen3:1.7b", "llama3.2:3b", "llama3:8b"]  # Fallback genérico
LLAMA_TEXT_MODELS = ["llama3.1:8b", "llama3.1", "llama3.2:3b", "llama3:8b", "qwen3:0.6b"]  # Preferencia llama; RAM-tier primero
DESKTOP_EXTRACTOR_MODELS = [
    "llama3.1:8b",
    "llama3.1",
    "llama3.2:3b",
    "llama3:8b",
    "llama3.2:3b",
] + TEXT_MODELS

_BBOX_VALUE_RE = re.compile(r"^\s*\[[\s\d\.,]+\]\s*$")


def _desktop_sidecar() -> bool:
    import sys

    return os.environ.get("RUN_BY_TAURI") == "1" or bool(getattr(sys, "frozen", False))


def _vision_raw_is_noise(text: str) -> bool:
    if not text:
        return True
    s = text.strip()
    if _BBOX_VALUE_RE.match(s):
        return True
    if len(s) < 40 and re.search(r"\b0\.\d+\b", s) and "[" in s:
        return True
    return False


def _filter_vision_fields(data: Dict) -> Dict:
    """Quita bbox/coordenadas; conserva campos contables."""
    if not data:
        return {}
    out: Dict = {}
    for k, v in data.items():
        kl = str(k).lower()
        if any(x in kl for x in ("bbox", "bounding", "coordinate", "x_min", "y_min", "x_max", "y_max")):
            continue
        if isinstance(v, list) and len(v) == 4 and all(isinstance(x, (int, float)) for x in v):
            continue
        if isinstance(v, str) and _vision_raw_is_noise(v):
            continue
        if v is None or v in ("", "null", "None"):
            continue
        out[k] = v
    return out


# ── Utilidades Ollama ─────────────────────────────────────────────────────────────
def _ollama_list() -> List[str]:
    """Lista modelos disponibles en Ollama."""
    try:
        import requests
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        if r.status_code == 200:
            return [m["name"] for m in r.json().get("models", [])]
    except Exception as e:
        logger.debug(f"No se pudo obtener modelos de Ollama: {e}")
    return []


_VISION_MODEL_MARKERS = (
    "moondream",
    "llava",
    "minicpm-v",
    "llama3.2-vision",
    "granite3.2-vision",
    "bakllava",
    "llama3-vision",
)


def _model_supports_vision(model: Optional[str]) -> bool:
    if not model:
        return False
    m = model.lower()
    return any(marker in m for marker in _VISION_MODEL_MARKERS)


def _resolve_vision_model(
    preferred: Optional[str],
    detected: Optional[str],
    available: Optional[List[str]],
) -> Optional[str]:
    """Elige un modelo Ollama que acepte imágenes (p. ej. moondream)."""
    candidates: List[str] = []
    for c in (preferred, detected):
        if c:
            candidates.append(c)
    candidates.extend(VISION_MODELS)
    if available:
        found = _find_best_model(candidates, available)
        if found and _model_supports_vision(found):
            return found
    for c in candidates:
        if c and _model_supports_vision(c):
            if not available:
                return c
            resolved = _find_best_model([c], available)
            if resolved:
                return resolved
    return None


def _find_best_model(preferred_list: List[str], available: List[str]) -> Optional[str]:
    """Encuentra el mejor modelo disponible de la lista de preferidos."""
    available_lower = {m.lower(): m for m in available}
    available_bases: Dict[str, str] = {}
    for m in available:
        base = m.split(":")[0].lower()
        if base not in available_bases:
            available_bases[base] = m

    for preferred in preferred_list:
        p_lower = preferred.lower()
        p_base = p_lower.split(":")[0]
        if p_lower in available_lower:
            return available_lower[p_lower]
        if p_base in available_bases:
            return available_bases[p_base]
    return None


def _extract_fields_from_thinking(thinking_text: str) -> Dict:
    """
    Extrae campos clave del texto del thinking cuando el JSON está truncado o ausente.
    Busca patrones como: `campo`: valor, **campo:** valor, "campo": valor
    Retorna un dict con los campos encontrados.
    """
    fields = {}
    if not thinking_text:
        return fields

    # Patrones para extraer valores mencionados en el thinking
    field_patterns = {
        "monto_total": [
            r'`?monto_total`?[:\s]+([\d,\.]+)',
            r'\*\*?[Tt]otal[:\s]+\*\*?\$?\s*([\d,\.]+)',
            r'TOTAL[\s:]+\$?\s*([\d,\.]{3,})',
            r'total.*?([\d,\.]{4,})',
        ],
        "monto_neto": [
            r'`?monto_neto`?[:\s]+([\d,\.]+)',
            r'[Nn]eto[:\s]+\$?\s*([\d,\.]+)',
            r'P\.\s*UNITARIO[:\s]+\$?\s*([\d,\.]+)',
        ],
        "iva": [
            r'`?iva`?[:\s]+([\d,\.]+)',
            r'IGV[:\s]+\$?\s*([\d,\.]+)',
        ],
        "proveedor": [
            r'`?proveedor`?[:\s]+["\']?([A-Z][\w\s\.]+)["\']?',
            r'[Ee]misor[:\s]+["\']?([A-Z][\w\s\.]+)["\']?',
        ],
        "rut_proveedor": [
            r'`?rut_proveedor`?[:\s]+["\']?([\d\.\-kK]{8,})["\']?',
            r'R\.?U\.?[CT]\.?[:\s]+([\d\.\-kK]{8,})',
            r'([\d]{8,11})\s*\(R\.?U\.?[CT]',
        ],
        "fecha_emision": [
            r'`?fecha_emision`?[:\s]+["\']?(\d{4}-\d{2}-\d{2})["\']?',
            r'(\d{4}-\d{2}-\d{2})',
        ],
        "folio": [
            r'`?folio`?[:\s]+["\']?([\d]{3,8})["\']?',
            r'N[\u00b0o]\s+([\d]{4,8})',
        ],
        "tipo_documento": [
            r'`?tipo_documento`?[:\s]+["\']?([A-Z\s]+)["\']?',
            r'(BOLETA DE VENTA|FACTURA|BOLETA|RECIBO)',
        ],
        "moneda": [
            r'`?moneda`?[:\s]+["\']?(CLP|PEN|MXN|USD|ARS)["\']?',
        ],
        "descripcion": [
            r'`?descripcion`?[:\s]+["\']?([\w\s]+)["\']?',
            r'[Dd]escripci[oó]n[:\s]+["\']?([\w\s]+)["\']?',
        ],
    }

    def parse_amount(raw: str) -> Optional[float]:
        if not raw:
            return None
        cleaned = raw.strip()
        if re.search(r'\d\.\d{3}', cleaned):
            cleaned = cleaned.replace('.', '').replace(',', '.')
        elif re.search(r'\d,\d{3}', cleaned):
            cleaned = cleaned.replace(',', '')
        elif ',' in cleaned and '.' not in cleaned:
            cleaned = cleaned.replace(',', '.')
        else:
            cleaned = cleaned.replace(',', '')
        try:
            val = float(cleaned)
            return val if math.isfinite(val) and val > 0 else None
        except (ValueError, TypeError):
            return None

    amount_fields = {'monto_total', 'monto_neto', 'iva'}
    for field, patterns in field_patterns.items():
        for pat in patterns:
            m = re.search(pat, thinking_text, re.IGNORECASE | re.MULTILINE)
            if m:
                val = m.group(1).strip()
                if field in amount_fields:
                    num = parse_amount(val)
                    if num and num > 0:
                        fields[field] = num
                        break
                else:
                    if val and val not in ('null', 'None', ''):
                        fields[field] = val
                        break

    return fields


def _ollama_generate(model: str, prompt: str, image_b64: Optional[str] = None,
                     system: Optional[str] = None,
                     timeout: int = 120,
                     temperature: float = 0.1,
                     max_tokens: int = 1000,
                     enable_thinking: bool = False) -> tuple:
    """
    Llama a Ollama y retorna (respuesta_limpia, prompt_enviado, respuesta_raw).
    Soporta imágenes en base64 para modelos multimodales.
    Para qwen3: activa thinking nativo con think=True en options.
    """
    try:
        import requests
        from ollama_client import resolve_ollama_model, resolve_ollama_vision_model
        if image_b64:
            model = resolve_ollama_vision_model(model)
        else:
            model = resolve_ollama_model(model)
        # Construir el prompt final (sin prefijo /think - usamos la opción nativa)
        final_prompt = prompt
        if enable_thinking and prompt.startswith("/think\n"):
            final_prompt = prompt[len("/think\n"):]

        payload: Dict = {
            "model": model,
            "prompt": final_prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                "top_p": 0.9
            }
        }
        # Activar thinking nativo para qwen3 (Ollama >= 0.7)
        if enable_thinking and "qwen" in model.lower():
            payload["think"] = True

        if system:
            payload["system"] = system
        if image_b64:
            if _model_supports_vision(model):
                payload["images"] = [image_b64]
            else:
                logger.warning(
                    "Omitiendo imagen: el modelo %s no es multimodal (use moondream)",
                    model,
                )

        r = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=timeout)
        if r.status_code == 200:
            resp_json = r.json()
            raw_response = resp_json.get("response", "").strip()
            # Extraer thinking si está disponible como campo separado (Ollama >= 0.7 con think=true)
            thinking_text = resp_json.get("thinking", "")
            # Limpiar tags <think>...</think> de la respuesta final
            clean_response = re.sub(r'<think>[\s\S]*?</think>', '', raw_response, flags=re.IGNORECASE).strip()

            # — Fallback: si clean_response no contiene JSON válido pero thinking sí,
            #   extraer el JSON del thinking (qwen3 a veces pone el JSON solo en thinking
            #   cuando el response se trunca antes de llegar al JSON final)
            if enable_thinking and thinking_text:
                # Verificar si clean_response tiene JSON válido
                has_json_in_response = bool(
                    re.search(r'\{[\s\S]*\}', clean_response) or
                    re.search(r'```json', clean_response, re.IGNORECASE)
                )
                if not has_json_in_response:
                    # Intentar extraer JSON completo del thinking
                    json_in_thinking = re.search(
                        r'```json\s*([\s\S]*?)\s*```|({\s*"[^"]+"[\s\S]*?})',
                        thinking_text, re.DOTALL
                    )
                    if json_in_thinking:
                        extracted = json_in_thinking.group(1) or json_in_thinking.group(2)
                        if extracted:
                            logger.debug("Usando JSON extraído del thinking como respuesta")
                            clean_response = extracted.strip()
                    else:
                        # Último recurso: construir JSON desde campos mencionados en el thinking
                        # usando regex sobre el texto del thinking
                        reconstructed = _extract_fields_from_thinking(thinking_text)
                        if reconstructed:
                            logger.debug("JSON reconstruido desde campos del thinking")
                            clean_response = json.dumps(reconstructed, ensure_ascii=False)

            return clean_response, final_prompt, raw_response, thinking_text
        logger.warning(f"Ollama HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.debug(f"Ollama error: {e}")
    return "", prompt, "", ""


def _image_to_base64(img_path: str, max_size: int = 1600) -> Optional[str]:
    """
    Convierte una imagen a base64 para enviar a modelos de visión.
    Redimensiona si es necesario para no sobrecargar el modelo.
    """
    try:
        from PIL import Image
        from security import open_receipt_image
        import io as _io
        img = open_receipt_image(img_path)
        if max(img.size) > max_size:
            img.thumbnail((max_size, max_size), Image.LANCZOS)
        buf = _io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        logger.debug(f"image_to_base64 error: {e}")
        return None


# ── OCR con Tesseract ─────────────────────────────────────────────────────────

def _preprocess_image(img_path: str) -> List[str]:
    """Genera variantes preprocesadas de la imagen para mejorar OCR."""
    variants = [img_path]
    try:
        from PIL import Image, ImageFilter, ImageEnhance
        from security import open_receipt_image
        img = open_receipt_image(img_path)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        w, h = img.size
        scale = max(1.5, 1200 / max(w, 1))
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

        gray = img.convert("L")

        # Variante 1: contraste alto + sharpen doble + threshold 180
        v1 = ImageEnhance.Contrast(gray).enhance(3.0)
        v1 = v1.filter(ImageFilter.SHARPEN)
        v1 = v1.filter(ImageFilter.SHARPEN)
        v1 = v1.point(lambda x: 0 if x < 180 else 255, "1").convert("L")
        tmp1 = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        v1.save(tmp1.name, "PNG", dpi=(300, 300))
        tmp1.close()
        variants.append(tmp1.name)

        # Variante 2: contraste moderado sin binarización
        v2 = ImageEnhance.Contrast(gray).enhance(2.0)
        v2 = v2.filter(ImageFilter.SHARPEN)
        tmp2 = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        v2.save(tmp2.name, "PNG", dpi=(300, 300))
        tmp2.close()
        variants.append(tmp2.name)

        # Variante 3: binarización suave (threshold 140)
        v3 = gray.point(lambda x: 0 if x < 140 else 255, "1").convert("L")
        tmp3 = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        v3.save(tmp3.name, "PNG", dpi=(300, 300))
        tmp3.close()
        variants.append(tmp3.name)

    except Exception as e:
        logger.debug(f"Preprocesamiento: {e}")
    return variants


def _run_tesseract(img_path: str) -> str:
    """Corre Tesseract con múltiples PSM y retorna el mejor resultado."""
    best, best_words = "", 0
    for psm in [6, 11, 4, 3]:
        try:
            r = subprocess.run(
                ["tesseract", img_path, "stdout", "-l", "spa+eng",
                 "--psm", str(psm), "--oem", "3"],
                capture_output=True, text=True, timeout=30
            )
            if r.returncode == 0:
                text = r.stdout.strip()
                words = len([w for w in text.split() if len(w) > 2])
                if words > best_words:
                    best_words = words
                    best = text
                if best_words > 60:
                    break
        except Exception as e:
            logger.debug(f"Excepción controlada: {e}")
    return best


def _ocr_quality_score(text: str) -> float:
    """Calcula un puntaje de calidad del texto OCR."""
    if not text:
        return 0.0
    words = text.split()
    if not words:
        return 0.0
    real_words = [w for w in words if len(w) > 2 and re.match(r'[A-Za-z0-9áéíóúÁÉÍÓÚñÑ]', w)]
    noise_tokens = [w for w in words if re.match(r'^[\\|/\-_=~<>\^]+$', w) or (len(w) == 1 and not w.isdigit())]
    noise_ratio = len(noise_tokens) / max(len(words), 1)
    score = len(real_words) * (1.0 - noise_ratio)
    return score


def ocr_image(img_path: str) -> Dict:
    """OCR completo sobre una imagen. Retorna {text, words, method}."""
    variants = _preprocess_image(img_path)
    all_texts = []
    best_text, best_score = "", 0.0
    best_words = 0
    method = "none"

    for v in variants:
        text = _run_tesseract(v)
        if text.strip():
            all_texts.append(text)
            score = _ocr_quality_score(text)
            words = len([w for w in text.split() if len(w) > 2])
            if score > best_score:
                best_score = score
                best_words = words
                best_text = text
                method = "tesseract"

    # Combinar textos de todas las variantes para maximizar cobertura
    if len(all_texts) > 1:
        combined = "\n\n".join(all_texts)
        combined_score = _ocr_quality_score(combined)
        if combined_score > best_score * 1.2:
            best_text = combined
            best_words = len([w for w in combined.split() if len(w) > 2])

    # Limpiar temporales
    for v in variants[1:]:
        try:
            os.unlink(v)
        except Exception as e:
            logger.debug(f"Excepción controlada: {e}")

    return {"text": best_text, "words": best_words, "method": method}


def ocr_pdf(pdf_path: str) -> Dict:
    """OCR para PDFs: texto digital primero, luego imagen."""
    # Texto digital
    try:
        from security import read_pdf_text, PdfRejected, sanitize_log
        text = read_pdf_text(pdf_path)
        words = len([w for w in text.split() if len(w) > 2])
        if words >= 15:
            return {"text": text, "words": words, "method": "pdf_digital"}
    except PdfRejected as exc:
        logger.info("PDF rechazado: %s", sanitize_log(exc))
        return {"text": "", "words": 0, "method": "pdf_rejected", "error": str(exc)}
    except Exception as e:
        logger.debug("PDF digital: %s", sanitize_log(e))

    # PDF escaneado → imagen → OCR
    try:
        from pdf2image import convert_from_path
        images = convert_from_path(pdf_path, dpi=200, first_page=1, last_page=2)
        all_text = []
        for img in images:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                img.save(tmp.name, "PNG")
                tmp_name = tmp.name
            result = ocr_image(tmp_name)
            if result["text"]:
                all_text.append(result["text"])
            try:
                os.unlink(tmp_name)
            except Exception as e:
                logger.debug(f"Excepción controlada: {e}")
        text = "\n".join(all_text)
        return {"text": text, "words": len([w for w in text.split() if len(w) > 2]),
                "method": "pdf_scanned"}
    except Exception as e:
        logger.debug(f"PDF OCR: {e}")

    return {"text": "", "words": 0, "method": "failed"}


# ── Extracción de campos por Regex ────────────────────────────────────────────

def _extract_by_regex(text: str) -> Dict:
    """
    Extracción robusta de campos contables por regex.
    Diseñado para facturas electrónicas y documentos equivalentes de Colombia (DIAN).
    """
    fields: Dict = {}
    if not text:
        return fields

    # ── NIT colombiano ──
    nit = re.search(r'N\.?I\.?T\.?\s*[:\s]?\s*(\d{3}\.?\d{3}\.?\d{3}-?\d)', text, re.I)
    if not nit:
        nit = re.search(r'(?:nit)\s*[:\s]?\s*(\d{6,10}-?\d)', text, re.I)
    if nit:
        fields["rut_proveedor"] = nit.group(1)

    # ── RUT chileno (documentos importados) ──
    if "rut_proveedor" not in fields:
        rut = re.search(r'R\.?U\.?T\.?\s*[:\s]?\s*(\d{1,2}[.\d]*\d-[\dkK])', text, re.I)
        if rut:
            fields["rut_proveedor"] = rut.group(1).replace(".", "")

    # ── RUC peruano ──
    if "rut_proveedor" not in fields:
        ruc = re.search(r'R\.?U\.?C\.?\s*[:\s]?\s*(\d{8,11})', text, re.I)
        if ruc:
            fields["rut_proveedor"] = ruc.group(1)

    # ── RFC mexicano ──
    if "rut_proveedor" not in fields:
        rfc = re.search(r'RFC\s*[:\s]?\s*([A-Z]{3,4}\d{6}[A-Z0-9]{3})', text, re.I)
        if rfc:
            fields["rut_proveedor"] = rfc.group(1)

    # ── Tipo de documento ──
    tipo_patterns = [
        (r'DOCUMENTO\s+EQUIVALENTE', 'DOCUMENTO EQUIVALENTE'),
        (r'DOCUMENTO\s+SOPORTE', 'DOCUMENTO SOPORTE'),
        (r'CUENTA\s+DE\s+COBRO', 'CUENTA DE COBRO'),
        (r'BOLETA\s+ELECTR[ÓO]NICA', 'BOLETA ELECTRÓNICA'),
        (r'BOLETA\s+DE\s+VENTA', 'BOLETA DE VENTA'),
        (r'BOLETA', 'BOLETA'),
        (r'FACTURA\s+ELECTR[ÓO]NICA', 'FACTURA ELECTRÓNICA'),
        (r'FACTURA', 'FACTURA'),
        (r'NOTA\s+DE\s+CR[ÉE]DITO', 'NOTA DE CRÉDITO'),
        (r'RECIBO\s+DE\s+HONORARIOS', 'RECIBO DE HONORARIOS'),
        (r'RECIBO', 'RECIBO'),
        (r'TICKET', 'TICKET'),
        (r'COMPROBANTE', 'COMPROBANTE'),
    ]
    for pat, tipo in tipo_patterns:
        if re.search(pat, text, re.I):
            fields["tipo_documento"] = tipo
            break

    # ── Folio / Número de documento ──
    folio_patterns = [
        r'(?:BOLETA|FACTURA)\s+(?:ELECTR[ÓO]NICA|DE\s+VENTA)\s*[\n\r]+\s*(\d{1,8})\s*[\n\r]',
        r'N[°o]\s*[:\s]?\s*(\d{4,8})',
        r'N[°o]\s+(\d{3,8})',
        r'001[-\s]+N[°o]?\s+(\d{4,8})',
        r'(?:FOLIO|CONSECUTIVO|N[UÚ]MERO\s+DE\s+FACTURA)\s*[:\s]?\s*(\d{3,8})',
        r'CUFE\s*[:\s]?\s*([A-Za-z0-9]{8,20})',
        r'SERIE\s+\w+\s+N[°o]?\s*(\d{4,8})',
    ]
    for pat in folio_patterns:
        m = re.search(pat, text, re.I | re.MULTILINE)
        if m:
            fields["folio"] = m.group(1)
            break

    # ── Fecha ──
    date_patterns = [
        r'(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{4})',
        r'(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{2})',
    ]
    for pat in date_patterns:
        m = re.search(pat, text)
        if m:
            d, mo, y = m.group(1), m.group(2), m.group(3)
            if len(y) == 2:
                y = "20" + y
            try:
                day_int = int(d)
                mon_int = int(mo)
                if 1 <= day_int <= 31 and 1 <= mon_int <= 12:
                    fields["fecha_emision"] = f"{y}-{mo.zfill(2)}-{d.zfill(2)}"
                    break
            except Exception as e:
                logger.debug(f"Excepción controlada: {e}")

    # ── Montos ──
    def parse_amount(raw: str) -> Optional[float]:
        if not raw:
            return None
        cleaned = raw.strip()
        if re.search(r'\d\.\d{3}', cleaned):
            cleaned = cleaned.replace('.', '').replace(',', '.')
        elif re.search(r'\d,\d{3}', cleaned):
            cleaned = cleaned.replace(',', '')
        elif ',' in cleaned and '.' not in cleaned:
            cleaned = cleaned.replace(',', '.')
        else:
            cleaned = cleaned.replace(',', '')
        try:
            val = float(cleaned)
            return val if math.isfinite(val) and val > 0 else None
        except (ValueError, TypeError):
            return None

    # Total — múltiples formatos latinoamericanos
    total_patterns = [
        r'TOTAL\s+S/\.?\s*([\d,\.]{3,})',
        r'TOTAL\s+S/\s*([\d,\.]{3,})',
        r'Total\s*[:\s]\s*\$?\s*([\d\.]{3,})',
        r'TOTAL\s*[:\s]\s*\$?\s*([\d\.]{3,})',
        r'IMPORTE\s+TOTAL\s*[:\s]\s*([\d,\.]{3,})',
        r'MONTO\s+TOTAL\s*[:\s]\s*([\d,\.]{3,})',
        r'Total\s+a\s+pagar\s*[:\s]\s*([\d,\.]{3,})',
        r'TOTAL\s+A\s+PAGAR\s*[:\s]\s*([\d,\.]{3,})',
        r'TOTAL\s+VENTA\s*[:\s]\s*([\d,\.]{3,})',
        r'TOTAL\s+FACTURA\s*[:\s]\s*([\d,\.]{3,})',
        r'TOTAL\s+BOLETA\s*[:\s]\s*([\d,\.]{3,})',
        r'TOTAL\s+SI\s*[:\s]?\s*([\d,\.]{3,})',
        r'^\s*Total\s+(\d[\d\.]{2,})\s*$',
        r'^\s*TOTAL\s+(\d[\d\.]{2,})\s*$',
        r'Total[\s\t]+(\d[\d\.]{2,})\s*$',
        r'TOTAL[\s\t]+(\d[\d\.]{2,})\s*$',
        r'Total[:\s]*\$\s*(\d[\d\.]{2,})',
        r'TOTAL[:\s]*\$\s*(\d[\d\.]{2,})',
    ]
    for p in total_patterns:
        m = re.search(p, text, re.I | re.MULTILINE)
        if m:
            val = parse_amount(m.group(1))
            if val and val > 10:
                fields["monto_total"] = val
                break
    # Fallback: número más grande en líneas con 'total'
    if "monto_total" not in fields:
        for line in text.split('\n'):
            if re.search(r'total', line, re.I):
                nums = re.findall(r'[\d][\d\.]{2,}', line)
                for n in nums:
                    val = parse_amount(n)
                    if val and val > 100:
                        fields["monto_total"] = val
                        break
            if "monto_total" in fields:
                break

    # IVA
    iva_patterns = [
        r'(?:^|\n)\s*I\.?V\.?A\.?\s*[:\s]\s*\$?\s*([\d\.]{2,})\s*(?:\n|$)',
        r'(?:^|\n)\s*IGV\s*[:\s]\s*\$?\s*([\d,\.]{2,})\s*(?:\n|$)',
        r'(?:^|\n)\s*Impuesto\s*[:\s]\s*([\d,\.]{2,})\s*(?:\n|$)',
        r'I\.?V\.?A\.?[:\s]+\$?\s*(\d{2,6})\s*$',
    ]
    for p in iva_patterns:
        m = re.search(p, text, re.I | re.MULTILINE)
        if m:
            val = parse_amount(m.group(1))
            if val and val > 1:
                fields["iva"] = val
                break

    # Monto neto
    neto_patterns = [
        r'Monto\s+Neto\s*[:\s]\s*\$?\s*([\d\.]{3,})',
        r'MONTO\s+NETO\s*[:\s]\s*\$?\s*([\d\.]{3,})',
        r'Base\s+Imponible\s*[:\s]\s*([\d,\.]{3,})',
        r'Subtotal\s*[:\s]\s*([\d,\.]{3,})',
    ]
    for p in neto_patterns:
        m = re.search(p, text, re.I)
        if m:
            val = parse_amount(m.group(1))
            if val and val > 10:
                fields["monto_neto"] = val
                break

    # Calcular neto si tenemos total e IVA
    if "monto_total" in fields and "iva" in fields and "monto_neto" not in fields:
        neto = fields["monto_total"] - fields["iva"]
        if neto > 0:
            fields["monto_neto"] = round(neto, 2)

    # ── Proveedor ──
    prov_patterns = [
        r'\d{2}:\d{2}:\d{2}\s*[\n\r]+([A-ZÁÉÍÓÚÑ][A-Za-záéíóúñÁÉÍÓÚÑ\s]{5,60}(?:SPA|S\.A\.|LTDA|SRL|SAC|EIRL|SAS)?)',
        r'^(Productos\s+"[^"]+"|Productos\s+[A-Za-z]+)',
        r'De:\s*([^\n\r]{5,60})',
        r'([A-ZÁÉÍÓÚÑ][A-Za-záéíóúñÁÉÍÓÚÑ\s]{5,50}(?:SPA|S\.A\.|LTDA|SRL|SAC|EIRL|SAS))',
    ]
    for pat in prov_patterns:
        for m in re.finditer(pat, text, re.I | re.MULTILINE):
            prov = m.group(1).strip().rstrip('"').strip()
            if (5 < len(prov) < 80 and
                    not re.match(r'^[A-Z]\s+[A-Z]', prov) and
                    not re.match(r'^\d', prov) and
                    not re.match(r'^(SN|NS|SS|NN)\s', prov)):
                fields["proveedor"] = prov
                break
        if fields.get("proveedor"):
            break

    # ── Descripción ──
    desc_patterns = [
        r'(?:Giro|GIRO)\s*[:\s]\s*([^\n\r]{5,100})',
        r'(?:DESCRIPCI[ÓO]N|DETALLE|CONCEPTO|GLOSA)\s*[:\s]\s*([^\n\r]{5,80})',
        r'(?:Descripci[oó]n|Detalle)\s*[:\s]\s*([^\n\r]{5,80})',
    ]
    for pat in desc_patterns:
        m = re.search(pat, text, re.I)
        if m:
            desc = m.group(1).strip()
            if len(desc) > 5:
                fields["descripcion"] = desc[:150]
                break

    # ── Moneda ──
    if re.search(r'S/\.?\s*\d|soles?|\bPEN\b', text, re.I):
        fields["moneda"] = "PEN"
    elif re.search(r'\bUSD\b|\bUS\$\b|\bdolares?\b', text, re.I):
        fields["moneda"] = "USD"
    elif re.search(r'\bMXN\b|pesos?\s+mexic', text, re.I):
        fields["moneda"] = "MXN"
    elif re.search(r'\bARS\b|pesos?\s+argent', text, re.I):
        fields["moneda"] = "ARS"
    elif re.search(r'\bCLP\b|pesos?\s+chile', text, re.I):
        fields["moneda"] = "CLP"
    else:
        fields["moneda"] = "COP"

    return fields


# ── Prompts para cada agente ──────────────────────────────────────────────────

# System prompt del Agente Visual
# Recibe: imagen + texto OCR del paso anterior
VISION_SYSTEM_PROMPT = """Eres el Agente Visual, un experto en análisis de documentos contables latinoamericanos.
Tu rol es analizar visualmente la imagen del documento y extraer TODA la información visible.
Eres el segundo agente en el pipeline — ya tienes el texto extraído por OCR del paso anterior.
Debes complementar y corregir el OCR con lo que ves directamente en la imagen.

REGLA ABSOLUTA: Tu respuesta final DEBE terminar con un objeto JSON completo y válido.
El JSON debe estar en una única línea al final, sin markdown, sin bloques de código.
Si el thinking te lleva a analizar el documento, SIEMPRE termina con el JSON.
NUNCA dejes el JSON incompleto o truncado."""

def build_vision_prompt(ocr_text: str) -> str:
    """Construye el prompt del Agente Visual con el contexto OCR del paso anterior."""
    return f"""El Agente OCR extrajo el siguiente texto del documento (puede tener errores):

=== TEXTO OCR (Agente OCR) ===
{ocr_text[:2000] if ocr_text else "(sin texto OCR)"}
=== FIN TEXTO OCR ===

Analiza la IMAGEN del documento. Corrige los errores del OCR con lo que ves directamente.

REGLAS PARA MONTOS:
- monto_total: el número que aparece junto a "TOTAL", "TOTAL SI.", "Total a pagar" al final
- monto_neto: precio sin impuestos (P. UNITARIO x cantidad, o subtotal)
- iva: impuesto (IVA, IGV, impuesto)
- Los montos son SOLO números flotantes (sin $, sin puntos de miles)
- Ejemplo: "$1,200.00" → 1200.0

DEVUELVE EXACTAMENTE ESTE JSON (sin texto antes ni después, sin markdown):
{{"tipo_documento":"FACTURA|DOCUMENTO_EQUIVALENTE|RECIBO|NOTA_CREDITO|OTRO","proveedor":"nombre o null","rut_proveedor":"NIT o null","fecha_emision":"YYYY-MM-DD o null","folio":"numero o null","descripcion":"descripcion o null","monto_neto":numero_o_null,"iva":numero_o_null,"monto_total":numero_o_null,"moneda":"COP|USD|EUR|PEN|MXN|ARS","texto_completo":"texto visible"}}"""


# System prompt del Agente Extractor
# Recibe: imagen + texto OCR + campos del Agente Visual
EXTRACTOR_SYSTEM_PROMPT = """Eres el Agente Extractor, un contador experto en documentos contables latinoamericanos.
Tu rol es extraer campos estructurados del documento con máxima precisión.
Eres el tercer agente en el pipeline — tienes el texto OCR y los campos detectados por el Agente Visual.
Debes consolidar y completar la información, priorizando los valores más confiables.

REGLA ABSOLUTA: Tu respuesta final DEBE ser un objeto JSON completo y válido en una sola línea.
NUNCA uses markdown, bloques de código, ni texto antes o después del JSON.
NUNCA dejes el JSON truncado o incompleto.
Si el Agente Visual ya detectó un campo con valor, úsalo como base y mejora si puedes."""

def build_extractor_prompt(ocr_text: str, vision_fields: Dict) -> str:
    """Construye el prompt del Agente Extractor con el contexto acumulado."""
    # Mostrar los campos del agente visual de forma clara, incluyendo montos
    vision_lines = []
    if vision_fields:
        for k, v in vision_fields.items():
            if v is not None and str(v) not in ('null', 'None', ''):
                vision_lines.append(f"  {k}: {v}")
    vision_summary = '\n'.join(vision_lines) if vision_lines else "(sin campos del agente visual)"

    return f"""Tienes acceso a:

=== TEXTO OCR (Agente OCR) ===
{ocr_text[:2000] if ocr_text else "(sin texto OCR)"}
=== FIN TEXTO OCR ===

=== CAMPOS YA DETECTADOS POR AGENTE VISUAL (usa estos como base) ===
{vision_summary}
=== FIN CAMPOS VISUALES ===

Analiza TODA esta información (y la imagen si está disponible) y consolida los campos.

REGLAS CRÍTICAS:
1. Si el Agente Visual ya detectó un campo, úsalo a menos que veas algo mejor en la imagen/OCR
2. monto_total: busca "TOTAL", "TOTAL SI.", "Total a pagar" al final del documento
   - Si el Visual detectó monto_total, úsalo directamente
   - Los montos son números flotantes: "$1,200.00" → 1200.0
3. Si monto_neto e iva están disponibles y monto_total no: monto_total = monto_neto + iva
4. rut_proveedor: NIT colombiano (XXX.XXX.XXX-X). También RUC, RFC o RUT si el documento es extranjero.
5. fecha_emision: formato YYYY-MM-DD

DEVUELVE EXACTAMENTE ESTE JSON EN UNA SOLA LÍNEA (sin texto antes ni después):
{{"tipo_documento":"FACTURA|DOCUMENTO_EQUIVALENTE|RECIBO|NOTA_CREDITO|OTRO","proveedor":"nombre o null","rut_proveedor":"NIT o null","fecha_emision":"YYYY-MM-DD o null","folio":"numero o null","descripcion":"descripcion o null","monto_neto":numero_o_null,"iva":numero_o_null,"monto_total":numero_o_null,"moneda":"COP|USD|EUR|PEN|MXN|ARS"}}"""


# System prompt del Agente Clasificador
# Recibe: todos los campos extraídos + texto combinado
CLASSIFIER_SYSTEM_PROMPT = """Eres el Agente Clasificador, un contador experto en categorización de gastos empresariales.
Tu rol es determinar la categoría contable más apropiada para este gasto.
Tienes acceso a TODA la información extraída por los agentes anteriores.
Responde SOLO con JSON válido, sin texto adicional ni markdown."""

def build_classifier_prompt(fields: Dict, combined_text: str, categorias: str, learnings_text: str = "") -> str:
    """Construye el prompt del Agente Clasificador con contexto completo y memoria de aprendizaje."""
    learnings_section = ""
    if learnings_text:
        learnings_section = f"""\n{learnings_text}\n
IMPORTANTE: Si el proveedor o tipo de documento coincide con alguna clasificación aprendida arriba,
USA esa categoría con confianza alta (0.95). El usuario ya corrigió esa clasificación antes.
"""
    return f"""Clasifica este gasto empresarial en la categoría contable más apropiada.

=== INFORMACIÓN EXTRAÍDA (todos los agentes anteriores) ===
- Tipo documento: {fields.get('tipo_documento', 'desconocido')}
- Proveedor: {fields.get('proveedor', 'desconocido')}
- NIT: {fields.get('rut_proveedor', 'no detectado')}
- Descripción/Giro: {fields.get('descripcion', 'no detectada')}
- Monto total: {fields.get('monto_total', 'no detectado')} {fields.get('moneda', 'COP')}
- Fecha: {fields.get('fecha_emision', 'no detectada')}
=== FIN INFORMACIÓN ===

=== TEXTO DEL DOCUMENTO (primeras 500 palabras) ===
{combined_text[:1000] if combined_text else "(sin texto)"}
=== FIN TEXTO ===
{learnings_section}
CATEGORÍAS DISPONIBLES:
{categorias}

Responde en JSON:
{{
  "categoria": "nombre exacto de la categoría de la lista",
  "confianza": 0.0 a 1.0,
  "razon": "explicación breve (máximo 100 caracteres)"
}}"""


# System prompt del Agente Auditor
# Recibe: todos los campos + clasificación + imagen (si disponible)
AUDITOR_SYSTEM_PROMPT = """Eres el Agente Auditor, un experto en control interno y detección de anomalías contables.
Tu rol es validar la coherencia del documento y detectar problemas.
Tienes acceso a TODA la información del pipeline: OCR, visión, extracción y clasificación.
Responde SOLO con JSON válido, sin texto adicional ni markdown."""

def build_auditor_prompt(fields: Dict, classification: Dict, ocr_text: str, vision_fields: Dict) -> str:
    """Construye el prompt del Agente Auditor con contexto completo."""
    return f"""Audita este documento contable y detecta anomalías.

=== CAMPOS EXTRAÍDOS (consolidados) ===
{json.dumps(fields, ensure_ascii=False, indent=2)}
=== FIN CAMPOS ===

=== CLASIFICACIÓN SUGERIDA ===
- Categoría: {classification.get('categoria_sugerida', 'Sin categoría')}
- Confianza: {classification.get('confianza', 0):.0%}
- Razón: {classification.get('razon', '')}
=== FIN CLASIFICACIÓN ===

=== CAMPOS DEL AGENTE VISUAL ===
{json.dumps(vision_fields, ensure_ascii=False)}
=== FIN CAMPOS VISUALES ===

Verifica:
1. ¿Los montos son coherentes? (monto_neto + iva ≈ monto_total)
2. ¿La fecha es razonable? (no futura, no muy antigua)
3. ¿El proveedor y la descripción son coherentes con la categoría?
4. ¿Hay campos críticos faltantes?
5. ¿El documento parece legítimo?

Responde en JSON:
{{
  "anomalias": ["lista de anomalías detectadas, vacía si no hay"],
  "campos_faltantes": ["lista de campos críticos no detectados"],
  "coherencia_montos": true/false,
  "requiere_revision_manual": true/false,
  "nivel_confianza_documento": 0.0 a 1.0,
  "observacion": "observación general breve"
}}"""


KEYWORD_MAP = {
    "tecnología": ["software", "hosting", "cloud", "internet", "computaci",
                   "informátic", "digital", "web", "servidor", "licencia",
                   "app", "sistema", "equipos computacionales", "programas"],
    "marketing": ["publicidad", "marketing", "diseño", "imprenta",
                  "redes sociales", "campaña", "flyer", "banner", "fotografía"],
    "operaciones": ["arriendo", "arrend", "servicio", "mantenci",
                    "limpieza", "seguridad", "agua", "luz", "gas", "electricidad"],
    "sueldos": ["sueldo", "salario", "honorario", "remuneraci", "liquidaci"],
    "impuestos": ["impuesto", "iva", "sii", "contribuci", "patente"],
    "transporte": ["transporte", "flete", "courier", "envío",
                   "despacho", "taxi", "uber", "combustible", "bencina"],
    "alimentación": ["restaurant", "comida", "almuerzo", "café",
                     "aliment", "supermercado", "catering"],
    "equipamiento": ["equipo", "maquinaria", "herramienta",
                     "mueble", "escritorio", "silla"],
    "servicios profesionales": ["consultoría", "asesoría", "abogado",
                                "contador", "auditor", "ingeniero", "notario"],
    "financiero": ["banco", "comisión", "interés", "crédito", "préstamo",
                   "leasing", "seguro", "pensión", "arl"],
}


def _parse_json_response(text: str) -> Dict:
    """Extrae JSON de la respuesta del LLM.

    Maneja:
    - Bloques ```json ... ``` con texto antes/después
    - JSON puro sin markdown
    - Respuestas con thinking tags <think>...</think>
    - Respuestas con texto explicativo antes del JSON
    - JSON con campos numéricos en formato string ("1,200.00" -> 1200.0)
    """
    if not text:
        return {}
    # Eliminar thinking tags de qwen3 (<think>...</think>)
    text = re.sub(r'<think>[\s\S]*?</think>', '', text, flags=re.IGNORECASE).strip()
    if not text:
        return {}

    # Patrones en orden de prioridad
    patterns = [
        r"```json\s*([\s\S]*?)\s*```",   # Bloque ```json ... ```
        r"```\s*(\{[\s\S]*?\})\s*```",   # Bloque ``` { ... } ```
        r"```\s*([\s\S]*?)\s*```",        # Cualquier bloque de código
        r"(\{[\s\S]*\})",                 # JSON puro (greedy - toma el más largo)
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            candidate = match.group(1).strip()
            try:
                result = json.loads(candidate)
                if isinstance(result, dict) and result:
                    return result
            except Exception:
                # Intentar limpiar el JSON antes de parsear
                try:
                    # Eliminar comentarios de estilo // ...
                    cleaned = re.sub(r'//[^\n]*', '', candidate)
                    # Eliminar trailing commas antes de } o ]
                    cleaned = re.sub(r',\s*([}\]])', r'\1', cleaned)
                    result = json.loads(cleaned)
                    if isinstance(result, dict) and result:
                        return result
                except Exception as e:
                    logger.debug(f"Excepción controlada: {e}")
    # Último intento: parsear el texto completo
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except Exception as e:
        logger.debug(f"Excepción controlada: {e}")
    return {}


# ── Pipeline Principal ────────────────────────────────────────────────────────

class DocumentPipelineAgent:
    """
    Agente de procesamiento de documentos con pipeline visible y contexto acumulado.

    Flujo de contexto:
      OCR → (texto) → Visual (imagen + texto OCR) → Extractor (imagen + texto + campos visual)
      → Clasificador (campos consolidados + texto) → Auditor (todo lo anterior + imagen)
      → Guardado

    Emite eventos SSE por cada paso del pipeline.
    """

    def __init__(self, db, empresa_id: str, uploads_dir: Path):
        self.db = db
        self.empresa_id = empresa_id
        self.uploads_dir = uploads_dir
        self._available_models: Optional[List[str]] = None
        self._text_model: Optional[str] = None
        self._vision_model: Optional[str] = None
        self._llama_model: Optional[str] = None

    def _get_models(self):
        """Detecta modelos disponibles (cached). Retorna (vision_model, llama_model)."""
        if self._available_models is None:
            from ollama_client import (
                resolve_ollama_model,
                resolve_ollama_vision_model,
                _is_vision_name,
            )
            self._available_models = _ollama_list()
            vision = resolve_ollama_vision_model(VISION_DEFAULT_MODEL)
            if self._available_models and not any(_is_vision_name(n) for n in self._available_models):
                vision = None
            elif not self._available_models:
                vision = None
            self._vision_model = vision
            self._llama_model = resolve_ollama_model(LLAMA_MODEL)
            # Fallback: si no hay llama, usar visión para texto no es válido; deja llama del launcher
            if not self._llama_model:
                self._llama_model = self._vision_model
            # text_model es el fallback genérico (texto, no visión)
            self._text_model = self._llama_model or self._vision_model
            logger.info(f"Modelos — visión/extractor: {self._vision_model}, clasificador/auditor: {self._llama_model}")
        return self._vision_model, self._llama_model

    def _emit(self, step: str, status: str, data: Dict) -> str:
        """Genera un evento SSE."""
        event = {
            "step": step,
            "status": status,
            "timestamp": datetime.now().isoformat(),
            **data
        }
        return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    def _get_agent_config(self, agent_id: str) -> Dict:
        """
        Obtiene la configuración del agente desde el archivo JSON de configuración.
        El archivo es gestionado por los endpoints /api/agents en app.py.
        Modelos por defecto: qwen para visual/extractor, llama para clasificador/auditor/recomendador.
        """
        # Defaults por agente
        agent_defaults = {
            # max_tokens aumentados para evitar truncamiento del JSON de salida.
            # VISION: Usa VISION_DEFAULT_MODEL (moondream) porque necesita soporte multimodal.
            # qwen3:0.6b NO soporta imágenes y falla silenciosamente en Windows.
            "vision":       {"modelo": VISION_DEFAULT_MODEL,  "timeout": 300, "temperature": 0.1, "max_tokens": 4096},
            "extractor":    {"modelo": LLAMA_MODEL, "timeout": 240, "temperature": 0.1, "max_tokens": 3000},
            "clasificador": {"modelo": LLAMA_MODEL, "timeout": 120, "temperature": 0.05, "max_tokens": 1024},
            "auditor":      {"modelo": LLAMA_MODEL, "timeout": 180, "temperature": 0.05, "max_tokens": 2048},
            "recomendador": {"modelo": LLAMA_MODEL, "timeout": 240, "temperature": 0.2,  "max_tokens": 3000},
        }
        d = agent_defaults.get(agent_id, {"modelo": QWEN_MODEL, "timeout": 240, "temperature": 0.1, "max_tokens": 3000})
        try:
            agents_file = self.uploads_dir.parent / "agents_config.json"
            if agents_file.exists():
                agents = json.loads(agents_file.read_text(encoding="utf-8"))
                for a in agents:
                    if a.get("id") == agent_id:
                        params = a.get("parametros", {})
                        modelo = a.get("modelo") or d["modelo"]
                        if str(modelo).strip().lower() in ("perfil", "auto"):
                            modelo = d["modelo"]
                        return {
                            "modelo": modelo,
                            "prompt": a.get("prompt") or "",
                            "system_prompt": a.get("system_prompt") or "",
                            "timeout": params.get("timeout", d["timeout"]),
                            "temperature": params.get("temperature", d["temperature"]),
                            "max_tokens": params.get("max_tokens", d["max_tokens"]),
                        }
        except Exception as e:
            logger.debug(f"AgentConfig read error: {e}")
        return {
            "modelo": d["modelo"],
            "prompt": "",
            "system_prompt": "",
            "timeout": d["timeout"],
            "temperature": d["temperature"],
            "max_tokens": d["max_tokens"],
        }

    def _auto_create_categories(self) -> List[Dict]:
        """Crea las categorías contables base si la empresa aún no tiene."""
        from default_categories import seed_default_categories
        return seed_default_categories(self.db, self.empresa_id)

    def process_stream(self, file_path: str, original_filename: str) -> Generator[str, None, None]:
        """
        Procesa un documento y emite eventos SSE por cada paso.
        Cada agente recibe la imagen + su prompt + contexto acumulado de pasos anteriores.
        """
        doc_id = str(uuid.uuid4())
        ext = Path(file_path).suffix.lower()
        vision_model, llama_model = self._get_models()
        # En desktop el perfil de RAM (OLLAMA_MODEL) es el modelo de texto.
        text_model = llama_model or _find_best_model(TEXT_MODELS, self._available_models or []) or vision_model

        # Determinar si el archivo es una imagen (para enviar a modelos de visión)
        is_image = ext in (".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp")

        # Pre-cargar imagen en base64 una sola vez (reutilizada por todos los agentes)
        img_b64: Optional[str] = None
        if is_image:
            img_b64 = _image_to_base64(file_path)

        # Para PDFs escaneados, convertir primera página a imagen para los agentes de visión
        if not is_image and ext == ".pdf" and vision_model:
            try:
                page = render_pdf_page(file_path)
                if page is not None:
                    import io as _io
                    buf = _io.BytesIO()
                    page.convert("RGB").save(buf, format="JPEG", quality=90)
                    img_b64 = base64.b64encode(buf.getvalue()).decode()
                    logger.info("PDF convertido a imagen ampliada para agentes de visión")
            except Exception as e:
                logger.debug(f"PDF to image: {e}")

        # ── Contexto acumulado durante el pipeline ────────────────────────────
        # Cada agente lee y escribe en este contexto
        ctx: Dict = {
            "doc_id": doc_id,
            "file_path": file_path,
            "original_filename": original_filename,
            "ext": ext,
            "is_image": is_image,
            "img_b64": img_b64,            # Imagen compartida por todos los agentes
            "ocr_text": "",                # Salida del Agente OCR
            "vision_fields": {},           # Salida del Agente Visual
            "vision_text": "",             # Texto adicional del Agente Visual
            "combined_text": "",           # Texto combinado OCR + Visual
            "fields": {},                  # Campos consolidados (Extractor)
            "classification": {},          # Salida del Agente Clasificador
            "audit": {},                   # Salida del Agente Auditor
            "saved": False,
            # Resultados individuales por agente (para mostrar en el modal de detalle)
            "agent_results": {
                "ocr": {},
                "vision": {},
                "extractor": {},
                "clasificador": {},
                "auditor": {}
            }
        }

        # ── PASO 1: OCR ───────────────────────────────────────────────────────
        # El OCR no usa LLM — usa Tesseract/EasyOCR directamente
        # Produce: texto bruto que alimenta a todos los agentes siguientes
        ocr_running_msg = (
            f"Procesando {original_filename} con RapidOCR (fotos)..."
            if os.environ.get("RUN_BY_TAURI") == "1"
            else f"Procesando {original_filename} con RapidOCR / Tesseract..."
        )
        yield self._emit("ocr", "running", {
            "title": "Agente OCR — Extracción de texto",
            "message": ocr_running_msg,
        })

        try:
            raw_ocr = get_ocr_agent().extract_from_file(file_path)
            ocr_result = {
                "text": raw_ocr.get("text", "") or "",
                "words": raw_ocr.get("word_count", 0),
                "method": raw_ocr.get("method", "unknown"),
            }
            logger.info(
                "Pipeline OCR: metodo=%s palabras=%s archivo=%s",
                ocr_result["method"],
                ocr_result["words"],
                sanitize_log(original_filename),
            )

            ctx["ocr_text"] = ocr_result["text"]
            ctx["combined_text"] = ocr_result["text"]
            if _desktop_sidecar() and not ctx["combined_text"]:
                logger.warning("Desktop OCR no extrajo texto (RapidOCR/Tesseract/VLM)")
            # Guardar resultado del agente OCR
            ctx["agent_results"]["ocr"] = {
                "metodo": ocr_result.get("method", "tesseract"),
                "palabras": ocr_result.get("words", 0),
                "texto_preview": ocr_result["text"][:500] if ocr_result["text"] else "",
                "campos_detectados": {},  # OCR no extrae campos estructurados
                # Debug: el texto completo extraído es la "salida" del OCR
                "debug_prompt": f"Archivo: {original_filename}\nMétodo: {ocr_result.get('method', 'tesseract')}\nPSM: multi-pass (3, 6, 11)",
                "debug_raw_response": ocr_result["text"][:3000] if ocr_result["text"] else "(sin texto extraído)"
            }

            yield self._emit("ocr", "done", {
                "title": "Agente OCR completado",
                "message": f"Extraídas {ocr_result['words']} palabras via {ocr_result['method']}",
                "words": ocr_result["words"],
                "method": ocr_result["method"],
                "preview": ocr_result["text"][:400] if ocr_result["text"] else ""
            })
        except Exception as e:
            logger.error(f"OCR error: {e}")
            ctx["ocr_text"] = ""
            yield self._emit("ocr", "error", {
                "title": "OCR falló",
                "message": str(e)
            })

        # ── PASO 2: Agente Visual ─────────────────────────────────────────────
        # Desktop: moondream ya transcribio en OCR; extraccion con llama texto.
        if _desktop_sidecar():
            yield self._emit("vision", "info", {
                "title": "Agente Visual omitido (desktop)",
                "message": "El OCR (RapidOCR) ya transcribió el documento; campos con modelo de texto.",
            })
        elif vision_model and img_b64:
            cfg_vision = self._get_agent_config("vision")
            vision_model_to_use = _resolve_vision_model(
                cfg_vision["modelo"] or vision_model,
                vision_model,
                self._available_models,
            )
            if not vision_model_to_use:
                yield self._emit("vision", "error", {
                    "title": "Agente Visual omitido",
                    "message": "No hay modelo de vision (moondream) en Ollama",
                })
                vision_model_to_use = None

            if vision_model_to_use:
                yield self._emit("vision", "running", {
                    "title": "Agente Visual — Análisis de imagen",
                    "message": f"Analizando imagen con {vision_model_to_use} + contexto OCR...",
                })
                try:
                    vision_prompt = (
                        cfg_vision["prompt"]
                        if cfg_vision["prompt"]
                        else build_vision_prompt(ctx["ocr_text"])
                    )
                    vision_system = (
                        cfg_vision["system_prompt"]
                        if cfg_vision["system_prompt"]
                        else VISION_SYSTEM_PROMPT
                    )
                    is_qwen_vision = "qwen" in vision_model_to_use.lower()
                    if is_qwen_vision and vision_prompt.startswith("/think\n"):
                        vision_prompt = vision_prompt[len("/think\n"):]

                    vision_resp, vision_prompt_sent, vision_raw, vision_thinking = _ollama_generate(
                        model=vision_model_to_use,
                        prompt=vision_prompt,
                        image_b64=img_b64,
                        system=vision_system,
                        timeout=cfg_vision["timeout"],
                        temperature=cfg_vision["temperature"],
                        max_tokens=cfg_vision["max_tokens"],
                        enable_thinking=is_qwen_vision,
                    )

                    vision_data = _parse_json_response(vision_resp)
                    if not vision_data and vision_raw:
                        vision_data = _parse_json_response(vision_raw)
                    if not vision_data and vision_raw:
                        vision_data = _extract_fields_from_thinking(vision_raw)
                    if not vision_data and vision_thinking:
                        vision_data = _extract_fields_from_thinking(vision_thinking)

                    if vision_data.get("texto_completo"):
                        ctx["vision_text"] = str(vision_data.pop("texto_completo", ""))
                        ctx["combined_text"] = (
                            ctx["ocr_text"] + "\n\n[AGENTE VISUAL]\n" + ctx["vision_text"]
                        ).strip()
                    elif vision_raw and not ctx["vision_text"] and not _vision_raw_is_noise(vision_raw):
                        ctx["vision_text"] = vision_raw[:4000]
                        ctx["combined_text"] = (
                            ctx["ocr_text"] + "\n\n[AGENTE VISUAL]\n" + ctx["vision_text"]
                        ).strip()

                    vision_data = _filter_vision_fields(vision_data)
                    ctx["vision_fields"] = _filter_vision_fields(
                        {
                            k: v
                            for k, v in vision_data.items()
                            if v is not None and v not in ("", "null", "None")
                        }
                    )

                    for k, v in ctx["vision_fields"].items():
                        if k not in ctx["fields"] or not ctx["fields"][k]:
                            ctx["fields"][k] = v

                    ctx["agent_results"]["vision"] = {
                        "modelo": vision_model_to_use,
                        "campos_detectados": {k: str(v) for k, v in ctx["vision_fields"].items()},
                        "texto_adicional": ctx["vision_text"][:300] if ctx["vision_text"] else "",
                        "heredado_de_ocr": ctx["agent_results"]["ocr"].get("campos_detectados", {}),
                        "debug_prompt": vision_prompt_sent[:3000],
                        "debug_system": vision_system[:1000] if vision_system else "",
                        "debug_raw_response": vision_raw[:3000],
                        "debug_thinking": vision_thinking[:2000] if vision_thinking else "",
                    }

                    yield self._emit("vision", "done", {
                        "title": "Agente Visual completado",
                        "message": f"Detectados {len(ctx['vision_fields'])} campos en la imagen",
                        "fields_found": list(ctx["vision_fields"].keys()),
                        "preview": ctx["vision_text"][:300] if ctx["vision_text"] else "",
                    })
                except Exception as e:
                    logger.error(f"Vision error: {e}")
                    yield self._emit("vision", "error", {
                        "title": "Agente Visual falló (continuando)",
                        "message": str(e)[:200],
                    })
        else:
            reason = "Motor IA no disponible (Ollama offline)" if not vision_model else "PDF sin conversión de imagen"
            yield self._emit("vision", "info", {
                "title": "Agente Visual omitido",
                "message": reason
            })

        # ── PASO 3: Agente Extractor ──────────────────────────────────────────────────
        # Recibe: IMAGEN + texto OCR + campos del Agente Visual
        # Produce: campos estructurados consolidados
        cfg_extractor = self._get_agent_config("extractor")
        if _desktop_sidecar():
            from ollama_client import resolve_ollama_model
            extractor_model_to_use = resolve_ollama_model(
                text_model or llama_model or cfg_extractor.get("modelo") or LLAMA_MODEL
            )
            if extractor_model_to_use and _model_supports_vision(extractor_model_to_use):
                extractor_model_to_use = resolve_ollama_model(LLAMA_MODEL)
        elif img_b64:
            extractor_model_to_use = _resolve_vision_model(
                cfg_extractor["modelo"] or vision_model,
                vision_model,
                self._available_models,
            )
        else:
            extractor_model_to_use = cfg_extractor["modelo"] or text_model
            if self._available_models is not None:
                resolved = _find_best_model(
                    [extractor_model_to_use], self._available_models
                )
                if resolved:
                    extractor_model_to_use = resolved
        yield self._emit("extraction", "running", {
            "title": "Agente Extractor — Consolidación de campos",
            "message": (
                f"Extrayendo campos con {extractor_model_to_use} (solo texto)..."
                if _desktop_sidecar()
                else f"Extrayendo campos con {extractor_model_to_use} + regex..."
            ),
        })

        try:
            # 1. Regex siempre como base (rápido, sin LLM)
            regex_fields = _extract_by_regex(ctx["combined_text"])
            # Merge: regex completa lo que no detectó el Agente Visual
            for k, v in regex_fields.items():
                if k not in ctx["fields"] or not ctx["fields"][k]:
                    ctx["fields"][k] = v

            # 2. LLM con imagen + contexto (desktop: moondream aun con OCR vacio)
            word_count = len(ctx["combined_text"].split()) if ctx["combined_text"] else 0
            run_extractor_llm = bool(
                extractor_model_to_use
                and (
                    img_b64
                    or ctx["vision_fields"]
                    or word_count >= 5
                )
            )
            if run_extractor_llm:
                # Usar prompt personalizado si está configurado
                if cfg_extractor["prompt"]:
                    extractor_prompt = cfg_extractor["prompt"].replace("{text}", ctx["combined_text"][:2500])
                else:
                    extractor_prompt = build_extractor_prompt(ctx["ocr_text"], ctx["vision_fields"])

                extractor_system = cfg_extractor["system_prompt"] if cfg_extractor["system_prompt"] else EXTRACTOR_SYSTEM_PROMPT
                # Activar thinking nativo para qwen3
                is_qwen_extractor = "qwen" in extractor_model_to_use.lower()
                if is_qwen_extractor and extractor_prompt.startswith("/think\n"):
                    extractor_prompt = extractor_prompt[len("/think\n"):]

                extractor_image = (
                    img_b64
                    if extractor_model_to_use and _model_supports_vision(extractor_model_to_use)
                    else None
                )
                llm_resp, extractor_prompt_sent, extractor_raw, extractor_thinking = _ollama_generate(
                    model=extractor_model_to_use,
                    prompt=extractor_prompt,
                    image_b64=extractor_image,
                    system=extractor_system,
                    timeout=cfg_extractor["timeout"],
                    temperature=cfg_extractor["temperature"],
                    max_tokens=cfg_extractor["max_tokens"],
                    enable_thinking=is_qwen_extractor
                )
                llm_fields = _parse_json_response(llm_resp)

                # Si el LLM no devolvió campos pero hay thinking, intentar extraer del thinking
                if not llm_fields and extractor_thinking:
                    logger.debug("Extractor: LLM sin campos, intentando extraer del thinking")
                    llm_fields = _extract_fields_from_thinking(extractor_thinking)

                # El Extractor consolida: LLM tiene prioridad para campos clave
                PRIORITY_FIELDS = {"monto_total", "monto_neto", "iva", "proveedor",
                                   "descripcion", "tipo_documento", "fecha_emision",
                                   "folio", "rut_proveedor"}
                for k, v in llm_fields.items():
                    if v is not None and v not in ("", "null", "None", 0):
                        if k in PRIORITY_FIELDS:
                            ctx["fields"][k] = v  # LLM siempre gana para campos clave
                        elif k not in ctx["fields"] or not ctx["fields"][k]:
                            ctx["fields"][k] = v

                # Fallback adicional: si monto_total sigue sin detectarse,
                # intentar extraerlo del thinking del agente visual
                if "monto_total" not in ctx["fields"] or not ctx["fields"].get("monto_total"):
                    vision_thinking = ctx["agent_results"].get("vision", {}).get("debug_thinking", "")
                    if vision_thinking:
                        vision_thinking_fields = _extract_fields_from_thinking(vision_thinking)
                        for k in ("monto_total", "monto_neto", "iva"):
                            if k in vision_thinking_fields and vision_thinking_fields[k]:
                                ctx["fields"][k] = vision_thinking_fields[k]
                                logger.debug(f"Campo {k}={vision_thinking_fields[k]} recuperado del thinking del agente visual")

            # Limpiar nulls y strings vacíos
            ctx["fields"] = {
                k: v for k, v in ctx["fields"].items()
                if v is not None and v != "" and str(v) not in ("null", "None")
            }

            # Guardar resultado del agente Extractor con herencia de Vision y OCR + debug
            ctx["agent_results"]["extractor"] = {
                "modelo": extractor_model_to_use or "regex",
                "campos_detectados": {k: str(v) for k, v in ctx["fields"].items()},
                "campos_regex": {k: str(v) for k, v in regex_fields.items()},
                "heredado_de_vision": ctx["agent_results"].get("vision", {}).get("campos_detectados", {}),
                "heredado_de_ocr": ctx["agent_results"].get("ocr", {}).get("campos_detectados", {}),
                # Datos de depuración
                "debug_prompt": locals().get("extractor_prompt_sent", "")[:3000],
                "debug_system": locals().get("extractor_system", "")[:1000] if locals().get("extractor_system") else "",
                "debug_raw_response": locals().get("extractor_raw", "")[:3000],
                "debug_thinking": locals().get("extractor_thinking", "")[:2000] if locals().get("extractor_thinking") else ""
            }

            yield self._emit("extraction", "done", {
                "title": "Agente Extractor completado",
                "message": f"Consolidados {len(ctx['fields'])} campos (OCR + Visual + LLM)",
                "fields": {k: str(v) for k, v in ctx["fields"].items()}
            })
        except Exception as e:
            logger.error(f"Extraction error: {e}")
            yield self._emit("extraction", "error", {
                "title": "Extracción falló",
                "message": str(e),
                "fields": {k: str(v) for k, v in ctx["fields"].items()}
            })

        # ── PASO 4: Agente Clasificador ───────────────────────────────────────
        # Recibe: todos los campos consolidados + texto completo
        # Produce: categoría contable + confianza
        cfg_classifier = self._get_agent_config("clasificador")
        yield self._emit("classification", "running", {
            "title": "Agente Clasificador — Categorización contable",
            "message": "Determinando categoría con todos los campos extraídos..."
        })

        try:
            from models import CategoriaContable
            categorias = self.db.query(CategoriaContable).filter(
                CategoriaContable.empresa_id == self.empresa_id
            ).all()
            cat_list = [{"id": c.id, "nombre": c.nombre,
                         "descripcion": c.tipo_gasto or c.nombre} for c in categorias]

            classification: Dict = {
                "categoria_sugerida": None, "confianza": 0.0,
                "razon": "", "categoria_id": None
            }

            # AUTO-CREACIÓN DE CATEGORÍAS: Si no hay categorías configuradas,
            # crear automáticamente un set base al escanear la primera boleta.
            # Esto resuelve el problema de que el usuario no tenga categorías
            # y el clasificador retorne "Sin categoría" sin poder asignar nada.
            if not cat_list:
                logger.info("No hay categorías configuradas. Creando categorías automáticas...")
                cat_list = self._auto_create_categories()
                yield self._emit("classification", "info", {
                    "title": "Categorías creadas automáticamente",
                    "message": f"Se crearon {len(cat_list)} categorías base para clasificar gastos",
                    "categorias_creadas": [c["nombre"] for c in cat_list]
                })

            if cat_list:
                combined_lower = ctx["combined_text"].lower()
                proveedor_lower = str(ctx["fields"].get("proveedor", "")).lower()
                desc_lower = str(ctx["fields"].get("descripcion", "")).lower()
                search_text = combined_lower + " " + proveedor_lower + " " + desc_lower

                # 0. MEMORIA DE PROVEEDOR: Buscar si este proveedor ya fue clasificado antes
                # Esto permite clasificación instantánea sin LLM para proveedores conocidos.
                try:
                    from classification_memory import find_provider_category, set_data_dir as _set_mem_dir2
                    import os as _os2
                    _mem_data_dir2 = Path(_os2.environ.get("DATA_DIR", str(self.uploads_dir.parent / "data")))
                    _set_mem_dir2(_mem_data_dir2)
                    memory_result = find_provider_category(
                        empresa_id=self.empresa_id,
                        proveedor=str(ctx["fields"].get("proveedor", "")),
                        rut_proveedor=str(ctx["fields"].get("rut_proveedor", ""))
                    )
                    if memory_result:
                        # Verificar que la categoría aún existe en cat_list
                        mem_cat_id = memory_result.get("categoria_id", "")
                        mem_cat_name = memory_result.get("categoria", "")
                        cat_exists = any(
                            c["id"] == mem_cat_id or c["nombre"].lower() == mem_cat_name.lower()
                            for c in cat_list
                        )
                        if cat_exists:
                            classification["categoria_sugerida"] = mem_cat_name
                            classification["categoria_id"] = mem_cat_id
                            classification["confianza"] = memory_result["confianza"]
                            classification["razon"] = (
                                f"Clasificación por memoria ({memory_result['fuente']}): "
                                f"proveedor ya clasificado previamente"
                            )
                            logger.info(
                                "Clasificación por memoria: %s → %s (confianza: %.0f%%)",
                                sanitize_log(ctx["fields"].get("proveedor", "?")),
                                sanitize_log(mem_cat_name),
                                memory_result["confianza"] * 100,
                            )
                except Exception as _mem_err:
                    logger.debug(f"Memoria de proveedor error (no crítico): {_mem_err}")

                # 1. LLM con contexto completo (solo si la memoria no resolvió)
                classifier_model_to_use = cfg_classifier["modelo"] or llama_model or text_model
                if classifier_model_to_use and not classification["categoria_sugerida"]:
                    cat_names = "\n".join(
                        f"- {c['nombre']}" + (f" ({c['descripcion']})" if c['descripcion'] != c['nombre'] else "")
                        for c in cat_list
                    )
                    if cfg_classifier["prompt"]:
                        cls_prompt = cfg_classifier["prompt"].format(
                            proveedor=ctx["fields"].get("proveedor", "Desconocido"),
                            descripcion=ctx["fields"].get("descripcion", ctx["combined_text"][:200]),
                            tipo_documento=ctx["fields"].get("tipo_documento", ""),
                            monto_total=ctx["fields"].get("monto_total", ""),
                            moneda=ctx["fields"].get("moneda", "COP"),
                            categorias=cat_names,
                            resumen="",
                            giro=""
                        )
                    else:
                        # Cargar memoria de clasificaciones humanas
                        _learnings_text = ""
                        try:
                            from classification_memory import get_classification_learnings
                            _learnings_text = get_classification_learnings(self.empresa_id)
                        except Exception as _learn_err:
                            logger.warning(f"No se pudo cargar memoria de clasificaciones: {_learn_err}")
                        cls_prompt = build_classifier_prompt(ctx["fields"], ctx["combined_text"], cat_names, _learnings_text)

                    classifier_system = cfg_classifier["system_prompt"] if cfg_classifier["system_prompt"] else CLASSIFIER_SYSTEM_PROMPT
                    llm_resp, cls_prompt_sent, cls_raw, cls_thinking = _ollama_generate(
                        model=classifier_model_to_use,
                        prompt=cls_prompt,
                        system=classifier_system,
                        timeout=cfg_classifier["timeout"],
                        temperature=cfg_classifier["temperature"],
                        max_tokens=cfg_classifier["max_tokens"]
                    )
                    cls_data = _parse_json_response(llm_resp)
                    if cls_data.get("categoria"):
                        classification["categoria_sugerida"] = cls_data["categoria"]
                        classification["confianza"] = float(cls_data.get("confianza", 0.7))
                        classification["razon"] = cls_data.get("razon", "")

                # 2. Keyword fallback si LLM no clasificó
                if not classification["categoria_sugerida"]:
                    for cat in cat_list:
                        cat_name_lower = cat["nombre"].lower()
                        for key, keywords in KEYWORD_MAP.items():
                            if key in cat_name_lower:
                                for kw in keywords:
                                    if kw in search_text:
                                        classification["categoria_sugerida"] = cat["nombre"]
                                        classification["confianza"] = 0.65
                                        classification["razon"] = f"Palabra clave '{kw}' encontrada"
                                        break
                            if classification["categoria_sugerida"]:
                                break
                        if classification["categoria_sugerida"]:
                            break

                # 3. Buscar ID de la categoría sugerida
                if classification["categoria_sugerida"]:
                    for c in cat_list:
                        if c["nombre"].lower() == classification["categoria_sugerida"].lower():
                            classification["categoria_id"] = c["id"]
                            break
                    if not classification["categoria_id"]:
                        for c in cat_list:
                            if (c["nombre"].lower() in classification["categoria_sugerida"].lower() or
                                    classification["categoria_sugerida"].lower() in c["nombre"].lower()):
                                classification["categoria_id"] = c["id"]
                                classification["categoria_sugerida"] = c["nombre"]
                                break

            ctx["classification"] = classification
            # Guardar debug del clasificador
            ctx["_cls_debug"] = {
                "prompt": locals().get("cls_prompt_sent", "")[:3000],
                "system": locals().get("classifier_system", "")[:1000] if locals().get("classifier_system") else "",
                "raw_response": locals().get("cls_raw", "")[:3000],
                "thinking": locals().get("cls_thinking", "")[:2000] if locals().get("cls_thinking") else ""
            }

            # Guardar resultado del agente Clasificador con herencia del Extractor + debug
            ctx["agent_results"]["clasificador"] = {
                "modelo": cfg_classifier.get("modelo", "llama3.2:3b"),
                "categoria_sugerida": classification.get("categoria_sugerida"),
                "confianza": classification.get("confianza", 0.0),
                "razon": classification.get("razon", ""),
                "heredado_de_extractor": ctx["agent_results"].get("extractor", {}).get("campos_detectados", {}),
                # Datos de depuración
                "debug_prompt": ctx.get("_cls_debug", {}).get("prompt", ""),
                "debug_system": ctx.get("_cls_debug", {}).get("system", ""),
                "debug_raw_response": ctx.get("_cls_debug", {}).get("raw_response", ""),
                "debug_thinking": ctx.get("_cls_debug", {}).get("thinking", "")
            }

            yield self._emit("classification", "done", {
                "title": "Agente Clasificador completado",
                "message": (f"Categoría: {classification['categoria_sugerida'] or 'Sin categoría'} "
                            f"(confianza: {int(classification['confianza'] * 100)}%)"),
                "categoria_sugerida": classification["categoria_sugerida"],
                "confianza": classification["confianza"],
                "razon": classification["razon"],
                "categorias_disponibles": [c["nombre"] for c in cat_list]
            })
        except Exception as e:
            logger.error(f"Classification error: {e}")
            yield self._emit("classification", "error", {
                "title": "Clasificación falló",
                "message": str(e)
            })

        # ── PASO 5: Agente Auditor ────────────────────────────────────────────
        # Recibe: TODOS los campos + clasificación + imagen (opcional)
        # Produce: alertas, validación de coherencia, detección de anomalías
        cfg_auditor = self._get_agent_config("auditor")
        yield self._emit("audit", "running", {
            "title": "Agente Auditor — Validación y anomalías",
            "message": "Verificando coherencia, duplicados y anomalías con contexto completo..."
        })

        try:
            from models import Documento as DocModel
            excepciones = []
            requiere_revision = False

            # Hash del archivo para detección de duplicados
            sha256 = hashlib.sha256()
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    sha256.update(chunk)
            file_hash = sha256.hexdigest()

            # Verificar duplicado exacto (mismo archivo)
            dup = self.db.query(DocModel).filter(
                DocModel.empresa_id == self.empresa_id,
                DocModel.hash_documento == file_hash
            ).first()
            if dup:
                excepciones.append({
                    "tipo": "DUPLICADO",
                    "mensaje": f"Documento ya existe (ID: {dup.id[:8]}...)"
                })
                requiere_revision = True

            # Validar campos críticos
            if not ctx["fields"].get("monto_total"):
                excepciones.append({"tipo": "CAMPO_FALTANTE",
                                     "mensaje": "Monto total no detectado"})
                requiere_revision = True

            if not ctx["fields"].get("proveedor"):
                excepciones.append({"tipo": "CAMPO_FALTANTE",
                                     "mensaje": "Proveedor no detectado"})
                requiere_revision = True

            # Verificar coherencia de montos
            monto_total = ctx["fields"].get("monto_total")
            monto_neto = ctx["fields"].get("monto_neto")
            iva = ctx["fields"].get("iva")
            if monto_total and monto_neto and iva:
                try:
                    total_calc = float(monto_neto) + float(iva)
                    total_doc = float(monto_total)
                    if abs(total_calc - total_doc) > total_doc * 0.05:  # >5% diferencia
                        excepciones.append({
                            "tipo": "INCOHERENCIA_MONTOS",
                            "mensaje": f"Neto ({monto_neto}) + IVA ({iva}) ≠ Total ({monto_total})"
                        })
                        requiere_revision = True
                except Exception as e:
                    logger.debug(f"Excepción controlada: {e}")

            # Baja confianza de clasificación
            if ctx["classification"].get("confianza", 0) < 0.5:
                excepciones.append({"tipo": "BAJA_CONFIANZA",
                                     "mensaje": "Clasificación con baja confianza"})
                requiere_revision = True

            # Auditoría con LLM si está disponible (para detectar anomalías semánticas)
            auditor_model_to_use = cfg_auditor["modelo"] or llama_model or text_model
            if _desktop_sidecar():
                from ollama_client import resolve_ollama_model
                auditor_model_to_use = resolve_ollama_model(auditor_model_to_use or LLAMA_MODEL)
            elif self._available_models is not None:
                resolved_auditor = _find_best_model(
                    [auditor_model_to_use], self._available_models
                )
                if resolved_auditor:
                    auditor_model_to_use = resolved_auditor
            if auditor_model_to_use and not dup:  # No auditar duplicados con LLM
                try:
                    if cfg_auditor["prompt"]:
                        audit_prompt = cfg_auditor["prompt"]
                    else:
                        audit_prompt = build_auditor_prompt(
                            ctx["fields"], ctx["classification"],
                            ctx["ocr_text"], ctx["vision_fields"]
                        )

                    auditor_system = cfg_auditor["system_prompt"] if cfg_auditor["system_prompt"] else AUDITOR_SYSTEM_PROMPT
                    audit_image = (
                        img_b64
                        if _model_supports_vision(auditor_model_to_use)
                        else None
                    )
                    audit_resp, audit_prompt_sent, audit_raw, audit_thinking = _ollama_generate(
                        model=auditor_model_to_use,
                        prompt=audit_prompt,
                        image_b64=audit_image,
                        system=auditor_system,
                        timeout=cfg_auditor["timeout"],
                        temperature=cfg_auditor["temperature"],
                        max_tokens=cfg_auditor["max_tokens"]
                    )
                    audit_data = _parse_json_response(audit_resp)
                    if audit_data:
                        # Agregar anomalías detectadas por LLM
                        for anomalia in audit_data.get("anomalias", []):
                            if anomalia and isinstance(anomalia, str):
                                excepciones.append({"tipo": "ANOMALIA_IA", "mensaje": anomalia})
                                requiere_revision = True
                        # Agregar campos faltantes detectados por LLM
                        for campo in audit_data.get("campos_faltantes", []):
                            if campo and isinstance(campo, str):
                                excepciones.append({"tipo": "CAMPO_FALTANTE_IA", "mensaje": f"Campo no detectado: {campo}"})
                        # Actualizar requiere_revision si el auditor IA lo indica
                        if audit_data.get("requiere_revision_manual"):
                            requiere_revision = True
                except Exception as e:
                    logger.debug(f"Audit LLM error: {e}")

            ctx["audit"] = {
                "hash": file_hash,
                "excepciones": excepciones,
                "requiere_revision": requiere_revision
            }

            # Guardar resultado del agente Auditor — tiene acceso a TODO el pipeline
            ctx["agent_results"]["auditor"] = {
                "modelo": cfg_auditor.get("modelo", "llama3.2:3b"),
                "excepciones": excepciones,
                "requiere_revision": requiere_revision,
                "hash": file_hash,
                # El auditor tiene la verdad consolidada de todos los agentes
                "campos_finales": {k: str(v) for k, v in ctx["fields"].items()},
                "categoria_final": ctx["classification"].get("categoria_sugerida"),
                "confianza_final": ctx["classification"].get("confianza", 0.0),
                # Datos de depuración del clasificador y auditor
                "debug_clasificador_prompt": ctx.get("_cls_debug", {}).get("prompt", ""),
                "debug_clasificador_system": ctx.get("_cls_debug", {}).get("system", ""),
                "debug_clasificador_response": ctx.get("_cls_debug", {}).get("raw_response", ""),
                "debug_prompt": locals().get("audit_prompt_sent", "")[:3000],
                "debug_system": locals().get("auditor_system", "")[:1000] if locals().get("auditor_system") else "",
                "debug_raw_response": locals().get("audit_raw", "")[:3000],
                "debug_thinking": locals().get("audit_thinking", "")[:2000] if locals().get("audit_thinking") else ""
            }

            yield self._emit("audit", "done", {
                "title": "Agente Auditor completado",
                "message": (f"{len(excepciones)} alertas encontradas"
                            if excepciones else "Sin alertas — documento válido"),
                "excepciones": excepciones,
                "requiere_revision": requiere_revision
            })
        except Exception as e:
            ctx["audit"] = {"hash": "", "excepciones": [], "requiere_revision": True}
            yield self._emit("audit", "error", {
                "title": "Auditoría falló",
                "message": str(e)
            })

        # ── PASO 6: Guardado ──────────────────────────────────────────────────
        yield self._emit("save", "running", {
            "title": "Guardando documento",
            "message": "Persistiendo en base de datos..."
        })

        try:
            saved_doc = self._save_document(
                doc_id=doc_id,
                file_path=file_path,
                original_filename=original_filename,
                ctx=ctx
            )
            ctx["saved"] = True

            # AUTO-APRENDIZAJE: Guardar contexto de clasificación exitosa
            # para que el clasificador recuerde proveedor→categoría en futuras boletas.
            try:
                from classification_memory import save_auto_classification_context, set_data_dir as _set_mem_dir
                _set_mem_dir(self.uploads_dir.parent / "data" if not (self.uploads_dir.parent / "data").exists()
                             else self.uploads_dir.parent / "data")
                # Usar DATA_DIR del entorno
                import os as _os
                _mem_data_dir = Path(_os.environ.get("DATA_DIR", str(self.uploads_dir.parent / "data")))
                _set_mem_dir(_mem_data_dir)

                if ctx["classification"].get("categoria_sugerida") and ctx["classification"].get("confianza", 0) >= 0.7:
                    save_auto_classification_context(
                        empresa_id=self.empresa_id,
                        proveedor=str(ctx["fields"].get("proveedor", "")),
                        rut_proveedor=str(ctx["fields"].get("rut_proveedor", "")),
                        tipo_documento=str(ctx["fields"].get("tipo_documento", "")),
                        categoria_nombre=ctx["classification"]["categoria_sugerida"],
                        categoria_id=ctx["classification"].get("categoria_id", ""),
                        texto_ocr=ctx["combined_text"][:300] if ctx["combined_text"] else "",
                        monto_total=float(ctx["fields"].get("monto_total", 0) or 0),
                        confianza=ctx["classification"]["confianza"]
                    )
                    logger.info(
                        "Auto-aprendizaje guardado: %s → %s",
                        sanitize_log(ctx["fields"].get("proveedor", "?")),
                        sanitize_log(ctx["classification"]["categoria_sugerida"]),
                    )
            except Exception as _learn_err:
                logger.debug(f"Auto-aprendizaje error (no crítico): {_learn_err}")

            yield self._emit("save", "done", {
                "title": "Documento guardado",
                "message": f"ID: {saved_doc.id[:8]}...",
                "doc_id": saved_doc.id,
                "empresa_id": self.empresa_id
            })

            # Evento final con resumen completo
            yield self._emit("complete", "done", {
                "title": "Procesamiento completado",
                "message": "El documento ha sido procesado y guardado exitosamente",
                "doc_id": saved_doc.id,
                "fields": {k: str(v) for k, v in ctx["fields"].items()},
                "classification": ctx["classification"],
                "audit": ctx["audit"],
                "ocr_words": len([w for w in ctx["combined_text"].split() if len(w) > 2]),
                "models_used": {
                    "text": text_model,
                    "vision": vision_model
                }
            })

        except Exception as e:
            logger.error(f"Save error: {e}")
            err_str = str(e)
            if "UNIQUE constraint" in err_str and "hash_documento" in err_str:
                try:
                    from models import Documento as DocModel
                    self.db.rollback()
                    file_hash = ctx.get("audit", {}).get("hash", "")
                    existing = None
                    if file_hash:
                        existing = self.db.query(DocModel).filter(
                            DocModel.empresa_id == self.empresa_id,
                            DocModel.hash_documento == file_hash
                        ).first()
                    if existing:
                        yield self._emit("save", "info", {
                            "title": "Documento duplicado detectado",
                            "message": f"Este documento ya fue procesado (ID: {existing.id[:8]}...)",
                            "doc_id": existing.id
                        })
                        yield self._emit("complete", "done", {
                            "title": "Documento ya existente",
                            "message": "Este documento ya fue procesado anteriormente",
                            "doc_id": existing.id,
                            "fields": {k: str(v) for k, v in ctx["fields"].items()},
                            "classification": ctx["classification"],
                            "audit": ctx["audit"],
                            "ocr_words": len([w for w in ctx["combined_text"].split() if len(w) > 2]),
                            "models_used": {"text": text_model, "vision": vision_model},
                            "duplicate": True
                        })
                        return
                except Exception as e2:
                    logger.error(f"Duplicate lookup error: {e2}")
            yield self._emit("save", "error", {
                "title": "Error al guardar",
                "message": err_str[:200]
            })

    def _save_document(self, doc_id: str, file_path: str,
                       original_filename: str, ctx: Dict):
        """Guarda el documento en la base de datos."""
        from models import (Documento, TipoDocumento, EstadoRevision)

        fields = ctx["fields"]
        classification = ctx["classification"]
        audit = ctx["audit"]

        # Tipo de documento
        tipo_str = str(fields.get("tipo_documento", "OTRO")).upper()
        tipo_str = re.sub(r'\s+', '_', tipo_str)
        tipo_str = re.sub(r'[ÁÉÍÓÚ]', lambda m: {'Á': 'A', 'É': 'E', 'Í': 'I', 'Ó': 'O', 'Ú': 'U'}.get(m.group(), m.group()), tipo_str)
        tipo_map = {
            "FACTURA": TipoDocumento.FACTURA,
            "FACTURA_ELECTRONICA": TipoDocumento.FACTURA,
            "BOLETA": TipoDocumento.BOLETA,
            "BOLETA_ELECTRONICA": TipoDocumento.BOLETA,
            "BOLETA_DE_VENTA": TipoDocumento.BOLETA,
            "RECIBO": TipoDocumento.COMPROBANTE,
            "RECIBO_DE_HONORARIOS": TipoDocumento.COMPROBANTE,
            "NOTA_CREDITO": TipoDocumento.COMPROBANTE,
            "NOTA_DE_CREDITO": TipoDocumento.COMPROBANTE,
            "COMPROBANTE": TipoDocumento.COMPROBANTE,
            "CARTOLA": TipoDocumento.CARTOLA,
        }
        tipo_enum = tipo_map.get(tipo_str, TipoDocumento.OTRO)

        # Fecha
        fecha_emision = None
        if fields.get("fecha_emision"):
            for fmt in ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"]:
                try:
                    fecha_emision = datetime.strptime(str(fields["fecha_emision"]), fmt)
                    break
                except Exception as e:
                    logger.debug(f"Excepción controlada: {e}")

        # Montos
        def to_float(v) -> Optional[float]:
            if v is None or isinstance(v, bool):
                return None
            text = str(v).strip().lower()
            if text in {"", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity", "nan", "null", "none"}:
                return None
            try:
                number = float(text.replace(",", ".").replace(" ", ""))
            except Exception:
                return None
            if not math.isfinite(number):
                return None
            return number

        # Estado
        estado = (EstadoRevision.PENDIENTE
                  if audit.get("requiere_revision", True)
                  else EstadoRevision.REVISADO)

        # Hash único
        file_hash = audit.get("hash", "")
        if not file_hash:
            sha256 = hashlib.sha256()
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    sha256.update(chunk)
            file_hash = sha256.hexdigest()

        existing = self.db.query(Documento).filter(
            Documento.empresa_id == self.empresa_id,
            Documento.hash_documento == file_hash,
        ).first()
        if existing:
            logger.info(
                "Documento duplicado hash=%s: actualizando %s",
                file_hash[:12],
                existing.id,
            )
            existing.tipo_documento = tipo_enum
            existing.proveedor = fields.get("proveedor") or existing.proveedor
            existing.rut_proveedor = fields.get("rut_proveedor") or existing.rut_proveedor
            existing.fecha_emision = fecha_emision or existing.fecha_emision
            existing.folio = (
                str(fields.get("folio", "")) if fields.get("folio") else existing.folio
            )
            if to_float(fields.get("monto_total")):
                existing.monto_total = to_float(fields.get("monto_total"))
            if to_float(fields.get("monto_neto")):
                existing.monto_neto = to_float(fields.get("monto_neto"))
            if to_float(fields.get("iva")):
                existing.iva = to_float(fields.get("iva"))
            existing.moneda = str(fields.get("moneda", existing.moneda or "COP"))
            existing.categoria_id = classification.get("categoria_id") or existing.categoria_id
            existing.categoria_sugerida = (
                classification.get("categoria_sugerida") or existing.categoria_sugerida
            )
            existing.confianza_clasificacion = float(
                classification.get("confianza", existing.confianza_clasificacion or 0.0)
            )
            existing.estado_revision = estado
            existing.ruta_archivo_original = str(file_path)
            existing.texto_extraido = (
                ctx["combined_text"][:5000] if ctx["combined_text"] else existing.texto_extraido
            )
            existing.campos_extraidos = json.dumps(
                {"campos": fields, "agent_results": ctx.get("agent_results", {})},
                ensure_ascii=False,
            )
            existing.excepciones = json.dumps(
                audit.get("excepciones", []), ensure_ascii=False
            )
            existing.fecha_actualizacion = datetime.utcnow()
            self.db.commit()
            self.db.refresh(existing)
            return existing

        doc = Documento(
            id=doc_id,
            empresa_id=self.empresa_id,
            tipo_documento=tipo_enum,
            proveedor=fields.get("proveedor"),
            rut_proveedor=fields.get("rut_proveedor"),
            fecha_emision=fecha_emision,
            folio=str(fields.get("folio", "")) if fields.get("folio") else None,
            monto_neto=to_float(fields.get("monto_neto")),
            iva=to_float(fields.get("iva")),
            monto_total=to_float(fields.get("monto_total")),
            moneda=str(fields.get("moneda", "COP")),
            categoria_id=classification.get("categoria_id"),
            categoria_sugerida=classification.get("categoria_sugerida"),
            confianza_clasificacion=float(classification.get("confianza", 0.0)),
            estado_revision=estado,
            ruta_archivo_original=str(file_path),
            hash_documento=file_hash,
            texto_extraido=ctx["combined_text"][:5000] if ctx["combined_text"] else "",
            # Guardar tanto los campos consolidados como los resultados por agente
            campos_extraidos=json.dumps({
                "campos": fields,
                "agent_results": ctx.get("agent_results", {})
            }, ensure_ascii=False),
            excepciones=json.dumps(audit.get("excepciones", []), ensure_ascii=False),
        )

        self.db.add(doc)
        self.db.commit()
        self.db.refresh(doc)
        return doc
