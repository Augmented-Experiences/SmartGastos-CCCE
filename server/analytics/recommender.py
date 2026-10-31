"""
Módulo de recomendaciones para pyme-ledger-ai.
Genera sugerencias de optimización y ahorro de gastos.
"""
import json
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from collections import defaultdict
from sqlalchemy.orm import Session

from models import Documento


class RecommendationEngine:
    """Motor de generación de recomendaciones de ahorro."""
    
    def __init__(self, db: Session, empresa_id: str):
        """
        Inicializa el motor de recomendaciones.
        
        Args:
            db: Sesión de base de datos
            empresa_id: ID de la empresa
        """
        self.db = db
        self.empresa_id = empresa_id
    
    def get_recommendations(self, period_days: int = 90) -> Dict:
        """
        Genera recomendaciones de optimización de gastos.
        
        Args:
            period_days: Período en días para análisis
            
        Returns:
            Dict con recomendaciones
        """
        start_date = datetime.utcnow() - timedelta(days=period_days)
        
        docs = self.db.query(Documento).filter(
            Documento.empresa_id == self.empresa_id,
            Documento.fecha_creacion >= start_date
        ).all()
        
        recommendations = []
        
        # Recomendación 1: Consolidación de proveedores
        consolidation_recs = self._recommend_provider_consolidation(docs)
        recommendations.extend(consolidation_recs)
        
        # Recomendación 2: Gastos duplicados
        duplicate_recs = self._recommend_duplicate_detection(docs)
        recommendations.extend(duplicate_recs)
        
        # Recomendación 3: Suscripciones recurrentes
        subscription_recs = self._recommend_subscription_review(docs)
        recommendations.extend(subscription_recs)
        
        # Recomendación 4: Categorías con mayor gasto
        category_recs = self._recommend_category_optimization(docs)
        recommendations.extend(category_recs)
        
        # Recomendación 5: Proveedores con alza anómala
        anomaly_recs = self._recommend_anomaly_investigation(docs)
        recommendations.extend(anomaly_recs)
        
        # Ordenar por impacto potencial
        recommendations.sort(key=lambda x: x.get("impacto_potencial", 0), reverse=True)
        
        return {
            "periodo_dias": period_days,
            "fecha_generacion": datetime.utcnow().isoformat(),
            "total_recomendaciones": len(recommendations),
            "ahorro_potencial_total": sum(r.get("ahorro_potencial", 0) for r in recommendations),
            "recomendaciones": recommendations,
            "metodologia": {
                "resumen": "Las recomendaciones se calculan solas a partir de tus documentos de los últimos meses. No hace falta configurar parámetros: revisa las tarjetas y decide qué aplicar.",
                "reglas": [
                    {
                        "id": "categoria_alto_gasto",
                        "cuando": "Una categoría supera el 20% del gasto del período",
                        "ahorro": "15% estimado de esa categoría",
                    },
                    {
                        "id": "consolidacion_proveedores",
                        "cuando": "Hay más de 2 proveedores en la misma categoría",
                        "ahorro": "5% estimado al consolidar compras",
                    },
                    {
                        "id": "duplicados",
                        "cuando": "Mismo proveedor, mismo monto y fechas a menos de 7 días",
                        "ahorro": "El monto del documento duplicado",
                    },
                    {
                        "id": "suscripcion_recurrente",
                        "cuando": "Un proveedor cobra con intervalos regulares (3 o más veces)",
                        "ahorro": "10% estimado de la proyección anual",
                    },
                    {
                        "id": "alza_anomala",
                        "cuando": "Un cobro supera 2,5 veces el promedio de ese proveedor",
                        "ahorro": "La diferencia respecto al promedio",
                    },
                ],
            },
        }
    
    def _recommend_provider_consolidation(self, docs: list) -> list:
        """Recomienda consolidación de proveedores similares."""
        recommendations = []
        
        # Agrupar por tipo de servicio
        providers_by_category = defaultdict(list)
        
        for doc in docs:
            if doc.categoria:
                cat_name = doc.categoria.nombre
                providers_by_category[cat_name].append({
                    "proveedor": doc.proveedor,
                    "monto": doc.monto_total or 0,
                    "fecha": doc.fecha_emision
                })
        
        # Buscar categorías con múltiples proveedores
        for category, providers_list in providers_by_category.items():
            providers_unique = set(p["proveedor"] for p in providers_list if p["proveedor"])
            
            if len(providers_unique) > 2:
                total_category = sum(p["monto"] for p in providers_list)
                
                recommendations.append({
                    "tipo": "CONSOLIDACIÓN_PROVEEDORES",
                    "severidad": "MEDIA",
                    "categoria": category,
                    "cantidad_proveedores": len(providers_unique),
                    "gasto_total": round(total_category, 2),
                    "ahorro_potencial": round(total_category * 0.05, 2),  # 5% de ahorro estimado
                    "impacto_potencial": round(total_category * 0.05, 2),
                    "mensaje": f"Consolidar {len(providers_unique)} proveedores de {category} podría ahorrar ~{round(total_category * 0.05, 2)}",
                    "accion": f"Negociar con 1-2 proveedores principales para {category}",
                    "criterio": "Hay más de 2 proveedores en la misma categoría. El ahorro es un 5% estimado al consolidar compras.",
                })
        
        return recommendations
    
    def _recommend_duplicate_detection(self, docs: list) -> list:
        """Recomienda revisión de posibles duplicados."""
        recommendations = []
        
        # Buscar documentos similares (mismo proveedor, monto similar, fechas cercanas)
        potential_duplicates = defaultdict(list)
        
        for i, doc1 in enumerate(docs):
            for doc2 in docs[i+1:]:
                if (doc1.proveedor and doc2.proveedor and
                    doc1.proveedor.lower() == doc2.proveedor.lower() and
                    abs((doc1.monto_total or 0) - (doc2.monto_total or 0)) < 1 and
                    doc1.fecha_emision and doc2.fecha_emision and
                    abs((doc1.fecha_emision - doc2.fecha_emision).days) < 7):
                    
                    key = f"{doc1.proveedor}_{doc1.monto_total}"
                    potential_duplicates[key].append((doc1, doc2))
        
        if potential_duplicates:
            total_duplicate_amount = 0
            
            for key, pairs in potential_duplicates.items():
                for doc1, doc2 in pairs:
                    total_duplicate_amount += doc1.monto_total or 0
            
            recommendations.append({
                "tipo": "DUPLICADOS_POTENCIALES",
                "severidad": "ALTA",
                "cantidad_pares": len(potential_duplicates),
                "monto_potencial_duplicado": round(total_duplicate_amount, 2),
                "ahorro_potencial": round(total_duplicate_amount, 2),
                "impacto_potencial": round(total_duplicate_amount, 2),
                "mensaje": f"Se detectaron {len(potential_duplicates)} pares de documentos potencialmente duplicados",
                "accion": "Revisar y eliminar documentos duplicados",
                "criterio": "Mismo proveedor, mismo monto y fechas a menos de 7 días. El ahorro es el monto duplicado.",
            })
        
        return recommendations
    
    def _recommend_subscription_review(self, docs: list) -> list:
        """Recomienda revisión de suscripciones recurrentes."""
        recommendations = []
        
        # Detectar pagos recurrentes
        recurring_providers = defaultdict(list)
        
        for doc in docs:
            if doc.proveedor and doc.fecha_emision:
                recurring_providers[doc.proveedor].append(doc.fecha_emision)
        
        for provider, dates in recurring_providers.items():
            if len(dates) >= 3:
                dates_sorted = sorted(dates)
                intervals = []
                
                for i in range(len(dates_sorted) - 1):
                    interval = (dates_sorted[i+1] - dates_sorted[i]).days
                    intervals.append(interval)
                
                # Si los intervalos son regulares, es probablemente una suscripción
                if intervals and all(abs(iv - intervals[0]) <= 3 for iv in intervals):
                    avg_interval = sum(intervals) / len(intervals)
                    total_annual = 0
                    
                    # Calcular gasto anual
                    for doc in docs:
                        if doc.proveedor == provider:
                            total_annual += doc.monto_total or 0
                    
                    # Proyectar anual si es necesario
                    if avg_interval > 0:
                        annual_projection = total_annual * (365 / avg_interval)
                    else:
                        annual_projection = total_annual
                    
                    recommendations.append({
                        "tipo": "SUSCRIPCIÓN_RECURRENTE",
                        "severidad": "BAJA",
                        "proveedor": provider,
                        "frecuencia_dias": round(avg_interval),
                        "gasto_registrado": round(total_annual, 2),
                        "proyeccion_anual": round(annual_projection, 2),
                        "ahorro_potencial": round(annual_projection * 0.10, 2),  # 10% de ahorro estimado
                        "impacto_potencial": round(annual_projection * 0.10, 2),
                        "mensaje": f"{provider} es un cobro recurrente cada ~{round(avg_interval)} días",
                        "accion": f"Revisar contrato y negociar descuentos anuales o cambiar proveedor",
                        "criterio": "El proveedor cobra con intervalos regulares. El ahorro es un 10% estimado de la proyección anual.",
                    })
        
        return recommendations
    
    def _recommend_category_optimization(self, docs: list) -> list:
        """Recomienda optimización por categoría de gasto."""
        recommendations = []
        
        # Agrupar por categoría
        categories_data = defaultdict(float)
        
        for doc in docs:
            cat_name = doc.categoria.nombre if doc.categoria else "Sin categoría"
            categories_data[cat_name] += doc.monto_total or 0
        
        # Identificar categorías con alto gasto
        total_gasto = sum(categories_data.values())
        
        for category, amount in categories_data.items():
            percentage = (amount / total_gasto * 100) if total_gasto > 0 else 0
            
            # Si una categoría representa > 20% del gasto, recomendar revisión
            if percentage > 20:
                recommendations.append({
                    "tipo": "CATEGORÍA_ALTO_GASTO",
                    "severidad": "MEDIA",
                    "categoria": category,
                    "gasto_total": round(amount, 2),
                    "porcentaje_total": round(percentage, 1),
                    "ahorro_potencial": round(amount * 0.15, 2),  # 15% de ahorro estimado
                    "impacto_potencial": round(amount * 0.15, 2),
                    "mensaje": f"{category} representa {percentage:.1f}% del gasto total",
                    "accion": f"Revisar gastos en {category} e identificar oportunidades de reducción",
                    "criterio": f"Esta categoría supera el 20% del gasto ({percentage:.1f}%). El ahorro potencial es un 15% estimado para revisar contratos y volúmenes.",
                })
        
        return recommendations
    
    def _recommend_anomaly_investigation(self, docs: list) -> list:
        """Recomienda investigación de anomalías de gasto."""
        recommendations = []
        
        # Agrupar por proveedor
        providers_data = defaultdict(list)
        
        for doc in docs:
            if doc.proveedor:
                providers_data[doc.proveedor].append(doc.monto_total or 0)
        
        for provider, montos in providers_data.items():
            if len(montos) >= 2:
                avg_monto = sum(montos) / len(montos)
                max_monto = max(montos)
                
                # Si hay un monto significativamente mayor, investigar
                if max_monto > avg_monto * 2.5:
                    recommendations.append({
                        "tipo": "ALZA_ANÓMALA_PROVEEDOR",
                        "severidad": "MEDIA",
                        "proveedor": provider,
                        "promedio_histórico": round(avg_monto, 2),
                        "monto_máximo": round(max_monto, 2),
                        "incremento_porcentaje": round(((max_monto - avg_monto) / avg_monto * 100), 1),
                        "ahorro_potencial": round((max_monto - avg_monto), 2),
                        "impacto_potencial": round((max_monto - avg_monto), 2),
                        "mensaje": f"{provider} tuvo un incremento de {round(((max_monto - avg_monto) / avg_monto * 100), 1)}% en último gasto",
                        "accion": f"Contactar a {provider} para aclarar el incremento de precio",
                        "criterio": "Un cobro supera 2,5 veces el promedio histórico de este proveedor. El ahorro es la diferencia respecto al promedio.",
                    })
        
        return recommendations


# Instancia global
recommender_engine = None


def get_recommender_engine(db: Session, empresa_id: str) -> RecommendationEngine:
    """Obtiene una instancia del motor de recomendaciones."""
    return RecommendationEngine(db, empresa_id)
