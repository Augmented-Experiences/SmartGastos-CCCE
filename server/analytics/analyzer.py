"""
Módulo de analítica para pyme-ledger-ai.
Genera KPIs, vistas mensuales, rankings y análisis de gastos.
"""
import json
import math
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from collections import defaultdict
from sqlalchemy.orm import Session
from sqlalchemy import func

from models import Documento, CategoriaContable, CentroCosto


def money(value) -> float:
    """0 si el monto es None, NaN o Infinity (no debe romper el JSON del dashboard)."""
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return number


def fx_rate(doc) -> float:
    """1 si no hay tipo de cambio. El cliente guarda 1 cuando la moneda es la local."""
    rate = money(getattr(doc, "tipo_cambio", None) or 1)
    return rate if rate > 0 else 1.0


def local_amount(doc, attr: str) -> float:
    """Monto del documento expresado en la moneda local de la empresa."""
    return money(getattr(doc, attr, None)) * fx_rate(doc)


class AnalyticsEngine:
    """Motor de análisis de gastos y generación de KPIs."""
    
    def __init__(self, db: Session, empresa_id: str):
        """
        Inicializa el motor de analítica.
        
        Args:
            db: Sesión de base de datos
            empresa_id: ID de la empresa
        """
        self.db = db
        self.empresa_id = empresa_id
    
    def get_dashboard_summary(self, period_days: int = 30) -> Dict:
        """
        Genera resumen para dashboard principal.
        
        Args:
            period_days: Período en días a analizar
            
        Returns:
            Dict con KPIs principales
        """
        start_date = datetime.utcnow() - timedelta(days=period_days)
        
        # Documentos en el período
        docs = self.db.query(Documento).filter(
            Documento.empresa_id == self.empresa_id,
            Documento.fecha_creacion >= start_date
        ).all()
        
        if not docs:
            return self._get_empty_summary()
        
        # Cálculos básicos
        total_gasto = sum(local_amount(d, "monto_total") for d in docs)
        total_iva = sum(local_amount(d, "iva") for d in docs)
        total_neto = sum(local_amount(d, "monto_neto") for d in docs)
        
        # Documentos por estado
        pending_review = len([d for d in docs if d.estado_revision.value == "Pendiente"])
        reviewed = len([d for d in docs if d.estado_revision.value == "Revisado"])
        duplicates = len([d for d in docs if d.estado_revision.value == "Duplicado"])
        
        # Promedio por documento
        avg_gasto = total_gasto / len(docs) if docs else 0
        
        # Proveedor con mayor gasto
        top_provider = self._get_top_provider(docs)
        
        # Categoría con mayor gasto
        top_category = self._get_top_category(docs)
        
        return {
            "periodo_dias": period_days,
            "fecha_generacion": datetime.utcnow().isoformat(),
            "kpis": {
                "total_gasto": round(total_gasto, 2),
                "total_iva": round(total_iva, 2),
                "total_neto": round(total_neto, 2),
                "promedio_documento": round(avg_gasto, 2),
                "cantidad_documentos": len(docs),
                "documentos_pendiente_revision": pending_review,
                "documentos_revisados": reviewed,
                "documentos_duplicados": duplicates
            },
            "top_provider": top_provider,
            "top_category": top_category,
            "tendencia": self._calculate_trend(docs)
        }
    
    def get_expenses_by_category(self, period_days: int = 30) -> Dict:
        """
        Análisis de gastos por categoría contable.
        
        Args:
            period_days: Período en días
            
        Returns:
            Dict con gastos agrupados por categoría
        """
        start_date = datetime.utcnow() - timedelta(days=period_days)
        
        docs = self.db.query(Documento).filter(
            Documento.empresa_id == self.empresa_id,
            Documento.fecha_creacion >= start_date
        ).all()
        
        categories_data = defaultdict(lambda: {
            "total": 0.0,
            "iva": 0.0,
            "neto": 0.0,
            "cantidad": 0,
            "documentos": []
        })
        
        for doc in docs:
            cat_name = doc.categoria.nombre if doc.categoria else "Sin categoría"
            categories_data[cat_name]["total"] += local_amount(doc, "monto_total")
            categories_data[cat_name]["iva"] += local_amount(doc, "iva")
            categories_data[cat_name]["neto"] += local_amount(doc, "monto_neto")
            categories_data[cat_name]["cantidad"] += 1
            categories_data[cat_name]["documentos"].append({
                "id": doc.id,
                "proveedor": doc.proveedor,
                "monto": local_amount(doc, "monto_total"),
                "fecha": doc.fecha_emision.isoformat() if doc.fecha_emision else None
            })
        
        # Convertir a lista y ordenar por total
        result = [
            {
                "categoria": cat,
                "total": round(data["total"], 2),
                "iva": round(data["iva"], 2),
                "neto": round(data["neto"], 2),
                "cantidad": data["cantidad"],
                "promedio": round(data["total"] / data["cantidad"], 2) if data["cantidad"] > 0 else 0,
                "porcentaje": 0  # Se calcula después
            }
            for cat, data in categories_data.items()
        ]
        
        # Calcular porcentajes
        total_general = sum(r["total"] for r in result)
        for r in result:
            r["porcentaje"] = round((r["total"] / total_general * 100), 1) if total_general > 0 else 0
        
        # Ordenar por total descendente
        result.sort(key=lambda x: x["total"], reverse=True)
        
        return {
            "periodo_dias": period_days,
            "total_general": round(total_general, 2),
            "categorias": result
        }
    
    def get_expenses_by_provider(self, period_days: int = 30, limit: int = 20) -> Dict:
        """
        Análisis de gastos por proveedor.
        
        Args:
            period_days: Período en días
            limit: Número máximo de proveedores a retornar
            
        Returns:
            Dict con gastos agrupados por proveedor
        """
        start_date = datetime.utcnow() - timedelta(days=period_days)
        
        docs = self.db.query(Documento).filter(
            Documento.empresa_id == self.empresa_id,
            Documento.fecha_creacion >= start_date
        ).all()
        
        providers_data = defaultdict(lambda: {
            "total": 0.0,
            "cantidad": 0,
            "rut": None,
            "categorias": defaultdict(int),
            "fechas": []
        })
        
        for doc in docs:
            prov_name = doc.proveedor or "Desconocido"
            providers_data[prov_name]["total"] += local_amount(doc, "monto_total")
            providers_data[prov_name]["cantidad"] += 1
            providers_data[prov_name]["rut"] = doc.rut_proveedor
            if doc.categoria:
                providers_data[prov_name]["categorias"][doc.categoria.nombre] += 1
            if doc.fecha_emision:
                providers_data[prov_name]["fechas"].append(doc.fecha_emision.isoformat())
        
        # Convertir a lista y ordenar
        result = [
            {
                "proveedor": prov,
                "rut": data["rut"],
                "total": round(data["total"], 2),
                "cantidad": data["cantidad"],
                "promedio": round(data["total"] / data["cantidad"], 2) if data["cantidad"] > 0 else 0,
                "categorias": dict(data["categorias"]),
                "frecuencia": self._detect_frequency(data["fechas"])
            }
            for prov, data in providers_data.items()
        ]
        
        # Ordenar por total descendente y limitar
        result.sort(key=lambda x: x["total"], reverse=True)
        result = result[:limit]
        
        return {
            "periodo_dias": period_days,
            "total_proveedores": len(providers_data),
            "top_proveedores": result
        }
    
    def get_expenses_by_cost_center(self, period_days: int = 30) -> Dict:
        """
        Análisis de gastos por centro de costo.
        
        Args:
            period_days: Período en días
            
        Returns:
            Dict con gastos agrupados por centro de costo
        """
        start_date = datetime.utcnow() - timedelta(days=period_days)
        
        docs = self.db.query(Documento).filter(
            Documento.empresa_id == self.empresa_id,
            Documento.fecha_creacion >= start_date
        ).all()
        
        cost_centers_data = defaultdict(lambda: {
            "total": 0.0,
            "cantidad": 0,
            "categorias": defaultdict(float)
        })
        
        for doc in docs:
            cc_name = doc.centro_costo.nombre if doc.centro_costo else "Sin asignar"
            cost_centers_data[cc_name]["total"] += local_amount(doc, "monto_total")
            cost_centers_data[cc_name]["cantidad"] += 1
            if doc.categoria:
                cost_centers_data[cc_name]["categorias"][doc.categoria.nombre] += local_amount(doc, "monto_total")
        
        # Convertir a lista
        result = [
            {
                "centro_costo": cc,
                "total": round(data["total"], 2),
                "cantidad": data["cantidad"],
                "promedio": round(data["total"] / data["cantidad"], 2) if data["cantidad"] > 0 else 0,
                "categorias": {k: round(v, 2) for k, v in data["categorias"].items()}
            }
            for cc, data in cost_centers_data.items()
        ]
        
        # Ordenar por total descendente
        result.sort(key=lambda x: x["total"], reverse=True)
        
        return {
            "periodo_dias": period_dias,
            "centros_costo": result
        }
    
    def get_monthly_trend(self, months: int = 12) -> Dict:
        """
        Análisis de tendencia mensual de gastos.
        
        Args:
            months: Número de meses a analizar
            
        Returns:
            Dict con datos mensuales
        """
        start_date = datetime.utcnow() - timedelta(days=30*months)
        
        docs = self.db.query(Documento).filter(
            Documento.empresa_id == self.empresa_id,
            Documento.fecha_creacion >= start_date
        ).all()
        
        monthly_data = defaultdict(lambda: {
            "total": 0.0,
            "neto": 0.0,
            "iva": 0.0,
            "cantidad": 0
        })
        
        for doc in docs:
            if doc.fecha_emision:
                month_key = doc.fecha_emision.strftime("%Y-%m")
            else:
                month_key = doc.fecha_creacion.strftime("%Y-%m")
            
            monthly_data[month_key]["total"] += local_amount(doc, "monto_total")
            monthly_data[month_key]["neto"] += local_amount(doc, "monto_neto")
            monthly_data[month_key]["iva"] += local_amount(doc, "iva")
            monthly_data[month_key]["cantidad"] += 1
        
        # Convertir a lista ordenada
        result = [
            {
                "mes": month,
                "total": round(data["total"], 2),
                "neto": round(data["neto"], 2),
                "iva": round(data["iva"], 2),
                "cantidad": data["cantidad"]
            }
            for month, data in sorted(monthly_data.items())
        ]
        
        return {
            "meses": months,
            "datos": result,
            "promedio_mensual": round(sum(d["total"] for d in result) / len(result), 2) if result else 0
        }
    
    def get_anomalies_and_alerts(self, period_days: int = 30) -> Dict:
        """
        Detecta anomalías y genera alertas.
        
        Args:
            period_days: Período en días
            
        Returns:
            Dict con anomalías detectadas
        """
        start_date = datetime.utcnow() - timedelta(days=period_days)
        
        docs = self.db.query(Documento).filter(
            Documento.empresa_id == self.empresa_id,
            Documento.fecha_creacion >= start_date
        ).all()
        
        alerts = []
        
        # Alerta 1: Documentos sin clasificación
        unclassified = [d for d in docs if not d.categoria_id]
        if unclassified:
            alerts.append({
                "tipo": "SIN_CLASIFICACIÓN",
                "severidad": "MEDIA",
                "cantidad": len(unclassified),
                "mensaje": f"{len(unclassified)} documentos sin categoría asignada"
            })
        
        # Alerta 2: Documentos sin centro de costo
        no_cost_center = [d for d in docs if not d.centro_costo_id]
        if no_cost_center:
            alerts.append({
                "tipo": "SIN_CENTRO_COSTO",
                "severidad": "MEDIA",
                "cantidad": len(no_cost_center),
                "mensaje": f"{len(no_cost_center)} documentos sin centro de costo"
            })
        
        # Alerta 3: Documentos con baja confianza
        low_confidence = [d for d in docs if d.confianza_clasificacion < 0.6]
        if low_confidence:
            alerts.append({
                "tipo": "BAJA_CONFIANZA",
                "severidad": "MEDIA",
                "cantidad": len(low_confidence),
                "mensaje": f"{len(low_confidence)} documentos con confianza < 60%"
            })
        
        # Alerta 4: Duplicados detectados
        duplicates = [d for d in docs if d.estado_revision.value == "Duplicado"]
        if duplicates:
            alerts.append({
                "tipo": "DUPLICADOS",
                "severidad": "ALTA",
                "cantidad": len(duplicates),
                "mensaje": f"{len(duplicates)} documentos duplicados detectados"
            })
        
        # Alerta 5: Gastos anómalos
        anomalies = [d for d in docs if d.excepciones]
        if anomalies:
            alerts.append({
                "tipo": "GASTOS_ANÓMALOS",
                "severidad": "MEDIA",
                "cantidad": len(anomalies),
                "mensaje": f"{len(anomalies)} documentos con excepciones detectadas"
            })
        
        return {
            "periodo_dias": period_days,
            "total_alertas": len(alerts),
            "alertas": alerts
        }
    
    # Métodos auxiliares
    
    def _get_empty_summary(self) -> Dict:
        """Retorna resumen vacío."""
        return {
            "periodo_dias": 30,
            "fecha_generacion": datetime.utcnow().isoformat(),
            "kpis": {
                "total_gasto": 0,
                "total_iva": 0,
                "total_neto": 0,
                "promedio_documento": 0,
                "cantidad_documentos": 0,
                "documentos_pendiente_revision": 0,
                "documentos_revisados": 0,
                "documentos_duplicados": 0
            },
            "top_provider": None,
            "top_category": None,
            "tendencia": "Sin datos"
        }
    
    def _get_top_provider(self, docs: list) -> Optional[Dict]:
        """Obtiene el proveedor con mayor gasto."""
        if not docs:
            return None
        
        providers = defaultdict(float)
        for doc in docs:
            if doc.proveedor:
                providers[doc.proveedor] += local_amount(doc, "monto_total")
        
        if not providers:
            return None
        
        top = max(providers.items(), key=lambda x: x[1])
        return {"proveedor": top[0], "total": round(top[1], 2)}
    
    def _get_top_category(self, docs: list) -> Optional[Dict]:
        """Obtiene la categoría con mayor gasto."""
        if not docs:
            return None
        
        categories = defaultdict(float)
        for doc in docs:
            cat_name = doc.categoria.nombre if doc.categoria else "Sin categoría"
            categories[cat_name] += local_amount(doc, "monto_total")
        
        if not categories:
            return None
        
        top = max(categories.items(), key=lambda x: x[1])
        return {"categoria": top[0], "total": round(top[1], 2)}
    
    def _calculate_trend(self, docs: list) -> str:
        """Calcula tendencia de gastos."""
        if len(docs) < 2:
            return "Sin datos"
        
        # Dividir en dos períodos
        mid = len(docs) // 2
        first_half = sum(local_amount(d, "monto_total") for d in docs[:mid])
        second_half = sum(local_amount(d, "monto_total") for d in docs[mid:])
        
        if first_half == 0:
            return "Sin datos"
        
        change = ((second_half - first_half) / first_half) * 100
        
        if change > 10:
            return f"Alza ({change:.1f}%)"
        elif change < -10:
            return f"Baja ({change:.1f}%)"
        else:
            return "Estable"
    
    def _detect_frequency(self, dates: list) -> str:
        """Detecta frecuencia de pagos."""
        if len(dates) < 2:
            return "Único"
        
        dates_sorted = sorted(dates)
        intervals = []
        
        for i in range(len(dates_sorted) - 1):
            d1 = datetime.fromisoformat(dates_sorted[i])
            d2 = datetime.fromisoformat(dates_sorted[i+1])
            interval = (d2 - d1).days
            intervals.append(interval)
        
        if not intervals:
            return "Único"
        
        avg_interval = sum(intervals) / len(intervals)
        
        if avg_interval < 5:
            return "Diario"
        elif avg_interval < 10:
            return "Semanal"
        elif avg_interval < 35:
            return "Mensual"
        elif avg_interval < 100:
            return "Trimestral"
        else:
            return "Anual"
