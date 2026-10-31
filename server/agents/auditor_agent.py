"""
Agente Auditor para validación y detección de excepciones en documentos.
Detecta duplicados, inconsistencias, documentos sospechosos, etc.
"""
import os
import json
import hashlib
from typing import Dict, List, Optional
from datetime import datetime, timedelta
import requests

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
TIMEOUT_AUDIT = int(os.getenv("OLLAMA_TIMEOUT_AUDIT", "300"))


class AuditorAgent:
    """Agente especializado en auditoría y validación de documentos contables."""
    
    def __init__(self, model: str = "llama3.2:3b"):
        """
        Inicializa el agente auditor.
        
        Args:
            model: Modelo Ollama a usar
        """
        self.model = model
    
    def audit(self, document: Dict, historical_docs: List[Dict] = None) -> Dict:
        """
        Realiza auditoría completa de un documento.
        
        Args:
            document: Datos del documento a auditar
            historical_docs: Documentos históricos para comparación
            
        Returns:
            Dict con excepciones y alertas detectadas
        """
        exceptions = []
        alerts = []
        risk_score = 0.0  # 0.0 a 1.0
        
        # Validaciones básicas
        basic_exceptions = self._validate_basic_fields(document)
        exceptions.extend(basic_exceptions)
        
        # Validaciones de consistencia
        consistency_exceptions = self._validate_consistency(document)
        exceptions.extend(consistency_exceptions)
        
        # Detección de duplicados
        if historical_docs:
            duplicate_check = self._check_duplicates(document, historical_docs)
            if duplicate_check["is_duplicate"]:
                exceptions.append({
                    "type": "DUPLICADO",
                    "severity": "CRÍTICA",
                    "message": f"Documento potencialmente duplicado: {duplicate_check['match_reason']}",
                    "matched_doc": duplicate_check.get("matched_doc_id")
                })
                risk_score += 0.5
        
        # Detección de anomalías
        anomalies = self._detect_anomalies(document, historical_docs or [])
        exceptions.extend(anomalies)
        if anomalies:
            risk_score += 0.2
        
        # Validaciones de política
        policy_exceptions = self._validate_policy(document)
        exceptions.extend(policy_exceptions)
        if policy_exceptions:
            risk_score += 0.1
        
        # Normalizar risk score
        risk_score = min(1.0, risk_score)
        
        return {
            "excepciones": exceptions,
            "alertas": alerts,
            "risk_score": risk_score,
            "requiere_revision": len(exceptions) > 0 or risk_score > 0.5,
            "timestamp": datetime.utcnow().isoformat()
        }
    
    def _validate_basic_fields(self, document: Dict) -> List[Dict]:
        """Valida que los campos básicos sean válidos."""
        exceptions = []
        
        # Campos requeridos
        required_fields = ["proveedor", "fecha_emision", "monto_total"]
        for field in required_fields:
            if not document.get(field):
                exceptions.append({
                    "type": "CAMPO_FALTANTE",
                    "severity": "MEDIA",
                    "field": field,
                    "message": f"Campo requerido faltante: {field}"
                })
        
        # Validar fecha
        fecha = document.get("fecha_emision")
        if fecha:
            try:
                if isinstance(fecha, str):
                    fecha_obj = datetime.fromisoformat(fecha)
                else:
                    fecha_obj = fecha
                
                # Fecha no puede ser futura
                if fecha_obj > datetime.utcnow():
                    exceptions.append({
                        "type": "FECHA_FUTURA",
                        "severity": "MEDIA",
                        "message": f"Fecha de emisión es futura: {fecha}"
                    })
                
                # Fecha no puede ser muy antigua (más de 5 años)
                if fecha_obj < datetime.utcnow() - timedelta(days=365*5):
                    exceptions.append({
                        "type": "FECHA_ANTIGUA",
                        "severity": "BAJA",
                        "message": f"Documento muy antiguo: {fecha}"
                    })
            except Exception as e:
                exceptions.append({
                    "type": "FECHA_INVÁLIDA",
                    "severity": "MEDIA",
                    "message": f"Formato de fecha inválido: {fecha}"
                })
        
        # Validar montos
        monto_neto = document.get("monto_neto")
        iva = document.get("iva")
        monto_total = document.get("monto_total")
        
        if monto_neto is not None and monto_neto < 0:
            exceptions.append({
                "type": "MONTO_NEGATIVO",
                "severity": "MEDIA",
                "field": "monto_neto",
                "message": "Monto neto no puede ser negativo"
            })
        
        if iva is not None and iva < 0:
            exceptions.append({
                "type": "IVA_NEGATIVO",
                "severity": "MEDIA",
                "field": "iva",
                "message": "IVA no puede ser negativo"
            })
        
        # Validar consistencia neto + IVA = total
        if monto_neto is not None and iva is not None and monto_total is not None:
            calculated = monto_neto + iva
            if abs(calculated - monto_total) > 1:  # Tolerancia de 1
                exceptions.append({
                    "type": "INCONSISTENCIA_MONTOS",
                    "severity": "ALTA",
                    "message": f"Inconsistencia: {monto_neto} + {iva} ≠ {monto_total}",
                    "calculated": calculated,
                    "reported": monto_total
                })
        
        return exceptions
    
    def _validate_consistency(self, document: Dict) -> List[Dict]:
        """Valida consistencia interna del documento."""
        exceptions = []
        
        # RUT debe tener formato válido
        rut = document.get("rut_proveedor")
        if rut and not self._validate_rut_format(rut):
            exceptions.append({
                "type": "RUT_INVÁLIDO",
                "severity": "MEDIA",
                "message": f"Formato de RUT inválido: {rut}"
            })
        
        # Tipo de documento debe ser válido
        tipo_doc = document.get("tipo_documento")
        valid_types = ["Factura", "Boleta", "Cartola", "Comprobante", "Otro"]
        if tipo_doc and tipo_doc not in valid_types:
            exceptions.append({
                "type": "TIPO_DOCUMENTO_INVÁLIDO",
                "severity": "BAJA",
                "message": f"Tipo de documento no reconocido: {tipo_doc}"
            })
        
        # Moneda debe ser código válido
        moneda = document.get("moneda", "CLP")
        if not self._validate_currency_code(moneda):
            exceptions.append({
                "type": "MONEDA_INVÁLIDA",
                "severity": "BAJA",
                "message": f"Código de moneda inválido: {moneda}"
            })
        
        return exceptions
    
    def _check_duplicates(self, document: Dict, historical_docs: List[Dict]) -> Dict:
        """Detecta posibles documentos duplicados."""
        proveedor = (document.get("proveedor") or "").lower()
        folio = document.get("folio")
        monto = document.get("monto_total")
        fecha = document.get("fecha_emision")
        
        for hist_doc in historical_docs:
            hist_proveedor = (hist_doc.get("proveedor") or "").lower()
            hist_folio = hist_doc.get("folio")
            hist_monto = hist_doc.get("monto_total")
            hist_fecha = hist_doc.get("fecha_emision")
            
            # Criterios de duplicado
            if proveedor == hist_proveedor and folio == hist_folio:
                return {
                    "is_duplicate": True,
                    "match_reason": "Mismo proveedor y folio",
                    "matched_doc_id": hist_doc.get("id")
                }
            
            # Mismo proveedor, monto y fecha (probable duplicado)
            if (proveedor == hist_proveedor and 
                monto == hist_monto and 
                fecha == hist_fecha and
                monto > 100):  # Solo si monto es significativo
                return {
                    "is_duplicate": True,
                    "match_reason": "Mismo proveedor, monto y fecha",
                    "matched_doc_id": hist_doc.get("id")
                }
        
        return {"is_duplicate": False}
    
    def _detect_anomalies(self, document: Dict, historical_docs: List[Dict]) -> List[Dict]:
        """Detecta anomalías comparando con histórico."""
        exceptions = []
        
        if not historical_docs:
            return exceptions
        
        proveedor = (document.get("proveedor") or "").lower()
        monto = document.get("monto_total") or 0
        
        # Buscar documentos del mismo proveedor
        same_provider_docs = [
            d for d in historical_docs
            if (d.get("proveedor") or "").lower() == proveedor
        ]
        
        if not same_provider_docs:
            return exceptions
        
        # Calcular promedio de montos
        montos = [d.get("monto_total") or 0 for d in same_provider_docs if d.get("monto_total")]
        if montos:
            avg_monto = sum(montos) / len(montos)
            max_monto = max(montos)
            
            # Alerta si monto es significativamente mayor al promedio
            if monto > avg_monto * 2.5:
                exceptions.append({
                    "type": "MONTO_ANÓMALO",
                    "severity": "MEDIA",
                    "message": f"Monto {monto} es 2.5x mayor al promedio {avg_monto:.0f}",
                    "average": avg_monto,
                    "current": monto
                })
            
            # Alerta si es el monto más alto registrado
            if monto > max_monto * 1.5:
                exceptions.append({
                    "type": "MONTO_MÁXIMO_HISTÓRICO",
                    "severity": "BAJA",
                    "message": f"Monto es el más alto registrado para este proveedor",
                    "previous_max": max_monto,
                    "current": monto
                })
        
        # Detectar suscripciones/cobros recurrentes
        if len(same_provider_docs) >= 3:
            dates = sorted([d.get("fecha_emision") for d in same_provider_docs if d.get("fecha_emision")])
            if len(dates) >= 2:
                # Calcular intervalos entre documentos
                intervals = []
                for i in range(len(dates) - 1):
                    try:
                        d1 = datetime.fromisoformat(dates[i]) if isinstance(dates[i], str) else dates[i]
                        d2 = datetime.fromisoformat(dates[i+1]) if isinstance(dates[i+1], str) else dates[i+1]
                        interval = (d2 - d1).days
                        intervals.append(interval)
                    except:
                        pass
                
                # Si intervalos son regulares (±3 días), es probablemente recurrente
                if intervals and all(abs(iv - intervals[0]) <= 3 for iv in intervals):
                    exceptions.append({
                        "type": "COBRO_RECURRENTE",
                        "severity": "BAJA",
                        "message": f"Proveedor tiene cobros recurrentes cada ~{intervals[0]} días",
                        "interval_days": intervals[0]
                    })
        
        return exceptions
    
    def _validate_policy(self, document: Dict) -> List[Dict]:
        """Valida políticas de gasto (requiere configuración)."""
        exceptions = []
        
        # Ejemplo: Alertar si no hay centro de costo asignado
        if not document.get("centro_costo_id"):
            exceptions.append({
                "type": "SIN_CENTRO_COSTO",
                "severity": "BAJA",
                "message": "Documento sin centro de costo asignado"
            })
        
        # Ejemplo: Alertar si no hay categoría asignada
        if not document.get("categoria_id"):
            exceptions.append({
                "type": "SIN_CATEGORÍA",
                "severity": "BAJA",
                "message": "Documento sin categoría contable asignada"
            })
        
        return exceptions
    
    def _validate_rut_format(self, rut: str) -> bool:
        """Valida formato de RUT chileno."""
        import re
        # Formato: XX.XXX.XXX-X o XXXXXXXX-X
        return bool(re.match(r"^\d{1,2}\.\d{3}\.\d{3}-[\dK]$|^\d{7,8}-[\dK]$", str(rut).upper()))
    
    def _validate_currency_code(self, code: str) -> bool:
        """Valida código de moneda ISO 4217."""
        import re
        return bool(re.match(r"^[A-Z]{3}$", str(code)))


# Instancia global
auditor_agent = None


def get_auditor_agent(model: str = "llama3.2:3b") -> AuditorAgent:
    """Obtiene o crea la instancia del agente auditor."""
    global auditor_agent
    if auditor_agent is None:
        auditor_agent = AuditorAgent(model)
    return auditor_agent
