"""
Agente Clasificador para clasificación automática de gastos.
Estrategia de tres capas:
  1. Matching directo contra nombres de categorías del usuario (siempre funciona)
  2. Keywords semánticas por tipo de gasto (sin dependencias)
  3. LLM Ollama (opcional) para clasificación semántica avanzada
"""
import os
import json
import re
from typing import Dict, List, Optional
from difflib import SequenceMatcher
import requests

from ollama_client import resolve_ollama_model

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
TIMEOUT_CLASSIFICATION = int(os.getenv("OLLAMA_TIMEOUT_CLASSIFICATION", "60"))

# ── Diccionario semántico de keywords por categoría genérica ─────────────────
# Cuando el usuario no tiene categorías configuradas, usamos estas como base
SEMANTIC_KEYWORDS: Dict[str, List[str]] = {
    "Operaciones": [
        "operacion", "operaciones", "suministro", "insumo", "materiales",
        "ferreteria", "herramienta", "mantención", "mantencion", "reparacion",
        "limpieza", "aseo", "seguridad", "vigilancia", "transporte", "flete",
        "courier", "envio", "bodega", "almacen", "logistica"
    ],
    "Marketing": [
        "marketing", "publicidad", "propaganda", "diseño", "diseño grafico",
        "imprenta", "impresion", "banner", "flyer", "redes sociales",
        "facebook", "instagram", "google", "ads", "pauta", "campaña",
        "agencia", "community", "fotografia", "video", "contenido"
    ],
    "Tecnología": [
        "tecnologia", "software", "hardware", "computador", "laptop",
        "servidor", "hosting", "dominio", "licencia", "suscripcion",
        "microsoft", "google workspace", "adobe", "zoom", "slack",
        "internet", "fibra", "datos", "nube", "cloud", "it", "soporte tecnico"
    ],
    "Servicios Básicos": [
        "agua", "luz", "electricidad", "gas", "telefono", "celular",
        "movil", "fijo", "epm", "codensa", "enel", "vanti", "acueducto",
        "etb", "tigo", "claro", "movistar", "wom"
    ],
    "Arriendo": [
        "arriendo", "alquiler", "renta", "arrendamiento", "local",
        "oficina", "bodega", "inmueble", "propiedad", "habitacion"
    ],
    "Sueldos y RRHH": [
        "sueldo", "salario", "remuneracion", "honorario", "boleta honorario",
        "pension", "salud", "arl", "cesantias", "prima", "caja compensacion",
        "nomina", "finiquito", "liquidacion", "rrhh",
        "recursos humanos", "personal", "empleado", "trabajador"
    ],
    "Impuestos y Legal": [
        "impuesto", "iva", "renta", "dian", "ica", "retefuente", "reteiva",
        "reteica", "tesoreria", "industria y comercio", "notaria", "abogado", "legal",
        "contabilidad", "contador", "auditoria", "multa", "infraccion"
    ],
    "Financiero": [
        "banco", "credito", "prestamo", "interes", "comision bancaria",
        "transferencia", "giro", "cheque", "leasing", "factoring",
        "seguro", "poliza", "prima", "deuda", "cuota"
    ],
    "Alimentación": [
        "alimentacion", "comida", "restaurante", "casino", "colacion",
        "supermercado", "almacen", "panaderia", "cafe", "catering",
        "exito", "olimpica", "carulla", "d1", "ara", "jumbo", "alkosto"
    ],
    "Capacitación": [
        "capacitacion", "curso", "taller", "seminario", "conferencia",
        "entrenamiento", "formacion", "educacion", "certificacion",
        "diploma", "universidad", "instituto", "escuela"
    ],
}


class ClassifierAgent:
    """Agente especializado en clasificación de gastos contables."""

    def __init__(self, model: str = "llama3.2:3b"):
        self.model = model
        self.system_prompt = self._get_system_prompt()

    def _get_system_prompt(self) -> str:
        return """Eres un experto contador especializado en clasificación de gastos.
Tu tarea es clasificar gastos en categorías contables.
Responde SIEMPRE en formato JSON válido:
{
  "categoria_sugerida": "Nombre exacto de una de las categorías disponibles",
  "confianza": 0.85,
  "razon": "Breve explicación de por qué elegiste esa categoría",
  "centro_costo_sugerido": null,
  "excepciones": []
}
Confianza: 0.0 a 1.0. Responde SOLO con JSON, sin markdown."""

    # ── Punto de entrada principal ────────────────────────────────────────────

    def classify(self, document_data: Dict, categories: List[Dict],
                 cost_centers: List[Dict]) -> Dict:
        """
        Clasifica un documento. Siempre retorna un resultado con categoría sugerida.
        Si no hay categorías configuradas, usa el diccionario semántico interno.
        """
        proveedor = (document_data.get("proveedor") or "").lower()
        tipo_doc = (document_data.get("tipo_documento") or "").lower()
        observaciones = (document_data.get("observaciones") or "").lower()
        search_text = f"{proveedor} {observaciones} {tipo_doc}"

        # Paso 1: matching directo contra nombres de categorías del usuario
        if categories:
            direct = self._match_against_user_categories(search_text, categories)
            if direct.get("confianza", 0) >= 0.6:
                direct = self._add_cost_center(direct, cost_centers)
                return direct

        # Paso 2: keywords semánticas internas
        semantic = self._classify_by_semantic_keywords(search_text, categories)
        if semantic.get("confianza", 0) >= 0.5:
            semantic = self._add_cost_center(semantic, cost_centers)
            # Si hay categorías del usuario, intentar mapear el nombre genérico
            if categories:
                semantic = self._map_to_user_category(semantic, categories)
            return semantic

        # Paso 3: LLM (si disponible)
        if self._ollama_available():
            try:
                llm_result = self._classify_by_llm(document_data, categories, cost_centers)
                if llm_result.get("categoria_sugerida"):
                    return llm_result
            except Exception as e:
                print(f"INFO: LLM clasificación no disponible ({e})")

        # Paso 4: fallback — usar primera categoría disponible con confianza baja
        if categories:
            return {
                "categoria_sugerida": categories[0].get("nombre"),
                "confianza": 0.3,
                "razon": "Clasificación por defecto (no se pudo determinar categoría)",
                "centro_costo_sugerido": None,
                "excepciones": ["Requiere revisión manual"]
            }

        return {
            "categoria_sugerida": "Sin categoría",
            "confianza": 0.0,
            "razon": "No hay categorías configuradas",
            "centro_costo_sugerido": None,
            "excepciones": ["Configure categorías en Administración"]
        }

    # ── Capa 1: Matching contra categorías del usuario ────────────────────────

    def _match_against_user_categories(self, search_text: str,
                                        categories: List[Dict]) -> Dict:
        """
        Busca coincidencias entre el texto del documento y los nombres
        de las categorías configuradas por el usuario.
        """
        best_score = 0.0
        best_cat = None

        for cat in categories:
            cat_name = (cat.get("nombre") or "").lower()
            if not cat_name:
                continue

            # Coincidencia exacta
            if cat_name in search_text:
                return {
                    "categoria_sugerida": cat.get("nombre"),
                    "confianza": 0.92,
                    "razon": f"Coincidencia directa con categoría '{cat.get('nombre')}'",
                    "centro_costo_sugerido": None,
                    "excepciones": []
                }

            # Similitud de cadena
            score = SequenceMatcher(None, cat_name, search_text[:len(cat_name) + 10]).ratio()
            if score > best_score:
                best_score = score
                best_cat = cat

            # Palabras individuales del nombre de categoría
            words = cat_name.split()
            matches = sum(1 for w in words if len(w) > 3 and w in search_text)
            word_score = matches / max(len(words), 1)
            if word_score > best_score:
                best_score = word_score
                best_cat = cat

        if best_cat and best_score >= 0.4:
            return {
                "categoria_sugerida": best_cat.get("nombre"),
                "confianza": min(0.85, best_score),
                "razon": f"Similitud con categoría '{best_cat.get('nombre')}'",
                "centro_costo_sugerido": None,
                "excepciones": []
            }

        return {"confianza": 0.0}

    # ── Capa 2: Keywords semánticas internas ──────────────────────────────────

    def _classify_by_semantic_keywords(self, search_text: str,
                                        categories: List[Dict]) -> Dict:
        """Clasifica usando el diccionario semántico interno."""
        best_category = None
        best_score = 0
        best_matches = 0

        for category_name, keywords in SEMANTIC_KEYWORDS.items():
            matches = sum(1 for kw in keywords if kw in search_text)
            if matches > best_matches:
                best_matches = matches
                best_category = category_name
                # Score proporcional a matches y longitud del keyword
                best_score = min(0.9, 0.4 + (matches * 0.15))

        if best_category and best_matches > 0:
            return {
                "categoria_sugerida": best_category,
                "confianza": best_score,
                "razon": f"Clasificación semántica: {best_matches} coincidencias",
                "centro_costo_sugerido": None,
                "excepciones": []
            }

        return {"confianza": 0.0}

    def _map_to_user_category(self, result: Dict, categories: List[Dict]) -> Dict:
        """
        Intenta mapear una categoría genérica al nombre exacto
        de una categoría configurada por el usuario.
        """
        generic_name = (result.get("categoria_sugerida") or "").lower()
        if not generic_name or not categories:
            return result

        for cat in categories:
            cat_name = (cat.get("nombre") or "").lower()
            # Coincidencia parcial
            if generic_name in cat_name or cat_name in generic_name:
                result["categoria_sugerida"] = cat.get("nombre")
                return result
            # Palabras clave compartidas
            generic_words = set(generic_name.split())
            cat_words = set(cat_name.split())
            if generic_words & cat_words:
                result["categoria_sugerida"] = cat.get("nombre")
                return result

        # Si no hay mapeo, mantener el nombre genérico
        return result

    # ── Capa 3: LLM Ollama ────────────────────────────────────────────────────

    def _ollama_available(self) -> bool:
        try:
            r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
            return r.status_code == 200
        except Exception:
            return False

    def _classify_by_llm(self, document_data: Dict, categories: List[Dict],
                          cost_centers: List[Dict]) -> Dict:
        """Clasificación usando LLM local."""
        proveedor = document_data.get("proveedor") or "Desconocido"
        monto = document_data.get("monto_total") or 0
        observaciones = document_data.get("observaciones") or ""

        categories_text = "\n".join([
            f"- {c.get('nombre')}" for c in categories[:15]
        ]) or "Sin categorías configuradas"

        cost_centers_text = "\n".join([
            f"- {cc.get('codigo')}: {cc.get('nombre')}" for cc in cost_centers[:10]
        ]) or "Sin centros de costo"

        prompt = f"""Clasifica este gasto:
Proveedor: {proveedor}
Monto: {monto}
Observaciones: {observaciones}

Categorías disponibles (elige UNA de estas exactamente):
{categories_text}

Centros de costo:
{cost_centers_text}"""

        response = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": resolve_ollama_model(self.model),
                "messages": [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": prompt}
                ],
                "options": {"temperature": 0.2},
                "stream": False
            },
            timeout=TIMEOUT_CLASSIFICATION
        )
        response.encoding = "utf-8"
        response.raise_for_status()
        content = response.json()["message"]["content"]
        return self._parse_classification_response(content)

    def _parse_classification_response(self, response: str) -> Dict:
        try:
            clean = re.sub(r"```(?:json)?\s*", "", response).strip().rstrip("`").strip()
            result = json.loads(clean)
            result.setdefault("categoria_sugerida", None)
            result.setdefault("confianza", 0.5)
            result.setdefault("razon", "")
            result.setdefault("centro_costo_sugerido", None)
            result.setdefault("excepciones", [])
            result["confianza"] = max(0.0, min(1.0, float(result.get("confianza", 0.5))))
            return result
        except Exception:
            start = response.find("{")
            end = response.rfind("}") + 1
            if start != -1 and end > start:
                try:
                    return json.loads(response[start:end])
                except Exception:
                    pass
            return {
                "categoria_sugerida": None, "confianza": 0.0,
                "razon": "Error al parsear respuesta LLM",
                "centro_costo_sugerido": None, "excepciones": ["Error de parseo"]
            }

    # ── Utilidades ────────────────────────────────────────────────────────────

    def _add_cost_center(self, result: Dict, cost_centers: List[Dict]) -> Dict:
        """Sugiere centro de costo si no hay uno asignado."""
        if result.get("centro_costo_sugerido") or not cost_centers:
            return result
        cat_name = (result.get("categoria_sugerida") or "").lower()
        for cc in cost_centers:
            if cat_name in (cc.get("nombre") or "").lower():
                result["centro_costo_sugerido"] = cc.get("codigo")
                return result
        return result

    def batch_classify(self, documents: list, categories: list,
                       cost_centers: list) -> list:
        results = []
        for doc in documents:
            result = self.classify(doc, categories, cost_centers)
            result["_document_id"] = doc.get("_id")
            results.append(result)
        return results


# ── Singleton ─────────────────────────────────────────────────────────────────
classifier_agent = None


def get_classifier_agent(model: str = "llama3.2:3b") -> ClassifierAgent:
    global classifier_agent
    if classifier_agent is None:
        classifier_agent = ClassifierAgent(model)
    return classifier_agent
