"""
Calidad de OCR: señales de documento contable vs alucinación de VLM.

Moondream (y otros captioners) generan texto fluido cuando no pueden leer
una foto. Ese texto NO debe alimentarse al extractor solo porque tiene
muchas palabras.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional

MIN_WORDS = 10

_CAPTION_MARKERS = (
    "the image shows",
    "the image is",
    "this is a photo",
    "this image",
    "in this picture",
    "the photo shows",
    "i can see",
    "i cannot see",
    "can't make out",
    "appears to be",
    "this appears to be",
    "the document appears",
    "as an ai",
    "i'm sorry",
    "la imagen muestra",
    "la imagen es",
    "esta imagen",
    "en la foto",
    "se observa",
    "parece una boleta",
    "parece una factura",
    "no puedo ver",
    "no logro leer",
)

_DOCUMENT_KEYWORDS = (
    "boleta",
    "factura",
    "recibo",
    "comprobante",
    "ticket",
    "nota de credito",
    "nota de crédito",
    "guia de despacho",
    "guía de despacho",
    "documento equivalente",
    "documento soporte",
    "cuenta de cobro",
    "honorarios",
    "subtotal",
    "exento",
    "excluido",
    "afecto",
    "cantidad",
    "despacho",
    "folio",
    "consecutivo",
    "cufe",
    "neto",
    "total",
    "iva",
    "igv",
    "nit",
    "rut",
    "ruc",
    "rfc",
    "dian",
    "fecha",
)

_RUT_RE = re.compile(
    r"\b\d{1,2}(?:\.\d{3}){2}-[\dkK]\b|\b\d{7,9}-[\dkK]\b"
)
_NIT_RE = re.compile(
    r"\b\d{3}(?:\.\d{3}){2}-\d\b|\b\d{8,10}-\d\b|\bN\.?I\.?T\.?\b",
    re.I,
)
_RUC_RE = re.compile(r"\b\d{11}\b")
_RFC_RE = re.compile(r"\b[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}\b", re.I)
_MONEY_RE = re.compile(
    r"\$\s*\d{1,3}(?:[.\s]\d{3})*(?:,\d{1,2})?"
    r"|\b\d{1,3}(?:\.\d{3})+\b"
    r"|\b\d+[.,]\d{2}\b"
)
_BBOX_RE = re.compile(r"^\s*\[[\s\d\.,]+\]\s*$")


def word_count(text: str) -> int:
    if not text:
        return 0
    return len([w for w in text.split() if len(w) > 2 and any(c.isalnum() for c in w)])


def is_vlm_method(method: Optional[str]) -> bool:
    m = (method or "").lower()
    return m.startswith("vllm") or "moondream" in m or m.startswith("pdf_vllm")


def document_signal_score(text: str) -> int:
    """Cuántas pistas de boleta/factura hay (RUT, TOTAL, IVA, montos, …)."""
    raw = (text or "").strip()
    if not raw:
        return 0
    lower = raw.lower()
    score = 0
    for kw in _DOCUMENT_KEYWORDS:
        if re.search(r"\b" + re.escape(kw) + r"\b", lower):
            score += 1
    if _NIT_RE.search(raw):
        score += 2
    if _RUT_RE.search(raw):
        score += 2
    if _RUC_RE.search(raw) and re.search(r"\bruc\b", lower):
        score += 2
    if _RFC_RE.search(raw):
        score += 2
    if _MONEY_RE.search(raw):
        score += 1
    return score


def _is_highly_repetitive(text: str) -> bool:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if len(lines) < 5:
        return False
    from collections import Counter

    top_n = Counter(lines).most_common(1)[0][1]
    return top_n >= 5 and (top_n / len(lines)) > 0.4


def looks_like_caption(text: str) -> bool:
    lower = (text or "").lower()
    return any(marker in lower for marker in _CAPTION_MARKERS)


def looks_hallucinated(text: str) -> bool:
    """True si el texto parece inventado / descripción de imagen, no una transcripción."""
    original = (text or "").strip()
    if not original:
        return True
    if _BBOX_RE.match(original):
        return True
    if looks_like_caption(original):
        return True
    if _is_highly_repetitive(original):
        return True
    words = word_count(original)
    signal = document_signal_score(original)
    if words >= MIN_WORDS and signal == 0:
        return True
    return False


def pick_ocr_result(candidates: Iterable[Dict]) -> Dict:
    """
    Elige el mejor candidato.

    OCR clásico (RapidOCR / Tesseract / EasyOCR) gana si tiene señal de
    documento o suficiente texto. El VLM solo se usa si no está alucinado
    y el OCR clásico no sirvió. Si lo único que hay es alucinación, vacío.
    """
    normalized: List[Dict] = []
    for raw in candidates or []:
        text = (raw.get("text") or "").strip()
        method = raw.get("method") or "unknown"
        words = raw.get("words")
        if words is None:
            words = word_count(text)
        signal = raw.get("signal")
        if signal is None:
            signal = document_signal_score(text)
        hallucinated = raw.get("hallucinated")
        if hallucinated is None:
            hallucinated = bool(text) and is_vlm_method(method) and looks_hallucinated(text)
        normalized.append(
            {
                "text": text,
                "method": method,
                "words": int(words),
                "signal": int(signal),
                "hallucinated": bool(hallucinated),
            }
        )

    usable = [c for c in normalized if c["text"] and not c["hallucinated"]]
    empty = {
        "text": "",
        "method": "none",
        "words": 0,
        "signal": 0,
        "hallucinated": False,
    }
    if not usable:
        return empty

    classical = [c for c in usable if not is_vlm_method(c["method"])]
    vlm = [c for c in usable if is_vlm_method(c["method"])]

    def rank(c: Dict) -> tuple:
        return (c["signal"], c["words"])

    classical_good = [
        c for c in classical if c["signal"] > 0 or c["words"] >= MIN_WORDS
    ]
    if classical_good:
        return max(classical_good, key=rank)
    if vlm:
        return max(vlm, key=rank)
    if classical:
        return max(classical, key=rank)
    return empty
