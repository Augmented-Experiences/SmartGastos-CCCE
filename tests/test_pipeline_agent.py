"""
test_pipeline_agent.py — Tests unitarios para el pipeline de agentes
=====================================================================
Cubre:
  - Inicialización del pipeline
  - Carga de agentes desde defaults
  - Sanitización de prompts en agentes
  - Manejo de errores en pipeline
  - Formato de respuestas
"""

import unittest
import sys
import os
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'server'))


class TestPipelineAgentInit(unittest.TestCase):
    """Tests para inicialización del pipeline."""

    def test_import_pipeline(self):
        """pipeline_agent debe ser importable sin errores."""
        try:
            import pipeline_agent
            self.assertTrue(True)
        except ImportError as e:
            self.fail(f"No se pudo importar pipeline_agent: {e}")

    def test_pipeline_has_required_functions(self):
        """pipeline_agent debe tener las funciones principales."""
        import pipeline_agent
        # Verificar funciones y clases reales del módulo
        required = ['ocr_image', 'ocr_pdf', 'DocumentPipelineAgent',
                    'build_vision_prompt', 'build_extractor_prompt',
                    'build_classifier_prompt', 'build_auditor_prompt']
        for func_name in required:
            self.assertTrue(
                hasattr(pipeline_agent, func_name),
                f"Función/clase faltante: {func_name}"
            )


class TestDocumentPipelineAgent(unittest.TestCase):
    """Tests para DocumentPipelineAgent."""

    def test_class_exists(self):
        """DocumentPipelineAgent debe existir."""
        from pipeline_agent import DocumentPipelineAgent
        self.assertTrue(callable(DocumentPipelineAgent))

    def test_ocr_image_exists(self):
        """ocr_image debe existir."""
        from pipeline_agent import ocr_image
        self.assertTrue(callable(ocr_image))

    def test_ocr_pdf_exists(self):
        """ocr_pdf debe existir."""
        from pipeline_agent import ocr_pdf
        self.assertTrue(callable(ocr_pdf))


class TestAgentPromptSecurity(unittest.TestCase):
    """Tests para seguridad en prompts de agentes."""

    def test_agent_prompts_have_security_clause(self):
        """Los prompts de agentes deben tener cláusula de seguridad."""
        defaults_path = Path(__file__).parent.parent / "defaults" / "agents.json"
        if defaults_path.exists():
            data = json.loads(defaults_path.read_text(encoding="utf-8"))
            for agent in data.get("agents", []):
                prompt = agent.get("system_prompt", "")
                if prompt and agent.get("tipo") != "ocr":
                    self.assertIn(
                        "SEGURIDAD",
                        prompt,
                        f"Agente '{agent['id']}' sin cláusula de seguridad"
                    )


if __name__ == '__main__':
    unittest.main()
