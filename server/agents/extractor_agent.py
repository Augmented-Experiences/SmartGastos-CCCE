"""
Agente Extractor para extracción de campos clave de documentos contables.
Estrategia de dos capas:
  1. Regex/heurísticas → extracción rápida y confiable sin dependencias
  2. LLM Ollama (opcional) → mejora y completa los campos cuando está disponible
"""
import os
import json
import re
from typing import Dict, Optional
from datetime import datetime
import requests

from ollama_client import resolve_ollama_model

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
TIMEOUT_EXTRACTION = int(os.getenv("OLLAMA_TIMEOUT_EXTRACTION", "60"))


class ExtractorAgent:
    """Agente especializado en extracción de campos clave de documentos contables."""

    def __init__(self, model: str = "llama3.2:3b"):
        self.model = model
        self.system_prompt = self._get_system_prompt()

    def _get_system_prompt(self) -> str:
        return """Eres un experto contador y procesador de documentos contables.
Tu tarea es extraer información clave de documentos contables colombianos (factura electrónica DIAN, documento equivalente POS, notas crédito/débito, cuentas de cobro).
IMPORTANTE: Responde SIEMPRE en formato JSON válido, sin explicaciones adicionales.
Campos a extraer:
- proveedor: Nombre de la empresa/persona que emite el documento
- rut_proveedor: NIT del proveedor (formato: XXX.XXX.XXX-X)
- fecha_emision: Fecha del documento (formato: YYYY-MM-DD)
- folio: Número de factura, consecutivo o prefijo-número
- monto_neto: Base gravable / subtotal (número sin separadores de miles)
- iva: Monto del IVA (19%, 5% o 0 si exento)
- monto_total: Monto total (número sin separadores de miles)
- moneda: Código de moneda (COP, USD, EUR, etc.)
- tipo_documento: Tipo (Factura, Documento equivalente, Nota de crédito, Comprobante, Otro)
- observaciones: Notas relevantes
Si no encuentras un campo, usa null. Normaliza montos a números enteros sin puntos ni comas.
Responde SOLO con JSON, sin markdown, sin explicaciones."""

    # ── Punto de entrada principal ────────────────────────────────────────────

    def extract_fields(self, text: str) -> Dict:
        """
        Extrae campos del texto de un documento.
        Siempre retorna un dict con todos los campos (pueden ser None).
        """
        if not text or len(text.strip()) < 5:
            return self._get_empty_fields()

        # Capa 1: extracción por regex (siempre funciona)
        regex_fields = self._extract_by_regex(text)

        # Capa 2: mejorar con LLM si Ollama está disponible
        if self._ollama_available():
            try:
                llm_fields = self._extract_by_llm(text)
                # Fusionar: LLM gana si tiene valor, regex como fallback
                merged = self._merge_fields(regex_fields, llm_fields)
                return self._validate_fields(merged)
            except Exception as e:
                print(f"INFO: LLM no disponible para extracción ({e}), usando regex")

        return self._validate_fields(regex_fields)

    # ── Capa 1: Extracción por Regex ──────────────────────────────────────────

    def _extract_by_regex(self, text: str) -> Dict:
        """Extrae campos usando expresiones regulares y heurísticas."""
        fields: Dict = {}

        # ── NIT colombiano / RUT / RUC ─────────────────────────────────────
        rut_patterns = [
            r"N\.?I\.?T\.?\s*[:\-]?\s*(\d{3}\.?\d{3}\.?\d{3}-?\d)",
            r"(?:nit|NIT)\s*[:\-]?\s*(\d{6,10}-?\d)",
            r"R\.?U\.?T\.?\s*[:\-]?\s*(\d{1,2}[\.\-]?\d{3}[\.\-]?\d{3}[\-]?[0-9Kk])",
            r"(?:rut|RUT)\s*[:\-]?\s*(\d{1,2}\.?\d{3}\.?\d{3}-?[0-9Kk])",
        ]
        for pat in rut_patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                fields["rut_proveedor"] = m.group(1)
                break

        # ── Folio / N° de documento ────────────────────────────────────────
        folio_patterns = [
            r"(?:folio|N°|No\.?|Número|numero|consecutivo|factura\s*n[°º]?)\s*[:\-]?\s*(\d{3,10})",
            r"(?:cufe|prefijo)\s*[:\-]?\s*([A-Za-z0-9\-]{6,20})",
            r"(?:boleta|recibo|pos)\s*n[°º]?\s*[:\-]?\s*(\d{3,10})",
        ]
        for pat in folio_patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                fields["folio"] = m.group(1)
                break

        # ── Fecha ──────────────────────────────────────────────────────────
        date_patterns = [
            r"(?:fecha|date|emisi[oó]n)\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})",
            r"(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{4})",
            r"(\d{4}[\/\-\.]\d{1,2}[\/\-\.]\d{1,2})",
        ]
        for pat in date_patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                fields["fecha_emision"] = m.group(1)
                break

        # ── Montos ─────────────────────────────────────────────────────────
        # Total / Monto total
        total_patterns = [
            r"(?:total|monto\s*total|importe\s*total)\s*[:\$]?\s*\$?\s*([\d\.,]+)",
            r"(?:total\s*a\s*pagar|total\s*factura)\s*[:\$]?\s*\$?\s*([\d\.,]+)",
        ]
        for pat in total_patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                fields["monto_total"] = self._parse_amount(m.group(1))
                break

        # Neto
        neto_patterns = [
            r"(?:neto|monto\s*neto|subtotal|base\s*imponible)\s*[:\$]?\s*\$?\s*([\d\.,]+)",
        ]
        for pat in neto_patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                fields["monto_neto"] = self._parse_amount(m.group(1))
                break

        # IVA
        iva_patterns = [
            r"(?:iva|i\.v\.a|impuesto)\s*(?:\d{1,2}%?)?\s*[:\$]?\s*\$?\s*([\d\.,]+)",
        ]
        for pat in iva_patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                fields["iva"] = self._parse_amount(m.group(1))
                break

        # ── Tipo de documento ──────────────────────────────────────────────
        text_upper = text.upper()
        if "DOCUMENTO EQUIVALENTE" in text_upper or "DOCUMENTO POS" in text_upper:
            fields["tipo_documento"] = "Documento equivalente"
        elif "FACTURA" in text_upper:
            fields["tipo_documento"] = "Factura"
        elif "BOLETA" in text_upper:
            fields["tipo_documento"] = "Boleta"
        elif "NOTA DE CRÉDITO" in text_upper or "NOTA DE CREDITO" in text_upper:
            fields["tipo_documento"] = "Nota de Crédito"
        elif "NOTA DE DÉBITO" in text_upper or "NOTA DE DEBITO" in text_upper:
            fields["tipo_documento"] = "Nota de Débito"
        elif "CARTOLA" in text_upper or "ESTADO DE CUENTA" in text_upper:
            fields["tipo_documento"] = "Cartola"
        elif "RECIBO" in text_upper:
            fields["tipo_documento"] = "Recibo"
        else:
            fields["tipo_documento"] = "Otro"

        # ── Proveedor (heurística: primera línea con mayúsculas y S.A./SpA/Ltda) ──
        proveedor_patterns = [
            r"(?:emisor|proveedor|empresa|razón\s*social|razon\s*social)\s*[:\-]?\s*([A-ZÁÉÍÓÚÑ][^\n]{3,60})",
            r"^([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑA-Za-záéíóúñ\s\.\,]{5,50}(?:S\.?A\.?S?\.?|SAS|Ltda\.?|LTDA|SRL|E\.U\.))",
        ]
        for pat in proveedor_patterns:
            m = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
            if m:
                candidate = m.group(1).strip()
                if len(candidate) >= 4:
                    fields["proveedor"] = candidate
                    break

        # Si no encontramos proveedor con patrones, tomar primera línea no vacía
        if not fields.get("proveedor"):
            lines = [l.strip() for l in text.split("\n") if len(l.strip()) > 5]
            if lines:
                fields["proveedor"] = lines[0][:80]

        # ── Moneda ─────────────────────────────────────────────────────────
        if re.search(r"\bUSD\b|\$\s*USD|\bDólares?\b", text, re.IGNORECASE):
            fields["moneda"] = "USD"
        elif re.search(r"\bEUR\b|\bEuros?\b", text, re.IGNORECASE):
            fields["moneda"] = "EUR"
        elif re.search(r"\bCLP\b|pesos?\s+chile", text, re.IGNORECASE):
            fields["moneda"] = "CLP"
        else:
            fields["moneda"] = "COP"

        # ── Observaciones / Descripción ────────────────────────────────────
        obs_patterns = [
            r"(?:descripci[oó]n|detalle|concepto|glosa|observaci[oó]n|servicio)\s*[:\-]?\s*(.{5,120})",
        ]
        for pat in obs_patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                candidate = m.group(1).strip()
                # Exclude lines that look like amounts
                if not re.match(r"^[\$\d\.\,]+$", candidate):
                    fields["observaciones"] = candidate[:200]
                    break

        return fields

    # ── Capa 2: LLM Ollama ────────────────────────────────────────────────────

    def _ollama_available(self) -> bool:
        """Verifica si Ollama está disponible con un timeout corto."""
        try:
            r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
            return r.status_code == 200
        except Exception:
            return False

    def _extract_by_llm(self, text: str) -> Dict:
        """Extrae campos usando LLM Ollama."""
        text_limited = text[:3000]
        response = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": resolve_ollama_model(self.model),
                "messages": [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": f"Extrae los campos de este documento:\n\n{text_limited}"}
                ],
                "options": {"temperature": 0.1},
                "stream": False
            },
            timeout=TIMEOUT_EXTRACTION
        )
        response.encoding = "utf-8"
        response.raise_for_status()
        content = response.json()["message"]["content"]
        return self._parse_json_response(content)

    def _parse_json_response(self, response: str) -> Dict:
        """Parsea respuesta JSON del LLM con tolerancia a errores."""
        try:
            clean = re.sub(r"```(?:json)?\s*", "", response).strip().rstrip("`").strip()
            return json.loads(clean)
        except json.JSONDecodeError:
            start = response.find("{")
            end = response.rfind("}") + 1
            if start != -1 and end > start:
                try:
                    return json.loads(response[start:end])
                except Exception:
                    pass
        return {}

    def _merge_fields(self, base: Dict, override: Dict) -> Dict:
        """Fusiona campos: override gana si tiene valor no nulo."""
        merged = dict(base)
        for key, val in override.items():
            if val is not None and str(val).strip():
                merged[key] = val
        return merged

    # ── Validación y normalización ────────────────────────────────────────────

    def _validate_fields(self, fields: Dict) -> Dict:
        """Valida y normaliza todos los campos."""
        validated = {
            "proveedor": self._clean_string(fields.get("proveedor")),
            "rut_proveedor": self._clean_rut(fields.get("rut_proveedor")),
            "fecha_emision": self._parse_date(fields.get("fecha_emision")),
            "folio": self._clean_string(fields.get("folio")),
            "monto_neto": self._parse_amount(fields.get("monto_neto")),
            "iva": self._parse_amount(fields.get("iva")),
            "monto_total": self._parse_amount(fields.get("monto_total")),
            "moneda": self._clean_currency(fields.get("moneda", "COP")),
            "tipo_documento": self._clean_string(fields.get("tipo_documento")),
            "observaciones": self._clean_string(fields.get("observaciones"))
        }

        # Inferir neto o IVA si falta uno
        if validated["monto_total"] and validated["monto_neto"] and not validated["iva"]:
            validated["iva"] = round(validated["monto_total"] - validated["monto_neto"], 2)
        elif validated["monto_total"] and validated["iva"] and not validated["monto_neto"]:
            validated["monto_neto"] = round(validated["monto_total"] - validated["iva"], 2)
        elif validated["monto_neto"] and validated["iva"] and not validated["monto_total"]:
            validated["monto_total"] = round(validated["monto_neto"] + validated["iva"], 2)

        return validated

    def _clean_string(self, value) -> Optional[str]:
        if value is None:
            return None
        s = str(value).strip()
        return s if s else None

    def _clean_rut(self, value) -> Optional[str]:
        if value is None:
            return None
        rut = re.sub(r"[^\d\-Kk\.]", "", str(value)).upper()
        # Normalizar: remover puntos, asegurar guión
        digits = re.sub(r"[^\dKk]", "", rut)
        if len(digits) >= 7:
            body = digits[:-1]
            dv = digits[-1]
            # Formatear con puntos
            body_fmt = ""
            for i, c in enumerate(reversed(body)):
                if i > 0 and i % 3 == 0:
                    body_fmt = "." + body_fmt
                body_fmt = c + body_fmt
            return f"{body_fmt}-{dv}"
        return None

    def _parse_date(self, value) -> Optional[str]:
        if value is None:
            return None
        date_str = str(value).strip()
        formats = [
            "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y",
            "%d/%m/%y", "%Y/%m/%d", "%d.%m.%Y"
        ]
        for fmt in formats:
            try:
                dt = datetime.strptime(date_str, fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
        return None

    def _parse_amount(self, value) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value) if value >= 0 else None
        try:
            s = str(value).strip()
            # Eliminar símbolo de moneda y espacios
            s = re.sub(r"[^\d\.,]", "", s)
            if not s:
                return None
            # Detectar si usa punto como separador de miles (ej: 1.234.567)
            if re.match(r"^\d{1,3}(\.\d{3})+$", s):
                s = s.replace(".", "")
            elif "," in s and "." in s:
                # Formato europeo: 1.234,56
                s = s.replace(".", "").replace(",", ".")
            elif "," in s:
                s = s.replace(",", ".")
            amount = float(s)
            return amount if amount >= 0 else None
        except (ValueError, AttributeError):
            return None

    def _clean_currency(self, value) -> str:
        if value is None:
            return "COP"
        code = str(value).strip().upper()
        if re.match(r"^[A-Z]{3}$", code):
            return code
        return "COP"

    def _get_empty_fields(self) -> Dict:
        return {
            "proveedor": None, "rut_proveedor": None, "fecha_emision": None,
            "folio": None, "monto_neto": None, "iva": None, "monto_total": None,
            "moneda": "COP", "tipo_documento": None, "observaciones": None
        }

    def batch_extract(self, documents: list) -> list:
        results = []
        for i, text in enumerate(list(documents)[:30]):
            result = self.extract_fields(text)
            result["_index"] = i
            results.append(result)
        return results


# ── Singleton ─────────────────────────────────────────────────────────────────
extractor_agent = None


def get_extractor_agent(model: str = "llama3.2:3b") -> ExtractorAgent:
    global extractor_agent
    if extractor_agent is None:
        extractor_agent = ExtractorAgent(model)
    return extractor_agent
