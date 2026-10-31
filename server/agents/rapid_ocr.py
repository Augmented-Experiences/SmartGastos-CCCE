"""
OCR clásico para fotos de boletas/facturas via RapidOCR + ONNX Runtime.

Sin torch. RapidOCR 3.3 no tiene Global.model_root_dir: el cache se redirige
a DATA_DIR/ocr_models parcheando InferSession.DEFAULT_MODEL_PATH.
Reconocimiento latin (español, números, RUT) con rec PP-OCRv4.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_engine = None
_engine_tried = False
_engine_lock = threading.Lock()


def _model_root_dir() -> Path:
    data = os.environ.get("DATA_DIR")
    if data:
        root = Path(data) / "ocr_models"
    else:
        root = Path(__file__).resolve().parents[2] / "data" / "ocr_models"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _bundled_model_dir() -> Path | None:
    """ONNX empaquetados en el sidecar (PyInstaller) o en el repo de desarrollo."""
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "ocr_models")
    candidates.append(Path(__file__).resolve().parents[2] / "data" / "ocr_models")
    for path in candidates:
        try:
            if path.is_dir() and any(path.glob("*.onnx")):
                return path
        except OSError:
            continue
    return None


def _seed_models(dest: Path) -> None:
    """Copia ONNX del bundle a DATA_DIR para no depender de modelscope en el primer OCR."""
    src = _bundled_model_dir()
    if src is None:
        return
    try:
        if src.resolve() == dest.resolve():
            return
    except OSError:
        return
    copied = 0
    for src_file in src.glob("*.onnx"):
        target = dest / src_file.name
        if target.exists() and target.stat().st_size > 0:
            continue
        try:
            shutil.copy2(src_file, target)
            copied += 1
        except OSError as exc:
            logger.warning("No se pudo copiar modelo OCR %s: %s", src_file.name, exc)
    if copied:
        logger.info("OCR: copiados %s modelos ONNX a %s", copied, dest)


def _redirect_model_cache(root: Path) -> None:
    """En RapidOCR 3.3 los ONNX se bajan a rapidocr/models (se pierde al cerrar
    el sidecar frozen). Forzar DATA_DIR para reutilizarlos entre sesiones."""
    try:
        from rapidocr.inference_engine.base import InferSession

        InferSession.DEFAULT_MODEL_PATH = root
    except Exception as exc:
        logger.debug("No se pudo redirigir InferSession.DEFAULT_MODEL_PATH: %s", exc)
    try:
        import rapidocr.ch_ppocr_rec.main as rec_main

        rec_main.DEFAULT_MODEL_PATH = root
    except Exception as exc:
        logger.debug("No se pudo redirigir rec DEFAULT_MODEL_PATH: %s", exc)


def _upright_image(img_path: str):
    """Aplica orientación EXIF (fotos de celular). Devuelve array RGB o None."""
    try:
        import numpy as np
        from PIL import Image, ImageOps
        from security import open_receipt_image

        img = open_receipt_image(img_path)
        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            img = img.convert("RGB")
        return np.array(img)
    except Exception as exc:
        logger.debug("EXIF/PIL upright falló: %s", exc)
        return None


def _result_to_text(result) -> str:
    if result is None:
        return ""
    txts = getattr(result, "txts", None)
    boxes = getattr(result, "boxes", None)
    if txts:
        lines = [str(t).strip() for t in txts if t]
        if boxes is not None and len(boxes) == len(txts):
            paired = []
            for box, txt in zip(boxes, txts):
                if not txt:
                    continue
                try:
                    y = float(box[0][1])
                except Exception:
                    y = 0.0
                paired.append((y, str(txt).strip()))
            paired.sort(key=lambda item: item[0])
            lines = [t for _, t in paired if t]
        return "\n".join(lines).strip()

    rows = result
    if isinstance(result, tuple) and result:
        rows = result[0]
    if isinstance(rows, list) and rows:
        sortable = []
        for row in rows:
            try:
                if isinstance(row, (list, tuple)) and len(row) >= 2:
                    box, txt = row[0], row[1]
                    y = float(box[0][1]) if box else 0.0
                    sortable.append((y, str(txt).strip()))
                elif isinstance(row, dict) and row.get("txt"):
                    sortable.append((0.0, str(row["txt"]).strip()))
            except Exception:
                continue
        sortable.sort(key=lambda item: item[0])
        lines = [t for _, t in sortable if t]
        return "\n".join(lines).strip()
    return ""


def engine_param_attempts():
    """Params válidos para RapidOCR 3.3 (sin Global.model_root_dir / log_level)."""
    attempts = []
    try:
        from rapidocr import EngineType, LangRec, ModelType, OCRVersion

        attempts.append(
            {
                "Global.max_side_len": 4800,
                "Det.limit_side_len": 1600,
                "Det.limit_type": "min",
                "Det.engine_type": EngineType.ONNXRUNTIME,
                "Cls.engine_type": EngineType.ONNXRUNTIME,
                "Rec.engine_type": EngineType.ONNXRUNTIME,
                "Rec.lang_type": LangRec.LATIN,
                "Rec.model_type": ModelType.MOBILE,
                "Rec.ocr_version": OCRVersion.PPOCRV4,
            }
        )
        attempts.append(
            {
                "Global.max_side_len": 4800,
                "Det.limit_side_len": 1600,
                "Det.limit_type": "min",
                "Det.engine_type": EngineType.ONNXRUNTIME,
                "Rec.engine_type": EngineType.ONNXRUNTIME,
                "Rec.lang_type": LangRec.EN,
                "Rec.model_type": ModelType.MOBILE,
                "Rec.ocr_version": OCRVersion.PPOCRV4,
            }
        )
    except Exception as exc:
        logger.debug("RapidOCR enums no disponibles: %s", exc)
    attempts.append({
        "Global.max_side_len": 4800,
        "Det.limit_side_len": 1600,
        "Det.limit_type": "min",
    })
    return attempts


def _build_engine():
    from rapidocr import RapidOCR

    model_root = _model_root_dir()
    _seed_models(model_root)
    _redirect_model_cache(model_root)
    last_error = None
    for params in engine_param_attempts():
        try:
            engine = RapidOCR(params=params) if params else RapidOCR()
            logger.info(
                "RapidOCR listo (lang=%s, models=%s)",
                params.get("Rec.lang_type", "default"),
                model_root,
            )
            return engine
        except Exception as exc:
            last_error = exc
            logger.warning(
                "RapidOCR init falló (%s): %s",
                params.get("Rec.lang_type", "default") if params else "default",
                exc,
            )
    if last_error:
        raise last_error
    raise RuntimeError("RapidOCR no pudo inicializarse")


def get_rapidocr_engine():
    """Singleton perezoso. None si RapidOCR/onnxruntime no está instalado."""
    global _engine, _engine_tried
    if _engine_tried:
        return _engine
    with _engine_lock:
        if _engine_tried:
            return _engine
        _engine_tried = True
        try:
            _engine = _build_engine()
        except Exception as exc:
            logger.warning("RapidOCR no disponible: %s", exc)
            _engine = None
        return _engine


def rapidocr_available() -> bool:
    return get_rapidocr_engine() is not None


def ocr_rapidocr(img_path: str) -> str:
    """Transcribe una imagen con RapidOCR. Nunca lanza."""
    engine = get_rapidocr_engine()
    if engine is None:
        return ""
    try:
        img = _upright_image(img_path)
        source = img if img is not None else img_path
        result = engine(source)
        text = _result_to_text(result)
        logger.info("RapidOCR: %s caracteres en %s", len(text), img_path)
        return text
    except Exception as exc:
        logger.warning("RapidOCR falló en %s: %s", img_path, exc)
        return ""
