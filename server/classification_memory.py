"""
Módulo de memoria de clasificaciones — Contexto enriquecido por proveedor.

Persiste las correcciones de clasificación del usuario en disco
y las inyecta como contexto en el prompt del clasificador.

MEJORAS v1.5:
  - Guarda contexto enriquecido: RUT proveedor, texto OCR parcial, montos,
    tipo de documento, y señales adicionales de la boleta.
  - Permite al clasificador "recordar" proveedores por múltiples señales,
    no solo por nombre exacto.
  - Soporta auto-aprendizaje: cuando el pipeline clasifica exitosamente
    un documento, guarda el contexto para futuras clasificaciones similares.
"""
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict

logger = logging.getLogger(__name__)
from security import sanitize_log, storage_file

# Directorio de datos (se configura al importar)
_DATA_DIR = Path(__file__).parent.parent / "data"

# Límites de tamaño para evitar DoS de disco
MAX_HUMAN_LEARNINGS = 500  # Máximo de clasificaciones humanas por empresa
MAX_AUTO_LEARNINGS = 300   # Máximo de clasificaciones automáticas por empresa
MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB máximo por archivo de aprendizaje


def set_data_dir(data_dir: Path):
    """Configura el directorio de datos."""
    global _DATA_DIR
    _DATA_DIR = data_dir


def save_classification_learning(
    empresa_id: str, proveedor: str, descripcion: str,
    tipo_documento: str, new_cat_name: str, new_categoria_id: str,
    rut_proveedor: str = "",
    texto_ocr: str = "",
    monto_total: Optional[float] = None,
    señales_extra: Optional[Dict] = None
):
    """
    Guarda la corrección de clasificación del usuario como aprendizaje.
    Se persiste en un archivo JSON que el clasificador usa como contexto
    para futuras clasificaciones similares.

    Parámetros enriquecidos:
      - rut_proveedor: RUT/NIT del proveedor (señal fuerte de identidad)
      - texto_ocr: Primeros 300 chars del texto OCR (para matching semántico)
      - monto_total: Monto del documento (para contexto de rango)
      - señales_extra: Dict con señales adicionales (folio, moneda, etc.)
    """
    # Archivo de aprendizaje por empresa
    learn_dir = _DATA_DIR / "learning"
    learn_file = storage_file(learn_dir, "classifications_", empresa_id, ".json")
    if learn_file is None:
        return

    # Cargar aprendizajes existentes
    learnings = []
    if learn_file.exists():
        try:
            learnings = json.loads(learn_file.read_text(encoding="utf-8"))[:MAX_HUMAN_LEARNINGS]
        except Exception:
            learnings = []

    # Normalizar proveedor para búsqueda
    proveedor_norm = proveedor.strip().lower()[:100] if proveedor else ""
    desc_norm = descripcion.strip().lower()[:200] if descripcion else ""
    rut_norm = rut_proveedor.strip().replace(".", "").replace("-", "").lower()[:20] if rut_proveedor else ""
    texto_ocr_norm = texto_ocr.strip().lower()[:300] if texto_ocr else ""

    # Buscar si ya existe un aprendizaje para este proveedor+tipo O por RUT
    found = False
    for entry in learnings:
        # Match por RUT (señal más fuerte) o por proveedor+tipo
        match_by_rut = (rut_norm and entry.get("rut_norm") == rut_norm)
        match_by_name = (
            entry.get("proveedor_norm") == proveedor_norm and
            entry.get("tipo_documento") == tipo_documento
        )
        if match_by_rut or match_by_name:
            # Actualizar la categoría y enriquecer contexto
            entry["categoria"] = new_cat_name
            entry["categoria_id"] = new_categoria_id
            entry["veces_corregido"] = entry.get("veces_corregido", 0) + 1
            entry["ultima_correccion"] = datetime.utcnow().isoformat()
            # Enriquecer con nuevos datos si están disponibles
            if rut_norm and not entry.get("rut_norm"):
                entry["rut_proveedor"] = rut_proveedor
                entry["rut_norm"] = rut_norm
            if texto_ocr_norm and not entry.get("texto_ocr_muestra"):
                entry["texto_ocr_muestra"] = texto_ocr_norm[:200]
            if monto_total and monto_total > 0:
                # Guardar rango de montos vistos para este proveedor
                montos = entry.get("montos_vistos", [])
                montos.append(monto_total)
                # Mantener solo últimos 10 montos
                entry["montos_vistos"] = montos[-10:]
            if señales_extra:
                existing_signals = entry.get("señales_extra", {})
                existing_signals.update(señales_extra)
                entry["señales_extra"] = existing_signals
            found = True
            break

    if not found:
        new_entry = {
            "proveedor": proveedor[:100] if proveedor else "",
            "proveedor_norm": proveedor_norm,
            "rut_proveedor": rut_proveedor[:20] if rut_proveedor else "",
            "rut_norm": rut_norm,
            "descripcion_ejemplo": desc_norm[:200],
            "texto_ocr_muestra": texto_ocr_norm[:200],
            "tipo_documento": tipo_documento,
            "categoria": new_cat_name,
            "categoria_id": new_categoria_id,
            "veces_corregido": 1,
            "montos_vistos": [monto_total] if monto_total and monto_total > 0 else [],
            "señales_extra": señales_extra or {},
            "fecha_primera": datetime.utcnow().isoformat(),
            "ultima_correccion": datetime.utcnow().isoformat()
        }
        learnings.append(new_entry)

    # Guardar (máximo MAX_HUMAN_LEARNINGS entradas, las más recientes primero)
    learnings.sort(key=lambda x: x.get("ultima_correccion", ""), reverse=True)
    learnings = learnings[:MAX_HUMAN_LEARNINGS]
    
    # Verificar tamaño antes de escribir
    content = json.dumps(learnings, ensure_ascii=False, indent=2)
    if len(content.encode('utf-8')) > MAX_FILE_SIZE_BYTES:
        # Reducir entradas hasta que quepa
        while len(content.encode('utf-8')) > MAX_FILE_SIZE_BYTES and len(learnings) > 50:
            learnings = learnings[:len(learnings) // 2]
            content = json.dumps(learnings, ensure_ascii=False, indent=2)
        logger.warning(f"Archivo de aprendizaje truncado a {len(learnings)} entradas por límite de tamaño")
    
    learn_file.write_text(content, encoding="utf-8")
    logger.info(f"Aprendizaje guardado: '{sanitize_log(proveedor)}' → '{sanitize_log(new_cat_name)}' (total: {len(learnings)})")


def save_auto_classification_context(
    empresa_id: str,
    proveedor: str,
    rut_proveedor: str,
    tipo_documento: str,
    categoria_nombre: str,
    categoria_id: str,
    texto_ocr: str = "",
    monto_total: Optional[float] = None,
    confianza: float = 0.0
):
    """
    Guarda contexto de clasificación AUTOMÁTICA exitosa (no corrección humana).
    Se usa cuando el pipeline clasifica con alta confianza (>= 0.7) para
    que el clasificador recuerde el patrón proveedor→categoría.

    Solo guarda si la confianza es suficiente para evitar contaminar
    la memoria con clasificaciones dudosas.
    """
    if confianza < 0.7:
        return  # No guardar clasificaciones de baja confianza

    # Usar la misma función pero sin incrementar veces_corregido artificialmente
    learn_dir = _DATA_DIR / "learning"
    auto_file = storage_file(learn_dir, "auto_context_", empresa_id, ".json")
    if auto_file is None:
        return

    # Cargar contexto automático existente
    auto_learnings = []
    if auto_file.exists():
        try:
            auto_learnings = json.loads(auto_file.read_text(encoding="utf-8"))[:MAX_AUTO_LEARNINGS]
        except Exception:
            auto_learnings = []

    proveedor_norm = proveedor.strip().lower()[:100] if proveedor else ""
    rut_norm = rut_proveedor.strip().replace(".", "").replace("-", "").lower()[:20] if rut_proveedor else ""

    # Buscar si ya existe
    found = False
    for entry in auto_learnings:
        match_by_rut = (rut_norm and entry.get("rut_norm") == rut_norm)
        match_by_name = (entry.get("proveedor_norm") == proveedor_norm and proveedor_norm)
        if match_by_rut or match_by_name:
            entry["veces_visto"] = entry.get("veces_visto", 0) + 1
            entry["ultima_vez"] = datetime.utcnow().isoformat()
            entry["confianza_promedio"] = (
                (entry.get("confianza_promedio", confianza) * entry.get("veces_visto", 1) + confianza) /
                (entry.get("veces_visto", 1) + 1)
            )
            if monto_total and monto_total > 0:
                montos = entry.get("montos_vistos", [])
                montos.append(monto_total)
                entry["montos_vistos"] = montos[-10:]
            found = True
            break

    if not found:
        auto_learnings.append({
            "proveedor": proveedor[:100] if proveedor else "",
            "proveedor_norm": proveedor_norm,
            "rut_proveedor": rut_proveedor[:20] if rut_proveedor else "",
            "rut_norm": rut_norm,
            "tipo_documento": tipo_documento,
            "categoria": categoria_nombre,
            "categoria_id": categoria_id,
            "texto_ocr_muestra": (texto_ocr or "").strip().lower()[:200],
            "confianza_promedio": confianza,
            "montos_vistos": [monto_total] if monto_total and monto_total > 0 else [],
            "veces_visto": 1,
            "fecha_primera": datetime.utcnow().isoformat(),
            "ultima_vez": datetime.utcnow().isoformat()
        })

    # Guardar (máximo MAX_AUTO_LEARNINGS entradas)
    auto_learnings.sort(key=lambda x: x.get("veces_visto", 0), reverse=True)
    auto_learnings = auto_learnings[:MAX_AUTO_LEARNINGS]
    
    # Verificar tamaño antes de escribir
    content = json.dumps(auto_learnings, ensure_ascii=False, indent=2)
    if len(content.encode('utf-8')) > MAX_FILE_SIZE_BYTES:
        while len(content.encode('utf-8')) > MAX_FILE_SIZE_BYTES and len(auto_learnings) > 50:
            auto_learnings = auto_learnings[:len(auto_learnings) // 2]
            content = json.dumps(auto_learnings, ensure_ascii=False, indent=2)
        logger.warning(f"Auto-aprendizaje truncado a {len(auto_learnings)} entradas")
    
    auto_file.write_text(content, encoding="utf-8")


def get_classification_learnings(empresa_id: str) -> str:
    """
    Retorna las clasificaciones aprendidas como texto para inyectar en el prompt
    del clasificador. Combina correcciones humanas (prioridad alta) y
    contexto automático (prioridad media).
    """
    learn_dir = _DATA_DIR / "learning"
    human_file = storage_file(learn_dir, "classifications_", empresa_id, ".json")
    auto_file = storage_file(learn_dir, "auto_context_", empresa_id, ".json")
    if human_file is None or auto_file is None:
        return ""
    human_learnings = []
    if human_file.exists():
        try:
            human_learnings = json.loads(human_file.read_text(encoding="utf-8"))[:MAX_HUMAN_LEARNINGS]
        except Exception:
            pass

    auto_learnings = []
    if auto_file.exists():
        try:
            auto_learnings = json.loads(auto_file.read_text(encoding="utf-8"))[:MAX_AUTO_LEARNINGS]
        except Exception:
            pass

    if not human_learnings and not auto_learnings:
        return ""

    lines = []

    # Sección de correcciones humanas (top 20)
    if human_learnings:
        human_learnings.sort(key=lambda x: x.get("veces_corregido", 0), reverse=True)
        top_human = human_learnings[:20]

        lines.append("=== CLASIFICACIONES CORREGIDAS POR EL USUARIO (PRIORIDAD MÁXIMA) ===")
        lines.append("El usuario ha corregido estas clasificaciones. SIEMPRE respeta estas decisiones:")
        for entry in top_human:
            prov = entry.get("proveedor", "")
            cat = entry.get("categoria", "")
            tipo = entry.get("tipo_documento", "")
            veces = entry.get("veces_corregido", 1)
            rut = entry.get("rut_proveedor", "")
            desc = entry.get("texto_ocr_muestra") or entry.get("descripcion_ejemplo", "")

            line = f"- Proveedor '{prov}'"
            if rut:
                line += f" (NIT: {rut})"
            if tipo:
                line += f" [tipo: {tipo}]"
            if desc:
                line += f" [texto: {desc[:80]}]"
            line += f" → Categoría: '{cat}' (corregido {veces}x)"
            lines.append(line)
        lines.append("=== FIN CORRECCIONES HUMANAS ===")

    # Sección de contexto automático (top 15, solo los más frecuentes)
    if auto_learnings:
        auto_learnings.sort(key=lambda x: x.get("veces_visto", 0), reverse=True)
        top_auto = [a for a in auto_learnings if a.get("veces_visto", 0) >= 2][:15]

        if top_auto:
            lines.append("")
            lines.append("=== PATRONES DE CLASIFICACIÓN APRENDIDOS AUTOMÁTICAMENTE ===")
            lines.append("Estos proveedores han sido clasificados consistentemente así:")
            for entry in top_auto:
                prov = entry.get("proveedor", "")
                cat = entry.get("categoria", "")
                rut = entry.get("rut_proveedor", "")
                veces = entry.get("veces_visto", 1)
                conf = entry.get("confianza_promedio", 0)

                line = f"- '{prov}'"
                if rut:
                    line += f" (NIT: {rut})"
                line += f" → '{cat}' (visto {veces}x, confianza {conf:.0%})"
                lines.append(line)
            lines.append("=== FIN PATRONES AUTOMÁTICOS ===")

    return "\n".join(lines)


def find_provider_category(empresa_id: str, proveedor: str = "",
                           rut_proveedor: str = "") -> Optional[Dict]:
    """
    Busca si un proveedor ya tiene una categoría asignada en la memoria.
    Busca primero por RUT (señal más fuerte), luego por nombre normalizado.

    Retorna dict con {categoria, categoria_id, confianza, fuente} o None.
    """
    if not proveedor and not rut_proveedor:
        return None

    learn_dir = _DATA_DIR / "learning"
    human_file = storage_file(learn_dir, "classifications_", empresa_id, ".json")
    auto_file = storage_file(learn_dir, "auto_context_", empresa_id, ".json")
    if human_file is None or auto_file is None:
        return None
    proveedor_norm = proveedor.strip().lower()[:100] if proveedor else ""
    rut_norm = rut_proveedor.strip().replace(".", "").replace("-", "").lower()[:20] if rut_proveedor else ""
    if human_file.exists():
        try:
            learnings = json.loads(human_file.read_text(encoding="utf-8"))[:MAX_HUMAN_LEARNINGS]
            for entry in learnings:
                if rut_norm and entry.get("rut_norm") == rut_norm:
                    return {
                        "categoria": entry["categoria"],
                        "categoria_id": entry.get("categoria_id"),
                        "confianza": 0.95,
                        "fuente": "correccion_humana_rut"
                    }
                if proveedor_norm and entry.get("proveedor_norm") == proveedor_norm:
                    return {
                        "categoria": entry["categoria"],
                        "categoria_id": entry.get("categoria_id"),
                        "confianza": 0.90,
                        "fuente": "correccion_humana_nombre"
                    }
        except Exception:
            pass

    if auto_file.exists():
        try:
            auto_learnings = json.loads(auto_file.read_text(encoding="utf-8"))[:MAX_AUTO_LEARNINGS]
            for entry in auto_learnings:
                if entry.get("veces_visto", 0) < 2:
                    continue  # Solo confiar en patrones vistos 2+ veces
                if rut_norm and entry.get("rut_norm") == rut_norm:
                    return {
                        "categoria": entry["categoria"],
                        "categoria_id": entry.get("categoria_id"),
                        "confianza": min(0.85, entry.get("confianza_promedio", 0.7)),
                        "fuente": "auto_contexto_rut"
                    }
                if proveedor_norm and entry.get("proveedor_norm") == proveedor_norm:
                    return {
                        "categoria": entry["categoria"],
                        "categoria_id": entry.get("categoria_id"),
                        "confianza": min(0.80, entry.get("confianza_promedio", 0.7)),
                        "fuente": "auto_contexto_nombre"
                    }
        except Exception:
            pass

    return None
