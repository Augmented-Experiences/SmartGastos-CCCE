"""
Agente OCR Multi-Estrategia para pyme-ledger-ai.

Estrategias en orden de prioridad:
  1. Para PDFs: pypdf (texto digital) → pdf2image + RapidOCR/Tesseract
  2. Para fotos/imágenes: RapidOCR ONNX (latin) → Tesseract
     → VLLM Ollama solo si el OCR clásico no sirvió y la salida no es alucinación.

Moondream es un captioner, no un OCR: en fotos de boletas térmicas inventa
texto. Esa salida se descarta (ver ocr_quality.looks_hallucinated).
"""

import base64
import hashlib
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Optional

from .ocr_quality import (
    MIN_WORDS as QUALITY_MIN_WORDS,
    document_signal_score,
    looks_hallucinated,
    pick_ocr_result,
    word_count as quality_word_count,
)
from .rapid_ocr import ocr_rapidocr

logger = logging.getLogger(__name__)

# ── Constantes ────────────────────────────────────────────────────────────────
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")

# Modelos de visión preferidos en orden (del más ligero al más pesado)
VISION_MODELS_PREFERENCE = [
    "moondream",
    "llama3.2-vision:11b",
    "llama3.2-vision",
    "llava:7b",
    "llava",
    "granite3.2-vision",
    "minicpm-v",
]

# Umbral mínimo de palabras para considerar OCR exitoso
MIN_WORDS = QUALITY_MIN_WORDS


def _sanitize_repetitive_ocr(text: str) -> str:
    """Recorta bucles de moondream sin vaciar transcripciones validas."""
    original = (text or "").strip()
    if not original:
        return original
    lines = [ln.strip() for ln in original.splitlines() if ln.strip()]
    if len(lines) < 5:
        return original
    from collections import Counter

    top_line, top_n = Counter(lines).most_common(1)[0]
    if top_n < 5 or top_n / len(lines) <= 0.4:
        return original
    unique: list[str] = []
    seen: set[str] = set()
    for ln in lines:
        if ln in seen:
            continue
        seen.add(ln)
        unique.append(ln)
    deduped = "\n".join(unique[:120]).strip()
    if _word_count(deduped) >= MIN_WORDS:
        logger.warning("OCR moondream: frase repetida (%dx), salida deduplicada", top_n)
        return deduped
    return original


# ── Imports opcionales ────────────────────────────────────────────────────────
try:
    import pypdf  # noqa: F401
    _PYPDF_OK = True
except ImportError:
    _PYPDF_OK = False

try:
    from PIL import Image, ImageFilter, ImageEnhance
    _PIL_OK = True
except ImportError:
    _PIL_OK = False

# Tesseract: verificar disponibilidad una sola vez
_tesseract_ok: Optional[bool] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _word_count(text: str) -> int:
    """Cuenta palabras alfanuméricas de más de 2 caracteres."""
    return quality_word_count(text)


def _tesseract_available() -> bool:
    global _tesseract_ok
    if _tesseract_ok is None:
        try:
            r = subprocess.run(['tesseract', '--version'],
                               capture_output=True, timeout=5)
            _tesseract_ok = (r.returncode == 0)
        except Exception:
            _tesseract_ok = False
    return _tesseract_ok


def _get_vision_model() -> Optional[str]:
    """Detecta qué modelo de visión está disponible en Ollama."""
    try:
        from ollama_client import (
            resolve_ollama_vision_model,
            _ollama_model_names,
            _is_vision_name,
        )
        names = _ollama_model_names()
        if not any(_is_vision_name(n) for n in names):
            return None
        return resolve_ollama_vision_model("moondream")
    except Exception:
        return None


# ── Preprocesamiento ──────────────────────────────────────────────────────────

def _preprocess(img_path: str, mode: str = "standard") -> str:
    """
    Preprocesa imagen para mejorar OCR.
    Retorna ruta al archivo temporal preprocesado.
    """
    if not _PIL_OK:
        return img_path
    try:
        from security import open_receipt_image
        img = open_receipt_image(img_path)
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')

        # Escalar si es muy pequeña
        w, h = img.size
        if w < 1000:
            scale = max(1000 / w, 1.5)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

        gray = img.convert('L')

        if mode == "standard":
            gray = ImageEnhance.Contrast(gray).enhance(2.0)
            gray = gray.filter(ImageFilter.SHARPEN)
        elif mode == "aggressive":
            gray = ImageEnhance.Contrast(gray).enhance(3.0)
            gray = gray.point(lambda x: 0 if x < 128 else 255, '1').convert('L')
            gray = gray.filter(ImageFilter.MedianFilter(size=3))

        tmp = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
        gray.save(tmp.name, 'PNG', dpi=(300, 300))
        return tmp.name
    except Exception as e:
        logger.debug(f"Preprocesamiento falló: {e}")
        return img_path


# ── Estrategia 1: Tesseract ───────────────────────────────────────────────────

def _run_tesseract(img_path: str, psm: int, lang: str = "spa+eng") -> str:
    try:
        result = subprocess.run(
            ['tesseract', img_path, 'stdout', '-l', lang,
             '--psm', str(psm), '--oem', '3'],
            capture_output=True, text=True, timeout=60
        )
        return result.stdout.strip()
    except Exception as e:
        logger.debug(f"Tesseract PSM={psm} error: {e}")
        return ""


def ocr_tesseract(img_path: str) -> str:
    """Tesseract con múltiples PSM y preprocesamiento. Retorna el mejor resultado."""
    if not _tesseract_available():
        return ""

    best_text, best_count = "", 0
    tmp_files = []

    try:
        # Variantes de preprocesamiento
        variants = [(img_path, "raw")]
        for mode in ("standard", "aggressive"):
            p = _preprocess(img_path, mode)
            if p != img_path:
                variants.append((p, mode))
                tmp_files.append(p)

        # PSM modes a probar
        psm_modes = [6, 11, 3, 4]

        for img_v, mode_name in variants:
            for psm in psm_modes:
                text = _run_tesseract(img_v, psm)
                count = _word_count(text)
                if count > best_count:
                    best_count = count
                    best_text = text
                    logger.debug(f"Tesseract mejor: mode={mode_name} psm={psm} words={count}")
    finally:
        for f in tmp_files:
            try:
                os.unlink(f)
            except Exception:
                pass

    logger.info(f"Tesseract resultado: {best_count} palabras")
    return best_text


# ── Estrategia 2: VLLM via Ollama ────────────────────────────────────────────

def ocr_vllm_ollama(img_path: str, model: Optional[str] = None) -> str:
    """
    OCR usando modelo de visión (VLLM) via Ollama.
    Ideal para documentos complejos, manuscritos o con layout irregular.
    """
    try:
        import requests

        if model is None:
            model = _get_vision_model()
        else:
            try:
                from ollama_client import resolve_ollama_vision_model, _is_vision_name, _ollama_model_names
                names = _ollama_model_names()
                if any(_is_vision_name(n) for n in names):
                    model = resolve_ollama_vision_model(model)
            except Exception:
                pass
        if model is None:
            logger.debug("No hay modelo de visión disponible en Ollama")
            return ""

        # Cargar imagen y redimensionar si es muy grande
        with open(img_path, 'rb') as f:
            img_data = f.read()

        if _PIL_OK:
            try:
                import io
                from security import open_receipt_image
                img = open_receipt_image(img_path)
                w, h = img.size
                if w > 1920 or h > 1920:
                    img.thumbnail((1920, 1920), Image.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, format='JPEG', quality=85)
                    img_data = buf.getvalue()
            except Exception:
                pass

        img_b64 = base64.b64encode(img_data).decode()

        prompts = [
            (
                "Eres un experto en lectura de documentos contables latinoamericanos. "
                "Transcribe TODO el texto visible en esta imagen (factura, boleta, recibo).\n"
                "Escribe linea por linea el texto y numeros tal como aparecen (proveedor, fecha, items, totales, NIT).\n"
                "NO describas la imagen. NO repitas la misma frase. Sin comentarios ni coordenadas.\n\n"
                "TEXTO DEL DOCUMENTO:"
            ),
            "Transcribe all visible text on this receipt. One line per row of text. Numbers and dates included. Text only:",
        ]
        options = {"temperature": 0.1, "num_predict": 1500, "repeat_penalty": 1.05}
        text = ""
        for prompt in prompts:
            payload = {
                "model": model,
                "prompt": prompt,
                "images": [img_b64],
                "stream": False,
                "options": options,
            }
            resp = requests.post(
                f"{OLLAMA_URL}/api/generate", json=payload, timeout=180
            )
            if resp.status_code != 200:
                logger.warning(
                    "Ollama VLLM HTTP %s body=%s", resp.status_code, resp.text[:200]
                )
                continue
            text = _sanitize_repetitive_ocr(
                resp.json().get("response", "").strip()
            )
            if not text:
                continue
            if looks_hallucinated(text):
                logger.warning(
                    "VLLM (%s): salida descartada (alucinacion/caption): %s",
                    model,
                    (text[:160] + "…") if len(text) > 160 else text,
                )
                text = ""
                continue
            if _word_count(text) >= MIN_WORDS or document_signal_score(text) > 0:
                break
        if text:
            logger.info("VLLM (%s): %s palabras", model, _word_count(text))
            return text
        logger.warning("VLLM (%s): transcripcion vacia, corta o alucinada", model)
        return ""
    except Exception as e:
        logger.warning("VLLM Ollama falló (model=%s): %s", model, e)
        return ""


# ── Estrategia 4: pypdf para PDFs digitales ──────────────────────────────────

def ocr_pdf_digital(pdf_path: str) -> str:
    if not _PYPDF_OK:
        return ""
    try:
        from security import read_pdf_text, PdfRejected, sanitize_log
        combined = read_pdf_text(pdf_path)
        if _word_count(combined) > MIN_WORDS:
            logger.info("pypdf: %s palabras", _word_count(combined))
            return combined
        return ""
    except PdfRejected as exc:
        logger.info("PDF rechazado: %s", sanitize_log(exc))
        return ""
    except Exception as e:
        logger.debug("pypdf falló: %s", sanitize_log(e))
        return ""


PDF_OCR_DPI = 600
OCR_MIN_LONG_SIDE = 4800


def focus_document_image(image, min_long_side: int = OCR_MIN_LONG_SIDE):
    """Recorta márgenes vacíos y amplía el documento si quedó chico para leerlo."""
    from PIL import Image, ImageOps

    rgb = image.convert("RGB")
    gray = ImageOps.grayscale(rgb)
    ink = gray.point(lambda p: 0 if p > 242 else 255)
    bbox = ink.getbbox()
    if bbox:
        width, height = rgb.size
        x0, y0, x1, y1 = bbox
        content = (x1 - x0) * (y1 - y0)
        if content < 0.85 * width * height:
            pad_x = max(12, int((x1 - x0) * 0.03))
            pad_y = max(12, int((y1 - y0) * 0.03))
            rgb = rgb.crop((
                max(0, x0 - pad_x),
                max(0, y0 - pad_y),
                min(width, x1 + pad_x),
                min(height, y1 + pad_y),
            ))
    long_side = max(rgb.size)
    if long_side and long_side < min_long_side:
        scale = min(min_long_side / long_side, 8)
        resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        rgb = rgb.resize(
            (max(1, int(rgb.size[0] * scale)), max(1, int(rgb.size[1] * scale))),
            resample,
        )
    return rgb


def render_pdf_page(pdf_path: str, dpi: int = PDF_OCR_DPI):
    """Primera página del PDF, ya recortada y ampliada para OCR y vista previa."""
    from pdf2image import convert_from_path

    pages = convert_from_path(pdf_path, dpi=dpi, first_page=1, last_page=1)
    if not pages:
        return None
    return focus_document_image(pages[0])


def ocr_region_text(file_path: str, x: float, y: float, w: float, h: float) -> str:
    """OCR de un recorte. En PDF las coordenadas son de la página ya enfocada."""
    path = Path(file_path)
    ext = path.suffix.lower()
    if ext == ".pdf":
        image = render_pdf_page(file_path)
    else:
        from security import open_receipt_image
        image = open_receipt_image(file_path).convert("RGB")
    if image is None:
        return ""
    width, height = image.size
    x0 = int(max(0.0, min(1.0, x)) * width)
    y0 = int(max(0.0, min(1.0, y)) * height)
    x1 = int(max(0.0, min(1.0, x + w)) * width)
    y1 = int(max(0.0, min(1.0, y + h)) * height)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return ""
    crop = focus_document_image(image.crop((x0, y0, x1, y1)), min_long_side=3200)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        crop.save(tmp.name, "PNG")
        tmp_name = tmp.name
    try:
        result = run_image_ocr(
            tmp_name,
            allow_vllm=False,
        )
        return (result.get("text") or "").strip()
    finally:
        try:
            os.unlink(tmp_name)
        except Exception:
            pass


def ocr_pdf_scanned(pdf_path: str) -> str:
    """Convierte PDF escaneado a imágenes grandes y aplica OCR."""
    try:
        from pdf2image import convert_from_path
        images = convert_from_path(pdf_path, dpi=PDF_OCR_DPI, first_page=1, last_page=3)
        all_text = []
        for img in images:
            focused = focus_document_image(img)
            with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
                focused.save(tmp.name, 'PNG')
                tmp_name = tmp.name
            page = run_image_ocr(
                tmp_name,
                allow_vllm=False,
            )
            t = page.get("text") or ""
            if t:
                all_text.append(t)
            try:
                os.unlink(tmp_name)
            except Exception:
                pass
        return '\n\n--- PÁGINA SIGUIENTE ---\n\n'.join(all_text)
    except Exception as e:
        logger.debug(f"PDF escaneado OCR falló: {e}")
        return ""


# ── Clase principal ───────────────────────────────────────────────────────────

def _ocr_enough(candidate: Dict) -> bool:
    if not candidate or not candidate.get("text"):
        return False
    return candidate.get("signal", 0) > 0 or candidate.get("words", 0) >= MIN_WORDS


def run_image_ocr(
    img_path: str,
    *,
    allow_vllm: bool = True,
    vision_model: Optional[str] = None,
) -> Dict:
    """
    OCR de una imagen. RapidOCR primero; VLM solo si el clásico no sirvió.
    """
    strategies_tried: Dict[str, int] = {}
    candidates = []

    text_rapid = ocr_rapidocr(img_path)
    wc_rapid = _word_count(text_rapid)
    strategies_tried["rapidocr"] = wc_rapid
    candidates.append({"text": text_rapid, "method": "rapidocr"})

    best = pick_ocr_result(candidates)
    if not _ocr_enough(best) and _tesseract_available():
        text_tess = ocr_tesseract(img_path)
        wc_tess = _word_count(text_tess)
        strategies_tried["tesseract"] = wc_tess
        if text_tess:
            candidates.append({"text": text_tess, "method": "tesseract"})
            best = pick_ocr_result(candidates)

    if allow_vllm and not _ocr_enough(best):
        text_vllm = ocr_vllm_ollama(img_path, vision_model)
        wc_vllm = _word_count(text_vllm)
        method = f"vllm_{vision_model or 'auto'}"
        strategies_tried[method] = wc_vllm
        if text_vllm:
            candidates.append({"text": text_vllm, "method": method})
            best = pick_ocr_result(candidates)

    method = best.get("method") or "none"
    return {
        "text": best.get("text") or "",
        "method": method,
        "words": best.get("words", _word_count(best.get("text") or "")),
        "signal": best.get("signal", document_signal_score(best.get("text") or "")),
        "strategies": strategies_tried,
    }


class OCRAgent:
    """
    Agente OCR multi-estrategia para documentos contables.

    Orden de estrategias para imágenes:
      1. RapidOCR ONNX (latin) — OCR real para fotos de boletas
      2. Tesseract (si RapidOCR da poco texto)
      3. VLLM via Ollama (último recurso; se descarta si alucina)

    Orden de estrategias para PDFs:
      1. pypdf texto digital
      2. pdf2image + RapidOCR/Tesseract
    """

    def __init__(self, hardware_profile: str = "cpu"):
        self.hardware_profile = hardware_profile
        self._vision_model: Optional[str] = None
        self._vision_model_checked = False
        logger.info(
            "OCRAgent inicializado. Tesseract: %s | PIL: %s | pypdf: %s",
            "yes" if _tesseract_available() else "no",
            "yes" if _PIL_OK else "no",
            "yes" if _PYPDF_OK else "no",
        )

    def _get_vision_model_cached(self) -> Optional[str]:
        if not self._vision_model_checked:
            self._vision_model = _get_vision_model()
            self._vision_model_checked = True
        return self._vision_model

    def extract_from_file(self, file_path: str) -> Dict:
        """
        Extrae texto de un archivo. Nunca lanza excepciones.
        Retorna dict con: text, method, word_count, confidence, hash, file.
        """
        path = Path(file_path)
        ext = path.suffix.lower()

        if not path.exists():
            return {"text": "", "method": "error",
                    "error": f"Archivo no encontrado: {file_path}",
                    "word_count": 0, "confidence": 0.0}

        try:
            file_hash = self._compute_hash(file_path)
        except Exception:
            file_hash = ""

        # ── Procesar según tipo ───────────────────────────────────────────────
        if ext == ".pdf":
            result = self._process_pdf(file_path)
        elif ext in (".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"):
            result = self._process_image(file_path)
        else:
            result = {"text": "", "method": "unsupported",
                      "error": f"Extensión no soportada: {ext}"}

        # Agregar metadatos
        result["hash"] = file_hash
        result["file"] = str(file_path)
        if "text" not in result:
            result["text"] = ""

        wc = _word_count(result.get("text", ""))
        result["word_count"] = wc
        result["confidence"] = self._estimate_confidence(
            wc,
            signal=int(result.get("signal") or document_signal_score(result.get("text", ""))),
            method=result.get("method") or "",
        )

        logger.info(
            f"OCR completado — método: {result.get('method', '?')}, "
            f"palabras: {wc}, confianza: {result['confidence']:.1f}"
        )
        return result

    def _process_pdf(self, file_path: str) -> Dict:
        """Pipeline OCR para PDFs."""
        # Paso 1: texto digital
        text = ocr_pdf_digital(file_path)
        if _word_count(text) >= MIN_WORDS:
            return {"text": text, "method": "pdf_digital"}

        # Paso 2: PDF escaneado → imagen → OCR
        text = ocr_pdf_scanned(file_path)
        if _word_count(text) >= MIN_WORDS:
            return {"text": text, "method": "pdf_scanned_ocr"}

        # Paso 3: VLLM si hay modelo disponible
        vm = self._get_vision_model_cached()
        if vm:
            # Convertir primera página a imagen para el VLLM
            try:
                page = render_pdf_page(file_path)
                if page is not None:
                    with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
                        page.save(tmp.name, 'JPEG', quality=90)
                        text = ocr_vllm_ollama(tmp.name, vm)
                        try:
                            os.unlink(tmp.name)
                        except Exception:
                            pass
                    if _word_count(text) >= MIN_WORDS:
                        return {"text": text, "method": f"pdf_vllm_{vm}"}
            except Exception:
                pass

        return {"text": text or "", "method": "pdf_partial"}

    def _process_image(self, file_path: str) -> Dict:
        """Pipeline OCR para imágenes: RapidOCR primero, VLM solo si hace falta."""
        vm = self._get_vision_model_cached()
        result = run_image_ocr(
            file_path,
            allow_vllm=True,
            vision_model=vm,
        )
        from security import log_safe_path
        logger.info(
            "OCR imagen %s: metodo=%s palabras=%s signal=%s estrategias=%s",
            log_safe_path(file_path),
            result.get("method"),
            result.get("words"),
            result.get("signal"),
            result.get("strategies"),
        )
        return result

    def _estimate_confidence(self, word_count: int, signal: int = 0, method: str = "") -> float:
        """Estima confianza por palabras + señales de documento (RUT, TOTAL, …)."""
        if (method or "") in ("none", "error", "unsupported") and word_count <= 0:
            return 0.0
        if signal >= 3 and word_count >= 10:
            return 0.9
        if word_count >= 50:
            return 0.9
        if signal >= 2 and word_count >= MIN_WORDS:
            return 0.8
        if word_count >= 20:
            return 0.7
        if word_count >= MIN_WORDS or signal > 0:
            return 0.5
        if word_count > 0:
            return 0.3
        return 0.0

    def _compute_hash(self, file_path: str) -> str:
        """SHA256 del archivo para detección de duplicados."""
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()


# ── Singleton ─────────────────────────────────────────────────────────────────
_ocr_agent_instance: Optional[OCRAgent] = None


def get_ocr_agent(hardware_profile: str = "cpu") -> OCRAgent:
    """Obtiene o crea la instancia singleton del agente OCR."""
    global _ocr_agent_instance
    if _ocr_agent_instance is None:
        _ocr_agent_instance = OCRAgent(hardware_profile)
    return _ocr_agent_instance


# ── Test directo ──────────────────────────────────────────────────────────────
if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO,
                        format='%(levelname)s: %(message)s')

    test_files = sys.argv[1:] if len(sys.argv) > 1 else [
        '/tmp/boleta_test.jpeg',
        '/tmp/boleta_manuscrita.jpg',
    ]

    agent = OCRAgent()
    for f in test_files:
        if os.path.exists(f):
            print(f"\n{'='*60}")
            print(f"Procesando: {Path(f).name}")
            print('='*60)
            result = agent.extract_from_file(f)
            print(f"Método:    {result['method']}")
            print(f"Palabras:  {result['word_count']}")
            print(f"Confianza: {result['confidence']}")
            if 'strategies' in result:
                print(f"Estrategias: {result['strategies']}")
        else:
            print(f"Archivo no encontrado: {f}")
