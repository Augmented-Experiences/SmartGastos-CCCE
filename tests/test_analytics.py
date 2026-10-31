"""
test_analytics.py — Tests unitarios para módulos de analytics
===============================================================
Cubre:
  - AnalyticsEngine: dashboard, categorías, proveedores, tendencias, anomalías
  - RecommendationEngine: recomendaciones
  - ExportEngine: CSV, XLSX, PDF
"""

import unittest
import sys
import os
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'server'))


class TestAnalyticsEngine(unittest.TestCase):
    """Tests para AnalyticsEngine (requiere DB Session)."""

    def test_class_importable(self):
        """AnalyticsEngine debe ser importable."""
        from analytics.analyzer import AnalyticsEngine
        self.assertTrue(callable(AnalyticsEngine))

    def test_has_required_methods(self):
        """AnalyticsEngine debe tener métodos requeridos."""
        from analytics.analyzer import AnalyticsEngine
        required = ['get_dashboard_summary', 'get_expenses_by_category',
                    'get_expenses_by_provider', 'get_monthly_trend',
                    'get_anomalies_and_alerts']
        for method in required:
            self.assertTrue(hasattr(AnalyticsEngine, method), f"Método faltante: {method}")


class TestRecommendationEngine(unittest.TestCase):
    """Tests para RecommendationEngine."""

    def test_class_importable(self):
        """RecommendationEngine debe ser importable."""
        from analytics.recommender import RecommendationEngine
        self.assertTrue(callable(RecommendationEngine))

    def test_has_required_methods(self):
        """RecommendationEngine debe tener métodos requeridos."""
        from analytics.recommender import RecommendationEngine
        required = ['get_recommendations']
        for method in required:
            self.assertTrue(hasattr(RecommendationEngine, method), f"Método faltante: {method}")


class TestExportEngine(unittest.TestCase):
    """Tests para ExportEngine."""

    def test_class_importable(self):
        """ExportEngine debe ser importable."""
        from analytics.exporter import ExportEngine
        self.assertTrue(callable(ExportEngine))

    def test_has_required_methods(self):
        """ExportEngine debe tener métodos requeridos."""
        from analytics.exporter import ExportEngine
        required = ['export_to_csv', 'export_to_xlsx', 'export_to_pdf']
        for method in required:
            self.assertTrue(hasattr(ExportEngine, method), f"Método faltante: {method}")


class TestAnalyticsViaAPI(unittest.TestCase):
    """Tests para analytics a través de la API (con DB real)."""

    def setUp(self):
        from app import app
        from fastapi.testclient import TestClient
        self.client = TestClient(app)
        # Crear empresa de prueba
        r = self.client.post("/api/empresas", json={
            "razon_social": "Test Analytics",
            "rut": "99.999.999-9",
            "pais": "Chile"
        })
        self.empresa_id = r.json().get("id")

    def test_dashboard_endpoint(self):
        if not self.empresa_id:
            self.skipTest("No se pudo crear empresa")
        r = self.client.get(f"/api/empresas/{self.empresa_id}/analytics/dashboard")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("kpis", data)

    def test_categorias_endpoint(self):
        if not self.empresa_id:
            self.skipTest("No se pudo crear empresa")
        r = self.client.get(f"/api/empresas/{self.empresa_id}/analytics/categorias")
        self.assertEqual(r.status_code, 200)

    def test_tendencias_endpoint(self):
        if not self.empresa_id:
            self.skipTest("No se pudo crear empresa")
        r = self.client.get(f"/api/empresas/{self.empresa_id}/analytics/tendencias")
        self.assertEqual(r.status_code, 200)


if __name__ == '__main__':
    unittest.main()
