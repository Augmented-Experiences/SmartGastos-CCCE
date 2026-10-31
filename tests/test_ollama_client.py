"""
test_ollama_client.py — Tests unitarios para el wrapper centralizado de Ollama
================================================================================
Cubre:
  - Verificación de disponibilidad de Ollama
  - Verificación de modelos disponibles
  - Llamadas a generate con sanitización
  - Tracking de uso (tokens, latencia, inyecciones bloqueadas)
  - Manejo de errores y timeouts
"""

import unittest
import sys
import os
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'server'))


class TestCheckOllama(unittest.TestCase):
    """Tests para check_ollama() en hardware.py."""

    def test_function_exists(self):
        """check_ollama debe existir en hardware.py."""
        from hardware import check_ollama
        self.assertTrue(callable(check_ollama))

    def test_returns_bool(self):
        """check_ollama retorna bool (False sin Ollama corriendo)."""
        from hardware import check_ollama
        result = check_ollama()
        self.assertIsInstance(result, bool)


class TestGetAvailableModels(unittest.TestCase):
    """Tests para get_available_models() en hardware.py."""

    def test_function_exists(self):
        """get_available_models debe existir."""
        from hardware import get_available_models
        self.assertTrue(callable(get_available_models))

    def test_returns_list(self):
        """get_available_models retorna lista (vacía sin Ollama)."""
        from hardware import get_available_models
        result = get_available_models()
        self.assertIsInstance(result, list)


class TestIsModelAvailable(unittest.TestCase):
    """Tests para is_model_available() en ollama_client.py."""

    def test_function_exists(self):
        """is_model_available debe existir."""
        from ollama_client import is_model_available
        self.assertTrue(callable(is_model_available))

    def test_returns_bool(self):
        """is_model_available retorna bool."""
        from ollama_client import is_model_available
        result = is_model_available("nonexistent-model")
        self.assertIsInstance(result, bool)


class TestCallOllamaGenerate(unittest.TestCase):
    """Tests para call_ollama_generate()."""

    def test_function_exists(self):
        """call_ollama_generate debe existir."""
        from ollama_client import call_ollama_generate
        self.assertTrue(callable(call_ollama_generate))

    def test_function_returns_dict(self):
        """call_ollama_generate retorna dict incluso sin Ollama."""
        from ollama_client import call_ollama_generate
        result = call_ollama_generate(
            model="llama3.2:3b",
            prompt="test",
            system="test"
        )
        self.assertIsInstance(result, dict)
        self.assertIn('ok', result)


class TestUsageTracking(unittest.TestCase):
    """Tests para tracking de uso."""

    def test_get_usage_stats_returns_dict(self):
        from ollama_client import get_usage_stats
        stats = get_usage_stats()
        self.assertIsInstance(stats, dict)
        expected_fields = ['total_calls', 'successful_calls', 'failed_calls',
                          'injection_attempts_blocked', 'total_input_tokens',
                          'total_output_tokens', 'total_savings_usd', 'avg_latency_ms']
        for field in expected_fields:
            self.assertIn(field, stats, f"Campo faltante: {field}")


class TestOllamaModelFallback(unittest.TestCase):
    """Usa el modelo que el launcher ya bajó (no 503 en chat)."""

    def test_resolve_uses_pulled_model_when_agent_model_missing(self):
        from ollama_client import resolve_ollama_model
        tags = MagicMock()
        tags.status_code = 200
        tags.json.return_value = {"models": [{"name": "llama3.1:8b"}]}
        with patch("ollama_client.requests.get", return_value=tags):
            with patch.dict(os.environ, {"OLLAMA_MODEL": "llama3.1:8b", "RUN_BY_TAURI": "1"}):
                self.assertEqual(resolve_ollama_model("llama3.2:3b"), "llama3.1:8b")

    def test_resolve_keeps_requested_when_present(self):
        from ollama_client import resolve_ollama_model
        tags = MagicMock()
        tags.status_code = 200
        tags.json.return_value = {
            "models": [{"name": "llama3.2:3b"}, {"name": "llama3.1:8b"}]
        }
        with patch("ollama_client.requests.get", return_value=tags):
            self.assertEqual(resolve_ollama_model("llama3.2:3b"), "llama3.2:3b")

    def test_desktop_prefers_launcher_profile_over_heavier_installed(self):
        from ollama_client import resolve_ollama_model
        tags = MagicMock()
        tags.status_code = 200
        tags.json.return_value = {
            "models": [{"name": "llama3.2:3b"}, {"name": "llama3.1:8b"}]
        }
        with patch("ollama_client.requests.get", return_value=tags):
            with patch.dict(os.environ, {"OLLAMA_MODEL": "llama3.2:3b", "RUN_BY_TAURI": "1"}):
                self.assertEqual(resolve_ollama_model("llama3.1:8b"), "llama3.2:3b")

    def test_vision_resolve_keeps_moondream_not_text_llm(self):
        from ollama_client import resolve_ollama_vision_model
        tags = MagicMock()
        tags.status_code = 200
        tags.json.return_value = {
            "models": [{"name": "llama3.1:8b"}, {"name": "moondream"}]
        }
        with patch("ollama_client.requests.get", return_value=tags):
            self.assertEqual(resolve_ollama_vision_model("llama3.2-vision"), "moondream")
            self.assertEqual(resolve_ollama_vision_model("moondream"), "moondream")

    @patch("ollama_client.requests.post")
    @patch("ollama_client.requests.get")
    def test_call_ollama_generate_uses_ram_model_instead_of_503(self, mock_get, mock_post):
        from ollama_client import call_ollama_generate
        tags = MagicMock()
        tags.status_code = 200
        tags.json.return_value = {"models": [{"name": "llama3.1:8b"}]}
        mock_get.return_value = tags
        gen = MagicMock()
        gen.status_code = 200
        gen.encoding = "utf-8"
        gen.json.return_value = {"response": "hola", "thinking": ""}
        gen.raise_for_status = MagicMock()
        mock_post.return_value = gen
        with patch.dict(os.environ, {"OLLAMA_MODEL": "llama3.1:8b", "RUN_BY_TAURI": "1"}):
            result = call_ollama_generate(
                "llama3.2:3b",
                "hi",
                system="test",
                skip_sanitization=True,
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("response"), "hola")
        self.assertEqual(mock_post.call_args.kwargs["json"]["model"], "llama3.1:8b")

    @patch("ollama_client.requests.post")
    @patch("ollama_client.requests.get")
    def test_ocr_generate_uses_moondream_from_tags(self, mock_get, mock_post):
        from ollama_client import call_ollama_generate
        tags = MagicMock()
        tags.status_code = 200
        tags.json.return_value = {
            "models": [{"name": "llama3.1:8b"}, {"name": "moondream"}]
        }
        mock_get.return_value = tags
        gen = MagicMock()
        gen.status_code = 200
        gen.encoding = "utf-8"
        gen.json.return_value = {"response": "factura", "thinking": ""}
        gen.raise_for_status = MagicMock()
        mock_post.return_value = gen
        with patch.dict(os.environ, {"OLLAMA_MODEL": "llama3.1:8b", "RUN_BY_TAURI": "1"}):
            result = call_ollama_generate(
                "moondream",
                "transcribe",
                images=["aaa"],
                skip_sanitization=True,
            )
        self.assertTrue(result.get("ok"))
        self.assertEqual(mock_post.call_args.kwargs["json"]["model"], "moondream")


if __name__ == '__main__':
    unittest.main()
