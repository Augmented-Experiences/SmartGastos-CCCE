"""Categorías contables base que se crean al registrar una empresa."""
from __future__ import annotations

import json
import logging
import uuid
from typing import Dict, List

logger = logging.getLogger("smartgastos")

AUTO_CATEGORIES = [
    {
        "nombre": "Operaciones",
        "tipo_gasto": "Operación",
        "codigo": "OPE",
        "keywords": ["suministro", "insumo", "materiales", "mantención", "reparación",
                     "limpieza", "transporte", "flete", "bodega", "logística"],
        "deducibilidad": "Total",
        "regla_iva": "Recuperable",
    },
    {
        "nombre": "Marketing y Publicidad",
        "tipo_gasto": "Marketing",
        "codigo": "MKT",
        "keywords": ["publicidad", "propaganda", "diseño", "imprenta", "redes sociales",
                     "facebook", "instagram", "google ads", "agencia"],
        "deducibilidad": "Total",
        "regla_iva": "Recuperable",
    },
    {
        "nombre": "Tecnología",
        "tipo_gasto": "Tecnología",
        "codigo": "TEC",
        "keywords": ["software", "hardware", "computador", "hosting", "licencia",
                     "suscripción", "internet", "nube", "cloud", "soporte técnico"],
        "deducibilidad": "Total",
        "regla_iva": "Recuperable",
    },
    {
        "nombre": "Servicios Básicos",
        "tipo_gasto": "Servicios",
        "codigo": "SVC",
        "keywords": ["agua", "luz", "electricidad", "gas", "teléfono", "celular",
                     "epm", "codensa", "enel", "vanti", "acueducto", "etb",
                     "tigo", "claro", "movistar", "wom"],
        "deducibilidad": "Total",
        "regla_iva": "Recuperable",
    },
    {
        "nombre": "Arriendo",
        "tipo_gasto": "Arriendo",
        "codigo": "ARR",
        "keywords": ["arriendo", "alquiler", "renta", "arrendamiento", "local", "oficina"],
        "deducibilidad": "Total",
        "regla_iva": "No recuperable",
    },
    {
        "nombre": "Sueldos y RRHH",
        "tipo_gasto": "Personal",
        "codigo": "RHH",
        "keywords": ["sueldo", "salario", "honorario", "nómina", "pensión",
                     "salud", "arl", "cesantías", "prima", "caja de compensación",
                     "liquidación"],
        "deducibilidad": "Total",
        "regla_iva": "No recuperable",
    },
    {
        "nombre": "Impuestos y Legal",
        "tipo_gasto": "Impuestos",
        "codigo": "IMP",
        "keywords": ["impuesto", "iva", "renta", "dian", "ica", "retefuente",
                     "reteiva", "reteica", "industria y comercio", "notaría",
                     "abogado", "legal", "contador"],
        "deducibilidad": "No deducible",
        "regla_iva": "No recuperable",
    },
    {
        "nombre": "Financiero",
        "tipo_gasto": "Financiero",
        "codigo": "FIN",
        "keywords": ["banco", "crédito", "préstamo", "interés", "comisión bancaria",
                     "seguro", "póliza", "leasing", "factoring"],
        "deducibilidad": "Parcial",
        "regla_iva": "No recuperable",
    },
    {
        "nombre": "Alimentación",
        "tipo_gasto": "Alimentación",
        "codigo": "ALI",
        "keywords": ["alimentación", "comida", "restaurante", "colación",
                     "supermercado", "café", "catering"],
        "deducibilidad": "Total",
        "regla_iva": "Recuperable",
    },
    {
        "nombre": "Otros Gastos",
        "tipo_gasto": "Otros",
        "codigo": "OTR",
        "keywords": ["otro", "varios", "misceláneo", "general"],
        "deducibilidad": "Parcial",
        "regla_iva": "Recuperable",
    },
]


def seed_default_categories(db, empresa_id: str) -> List[Dict]:
    """Crea las categorías base si la empresa aún no tiene ninguna con ese nombre."""
    from models import CategoriaContable

    existing = {
        (c.nombre or "").strip().lower()
        for c in db.query(CategoriaContable).filter(
            CategoriaContable.empresa_id == empresa_id
        ).all()
    }
    created = []
    for cat_def in AUTO_CATEGORIES:
        key = cat_def["nombre"].strip().lower()
        if key in existing:
            continue
        cat_id = str(uuid.uuid4())
        db.add(CategoriaContable(
            id=cat_id,
            empresa_id=empresa_id,
            codigo=cat_def["codigo"],
            nombre=cat_def["nombre"],
            tipo_gasto=cat_def["tipo_gasto"],
            deducibilidad=cat_def["deducibilidad"],
            regla_iva=cat_def["regla_iva"],
            keywords=json.dumps(cat_def["keywords"], ensure_ascii=False),
            activa=True,
        ))
        created.append({
            "id": cat_id,
            "nombre": cat_def["nombre"],
            "descripcion": cat_def["tipo_gasto"],
        })
        existing.add(key)

    if not created:
        return []
    try:
        db.commit()
        logger.info("Creadas %s categorías base para empresa %s", len(created), empresa_id)
        return created
    except Exception as exc:
        logger.error("Error creando categorías base: %s", exc)
        db.rollback()
        return []
