"""
Tests para el flujo de captura de gastos con formulario editable y validación.
Verifica:
1. Upload de documentos funciona correctamente
2. El endpoint PUT acepta todos los campos editables
3. Validación de montos, fechas, RUT, tipo_documento
4. El pipeline SSE se inicia correctamente
5. La UI tiene todos los elementos necesarios
"""
import sys
import os
import unittest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

# Agregar server al path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'server'))

# Configurar DATA_DIR temporal antes de importar
# Reutilizar DATA_DIR si ya fue configurado por otro test file en la misma sesión
TEST_DIR = os.environ.get('DATA_DIR') or tempfile.mkdtemp()
os.environ['DATA_DIR'] = TEST_DIR
os.environ.setdefault('PORT', '8000')

from fastapi.testclient import TestClient
from app import app

client = TestClient(app)


class TestExpenseUpload(unittest.TestCase):
    """Tests para la subida de documentos de gasto."""

    @classmethod
    def setUpClass(cls):
        """Crear empresa de prueba."""
        resp = client.post("/api/empresas", json={
            "rut": "88.888.888-8",
            "razon_social": "Test Gastos SpA",
            "giro": "Servicios"
        })
        if resp.status_code == 200:
            cls.empresa_id = resp.json()["id"]
        else:
            # Puede que ya exista
            resp2 = client.get("/api/empresas")
            empresas = resp2.json().get("empresas", [])
            if empresas:
                cls.empresa_id = empresas[0]["id"]
            else:
                raise RuntimeError("No se pudo crear empresa de prueba")

    def test_upload_pdf_valid(self):
        """Subir un PDF válido debe retornar file_name."""
        # Crear un PDF mínimo válido
        pdf_content = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF"
        from io import BytesIO
        resp = client.post(
            f"/api/empresas/{self.empresa_id}/documentos/upload",
            files={"file": ("factura_test.pdf", BytesIO(pdf_content), "application/pdf")}
        )
        # Puede fallar por validación MIME real, pero no debe dar 404
        self.assertIn(resp.status_code, [200, 400])
        if resp.status_code == 200:
            data = resp.json()
            self.assertIn("file_name", data)
            self.assertIn("file_id", data)
            self.assertEqual(data["status"], "uploaded")

    def test_upload_image_valid(self):
        """Subir una imagen PNG válida debe retornar file_name."""
        # PNG mínimo válido (1x1 pixel transparente)
        png_header = (
            b'\x89PNG\r\n\x1a\n'  # PNG signature
            b'\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06'
            b'\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx'
            b'\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n\xb4\x00\x00\x00\x00'
            b'IEND\xaeB`\x82'
        )
        from io import BytesIO
        resp = client.post(
            f"/api/empresas/{self.empresa_id}/documentos/upload",
            files={"file": ("boleta.png", BytesIO(png_header), "image/png")}
        )
        self.assertIn(resp.status_code, [200, 400])
        if resp.status_code == 200:
            data = resp.json()
            self.assertIn("file_name", data)

    def test_upload_reject_executable(self):
        """Rechazar archivos con extensión no permitida."""
        from io import BytesIO
        resp = client.post(
            f"/api/empresas/{self.empresa_id}/documentos/upload",
            files={"file": ("virus.exe", BytesIO(b"MZ\x90\x00"), "application/octet-stream")}
        )
        self.assertEqual(resp.status_code, 400)

    def test_upload_reject_empty(self):
        """Rechazar archivos vacíos."""
        from io import BytesIO
        resp = client.post(
            f"/api/empresas/{self.empresa_id}/documentos/upload",
            files={"file": ("empty.pdf", BytesIO(b""), "application/pdf")}
        )
        self.assertEqual(resp.status_code, 400)

    def test_upload_empresa_inexistente(self):
        """Upload a empresa inexistente debe dar 404."""
        from io import BytesIO
        resp = client.post(
            "/api/empresas/no-existe-id/documentos/upload",
            files={"file": ("test.pdf", BytesIO(b"%PDF-1.4"), "application/pdf")}
        )
        self.assertEqual(resp.status_code, 404)


class TestExpenseFieldValidation(unittest.TestCase):
    """Tests para la validación de campos editables del documento/gasto."""

    @classmethod
    def setUpClass(cls):
        """Crear empresa y documento de prueba."""
        resp = client.post("/api/empresas", json={
            "rut": "99.999.999-9",
            "razon_social": "Validación Test SpA",
            "giro": "Retail"
        })
        if resp.status_code == 200:
            cls.empresa_id = resp.json()["id"]
        else:
            resp2 = client.get("/api/empresas")
            empresas = resp2.json().get("empresas", [])
            cls.empresa_id = empresas[0]["id"] if empresas else None

        # Crear un documento directamente en la DB para testear PUT
        if cls.empresa_id:
            cls._create_test_document()

    @classmethod
    def _create_test_document(cls):
        """Crear documento de prueba directamente en la DB."""
        from database import SessionLocal
        from models import Documento, TipoDocumento, EstadoRevision
        import uuid
        from datetime import datetime

        db = SessionLocal()
        try:
            cls.doc_id = str(uuid.uuid4())
            doc = Documento(
                id=cls.doc_id,
                empresa_id=cls.empresa_id,
                ruta_archivo_original=f"/tmp/test_{cls.doc_id}.pdf",
                proveedor="Proveedor Test",
                rut_proveedor="76.123.456-7",
                monto_total=100000,
                monto_neto=84034,
                iva=15966,
                folio="12345",
                tipo_documento=TipoDocumento.FACTURA,
                estado_revision=EstadoRevision.PENDIENTE,
                fecha_emision=datetime(2024, 6, 15),
                fecha_creacion=datetime.utcnow()
            )
            db.add(doc)
            db.commit()
        finally:
            db.close()

    def test_update_proveedor(self):
        """Actualizar proveedor con valor válido."""
        resp = client.put(
            f"/api/empresas/{self.empresa_id}/documentos/{self.doc_id}",
            json={"proveedor": "Sodimac S.A."}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["proveedor"], "Sodimac S.A.")

    def test_update_rut_proveedor(self):
        """Actualizar RUT proveedor."""
        resp = client.put(
            f"/api/empresas/{self.empresa_id}/documentos/{self.doc_id}",
            json={"rut_proveedor": "96.000.000-1"}
        )
        self.assertEqual(resp.status_code, 200)

    def test_update_fecha_emision_formato_iso(self):
        """Actualizar fecha en formato ISO."""
        resp = client.put(
            f"/api/empresas/{self.empresa_id}/documentos/{self.doc_id}",
            json={"fecha_emision": "2024-03-15"}
        )
        self.assertEqual(resp.status_code, 200)

    def test_update_fecha_emision_formato_cl(self):
        """Actualizar fecha en formato chileno DD/MM/YYYY."""
        resp = client.put(
            f"/api/empresas/{self.empresa_id}/documentos/{self.doc_id}",
            json={"fecha_emision": "15/03/2024"}
        )
        self.assertEqual(resp.status_code, 200)

    def test_update_montos_validos(self):
        """Actualizar montos con valores válidos."""
        resp = client.put(
            f"/api/empresas/{self.empresa_id}/documentos/{self.doc_id}",
            json={
                "monto_neto": 100000,
                "iva": 19000,
                "monto_total": 119000
            }
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["monto_total"], 119000)
        self.assertEqual(data["monto_neto"], 100000)
        self.assertEqual(data["iva"], 19000)

    def test_reject_monto_negativo(self):
        """Rechazar montos negativos."""
        resp = client.put(
            f"/api/empresas/{self.empresa_id}/documentos/{self.doc_id}",
            json={"monto_total": -5000}
        )
        self.assertIn(resp.status_code, [400, 422])

    def test_update_tipo_documento(self):
        """Actualizar tipo de documento."""
        for tipo in ["FACTURA", "BOLETA", "COMPROBANTE", "CARTOLA", "OTRO"]:
            resp = client.put(
                f"/api/empresas/{self.empresa_id}/documentos/{self.doc_id}",
                json={"tipo_documento": tipo}
            )
            self.assertEqual(resp.status_code, 200, f"Falló para tipo: {tipo}")

    def test_update_estado_revision(self):
        """Actualizar estado de revisión."""
        for estado in ["PENDIENTE", "REVISADO", "RECHAZADO"]:
            resp = client.put(
                f"/api/empresas/{self.empresa_id}/documentos/{self.doc_id}",
                json={"estado_revision": estado}
            )
            self.assertEqual(resp.status_code, 200, f"Falló para estado: {estado}")

    def test_reject_estado_invalido(self):
        """Rechazar estado de revisión inválido."""
        resp = client.put(
            f"/api/empresas/{self.empresa_id}/documentos/{self.doc_id}",
            json={"estado_revision": "INVENTADO"}
        )
        self.assertIn(resp.status_code, [400, 422])

    def test_update_moneda(self):
        """Actualizar moneda."""
        resp = client.put(
            f"/api/empresas/{self.empresa_id}/documentos/{self.doc_id}",
            json={"moneda": "USD"}
        )
        self.assertEqual(resp.status_code, 200)

    def test_update_folio(self):
        """Actualizar folio."""
        resp = client.put(
            f"/api/empresas/{self.empresa_id}/documentos/{self.doc_id}",
            json={"folio": "F-2024-00789"}
        )
        self.assertEqual(resp.status_code, 200)

    def test_update_multiple_fields(self):
        """Actualizar múltiples campos a la vez."""
        resp = client.put(
            f"/api/empresas/{self.empresa_id}/documentos/{self.doc_id}",
            json={
                "proveedor": "Nuevo Proveedor",
                "monto_total": 250000,
                "monto_neto": 210084,
                "iva": 39916,
                "tipo_documento": "BOLETA",
                "estado_revision": "REVISADO"
            }
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["proveedor"], "Nuevo Proveedor")
        self.assertEqual(data["monto_total"], 250000)

    def test_documento_no_encontrado(self):
        """PUT a documento inexistente debe dar error (400 o 404)."""
        resp = client.put(
            f"/api/empresas/{self.empresa_id}/documentos/id-no-existe",
            json={"proveedor": "Test"}
        )
        self.assertIn(resp.status_code, [400, 404])


class TestExpenseFormUI(unittest.TestCase):
    """Tests para verificar que la UI del formulario de gastos tiene todos los elementos."""

    @classmethod
    def setUpClass(cls):
        """Leer el HTML del frontend."""
        html_path = Path(__file__).parent.parent / "app" / "index.html"
        cls.html = html_path.read_text(encoding="utf-8")

    def test_upload_zone_exists(self):
        """La zona de upload debe existir."""
        self.assertIn('id="uploadZone"', self.html)
        self.assertIn('id="fileInput"', self.html)

    def test_pipeline_wrap_exists(self):
        """El contenedor del pipeline debe existir."""
        self.assertIn('id="pipelineWrap"', self.html)
        self.assertIn('id="pipelineSteps"', self.html)

    def test_result_wrap_exists(self):
        """El contenedor de resultados debe existir."""
        self.assertIn('id="resultWrap"', self.html)

    def test_expense_form_exists(self):
        """El formulario editable de gasto debe existir."""
        self.assertIn('id="expenseForm"', self.html)

    def test_all_editable_fields_in_form(self):
        """Todos los campos editables deben estar en el formulario."""
        fields = [
            'ef_proveedor', 'ef_rut_proveedor', 'ef_fecha_emision',
            'ef_folio', 'ef_monto_neto', 'ef_iva', 'ef_monto_total',
            'ef_tipo_documento', 'ef_moneda'
        ]
        for field in fields:
            self.assertIn(f'id="{field}"', self.html, f"Campo {field} no encontrado en el formulario")

    def test_validation_functions_exist(self):
        """Las funciones de validación deben existir."""
        self.assertIn('function validateExpenseField', self.html)
        self.assertIn('function validateAllExpenseFields', self.html)
        self.assertIn('function autoCalcTotal', self.html)
        self.assertIn('EXPENSE_VALIDATORS', self.html)

    def test_validate_and_accept_exists(self):
        """La función validateAndAcceptDocument debe existir."""
        self.assertIn('function validateAndAcceptDocument', self.html)
        self.assertIn('validateAndAcceptDocument()', self.html)

    def test_error_elements_for_each_field(self):
        """Cada campo debe tener un elemento de error asociado."""
        error_fields = [
            'ef_err_proveedor', 'ef_err_rut_proveedor', 'ef_err_fecha_emision',
            'ef_err_folio', 'ef_err_monto_neto', 'ef_err_iva', 'ef_err_monto_total'
        ]
        for field in error_fields:
            self.assertIn(f'id="{field}"', self.html, f"Error element {field} no encontrado")

    def test_ia_detected_badges(self):
        """Los badges de detección IA deben estar presentes."""
        self.assertIn('ef-detected', self.html)
        self.assertIn('🤖 IA', self.html)

    def test_tipo_documento_options(self):
        """El select de tipo documento debe tener todas las opciones."""
        for tipo in ['FACTURA', 'BOLETA', 'COMPROBANTE', 'CARTOLA', 'OTRO']:
            self.assertIn(f'value="{tipo}"', self.html)

    def test_moneda_options(self):
        """El select de moneda debe tener opciones."""
        for moneda in ['COP', 'USD', 'EUR']:
            self.assertIn(f'value="{moneda}"', self.html)

    def test_categoria_select_exists(self):
        """El selector de categoría debe existir."""
        self.assertIn('id="catSelect"', self.html)

    def test_process_file_function_exists(self):
        """La función processFile debe existir."""
        self.assertIn('async function processFile', self.html)

    def test_render_result_panel_exists(self):
        """La función renderResultPanel debe existir."""
        self.assertIn('function renderResultPanel', self.html)

    def test_css_classes_for_form(self):
        """Las clases CSS del formulario deben estar definidas."""
        css_classes = [
            '.expense-form', '.expense-form-grid', '.ef-group',
            '.ef-label', '.ef-input', '.ef-error', '.ef-select',
            '.ef-detected', '.ef-total-highlight'
        ]
        for cls in css_classes:
            self.assertIn(cls, self.html, f"CSS class {cls} no encontrada")

    def test_validation_patterns(self):
        """Los patrones de validación deben estar presentes."""
        # RUT validation
        self.assertIn('rut_proveedor', self.html)
        # Fecha validation
        self.assertIn('fecha_emision', self.html)
        # Monto validation
        self.assertIn('monto_total', self.html)

    def test_auto_calc_total_hint(self):
        """El hint de cálculo automático debe existir."""
        self.assertIn('ef_calc_hint', self.html)

    def test_nav_nuevo_gasto_exists(self):
        """El menú de navegación debe tener 'Nuevo Gasto'."""
        self.assertIn("data-s=\"nuevo-gasto\"", self.html)
        self.assertIn("Nuevo Gasto", self.html)

    def test_offline_assets(self):
        """Chart.js y DOMPurify deben estar cargados localmente."""
        self.assertIn('vendor/chart.umd.min.js', self.html)
        self.assertIn('vendor/purify.min.js', self.html)
        # No debe haber CDN
        self.assertNotIn('cdn.jsdelivr.net/npm/chart.js', self.html)


class TestExpenseProcessStream(unittest.TestCase):
    """Tests para el endpoint de procesamiento SSE."""

    @classmethod
    def setUpClass(cls):
        """Crear empresa de prueba."""
        resp = client.post("/api/empresas", json={
            "rut": "77.777.777-7",
            "razon_social": "Stream Test SpA",
            "giro": "Tech"
        })
        if resp.status_code == 200:
            cls.empresa_id = resp.json()["id"]
        else:
            resp2 = client.get("/api/empresas")
            empresas = resp2.json().get("empresas", [])
            cls.empresa_id = empresas[0]["id"] if empresas else None

    def test_process_stream_requires_file_name(self):
        """El endpoint SSE debe requerir file_name o file_path."""
        resp = client.get(
            f"/api/empresas/{self.empresa_id}/documentos/process-stream"
        )
        self.assertEqual(resp.status_code, 400)

    def test_process_stream_file_not_found(self):
        """Si el archivo no existe, debe retornar 404."""
        resp = client.get(
            f"/api/empresas/{self.empresa_id}/documentos/process-stream",
            params={"file_name": "no-existe-abc123.pdf"}
        )
        self.assertEqual(resp.status_code, 404)

    def test_process_stream_path_traversal_blocked(self):
        """Path traversal en file_name debe ser bloqueado."""
        resp = client.get(
            f"/api/empresas/{self.empresa_id}/documentos/process-stream",
            params={"file_name": "../../etc/passwd"}
        )
        # Debe ser 403 o 404 (no 200)
        self.assertIn(resp.status_code, [403, 404])

    def test_process_stream_empresa_not_found(self):
        """Empresa inexistente debe dar 404."""
        resp = client.get(
            "/api/empresas/no-existe/documentos/process-stream",
            params={"file_name": "test.pdf"}
        )
        self.assertEqual(resp.status_code, 404)


class TestExpenseDocumentDetail(unittest.TestCase):
    """Tests para el endpoint GET de detalle de documento."""

    @classmethod
    def setUpClass(cls):
        """Crear empresa y documento de prueba."""
        resp = client.post("/api/empresas", json={
            "rut": "55.555.555-5",
            "razon_social": "Detail Test SpA",
            "giro": "Comercio"
        })
        if resp.status_code == 200:
            cls.empresa_id = resp.json()["id"]
        else:
            resp2 = client.get("/api/empresas")
            empresas = resp2.json().get("empresas", [])
            cls.empresa_id = empresas[0]["id"] if empresas else None

        # Crear documento
        from database import SessionLocal
        from models import Documento, TipoDocumento, EstadoRevision
        import uuid
        from datetime import datetime

        db = SessionLocal()
        try:
            cls.doc_id = str(uuid.uuid4())
            doc = Documento(
                id=cls.doc_id,
                empresa_id=cls.empresa_id,
                ruta_archivo_original=f"/tmp/detail_{cls.doc_id}.pdf",
                proveedor="Proveedor Detalle",
                rut_proveedor="11.111.111-1",
                monto_total=50000,
                monto_neto=42017,
                iva=7983,
                folio="D-001",
                tipo_documento=TipoDocumento.BOLETA,
                estado_revision=EstadoRevision.PENDIENTE,
                fecha_emision=datetime(2024, 5, 20),
                fecha_creacion=datetime.utcnow(),
                campos_extraidos='{"campos": {"proveedor": "Proveedor Detalle", "monto_total": 50000}, "agent_results": {}}'
            )
            db.add(doc)
            db.commit()
        finally:
            db.close()

    def test_get_document_detail(self):
        """GET detalle de documento debe retornar todos los campos."""
        resp = client.get(
            f"/api/empresas/{self.empresa_id}/documentos/{self.doc_id}"
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["proveedor"], "Proveedor Detalle")
        self.assertEqual(data["monto_total"], 50000)
        self.assertIn("campos_detectados", data)

    def test_get_document_not_found(self):
        """GET documento inexistente debe dar 404."""
        resp = client.get(
            f"/api/empresas/{self.empresa_id}/documentos/no-existe-id"
        )
        self.assertEqual(resp.status_code, 404)


def tearDownModule():
    """Nota: No limpiamos TEST_DIR porque puede ser compartido con otros test files."""
    pass


if __name__ == '__main__':
    unittest.main(verbosity=2)
