"""Tests del detector de alucinación OCR y de la prioridad RapidOCR vs VLM."""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

from agents.ocr_quality import (
    document_signal_score,
    looks_hallucinated,
    looks_like_caption,
    pick_ocr_result,
    word_count,
)


BOLETA_TEXT = """
BOLETA ELECTRONICA
RUT 76.123.456-K
Fecha 12/03/2024
TOTAL $12.590
IVA $2.010
"""


class TestDocumentSignal(unittest.TestCase):
    def test_chilean_boleta_has_strong_signal(self):
        score = document_signal_score(BOLETA_TEXT)
        self.assertGreaterEqual(score, 4)

    def test_empty_has_zero_signal(self):
        self.assertEqual(document_signal_score(""), 0)
        self.assertEqual(word_count(""), 0)


class TestHallucination(unittest.TestCase):
    def test_caption_english_is_hallucinated(self):
        text = (
            "The image shows a crumpled paper receipt on a wooden table. "
            "There are some numbers visible but they are hard to read."
        )
        self.assertTrue(looks_like_caption(text))
        self.assertTrue(looks_hallucinated(text))

    def test_caption_spanish_is_hallucinated(self):
        text = (
            "La imagen muestra un papel sobre una mesa. "
            "Se observa una foto borrosa con sombras."
        )
        self.assertTrue(looks_hallucinated(text))

    def test_fluent_text_without_fiscal_tokens_is_hallucinated(self):
        text = (
            "There is faded ink and wrinkles near a coffee cup on the table "
            "surface and the lighting makes everything look yellowish overall today."
        )
        self.assertTrue(looks_hallucinated(text))

    def test_real_boleta_is_not_hallucinated(self):
        self.assertFalse(looks_hallucinated(BOLETA_TEXT))

    def test_repetitive_moondream_loop_is_hallucinated(self):
        text = "\n".join(["TOTAL TOTAL TOTAL"] * 8)
        self.assertTrue(looks_hallucinated(text))

    def test_bbox_only_is_hallucinated(self):
        self.assertTrue(looks_hallucinated("[0.12, 0.34, 0.56, 0.78]"))


class TestPickOcrResult(unittest.TestCase):
    def test_prefers_short_rapidocr_over_long_caption(self):
        chosen = pick_ocr_result(
            [
                {"text": "BOLETA\nRUT 76.123.456-K\nTOTAL $8900", "method": "rapidocr"},
                {
                    "text": "The image shows a receipt on a table with many details "
                    "that look like a Chilean boleta from a supermarket visit.",
                    "method": "vllm_moondream",
                },
            ]
        )
        self.assertEqual(chosen["method"], "rapidocr")
        self.assertIn("76.123.456-K", chosen["text"])

    def test_discards_vlm_when_it_is_the_only_candidate_and_caption(self):
        chosen = pick_ocr_result(
            [
                {
                    "text": "The image shows a photo of a receipt that I cannot read clearly.",
                    "method": "vllm_moondream",
                }
            ]
        )
        self.assertEqual(chosen["method"], "none")
        self.assertEqual(chosen["text"], "")

    def test_allows_vlm_when_classical_empty_and_text_looks_like_boleta(self):
        chosen = pick_ocr_result(
            [
                {"text": "", "method": "rapidocr"},
                {"text": BOLETA_TEXT, "method": "vllm_moondream"},
            ]
        )
        self.assertTrue(chosen["method"].startswith("vllm"))
        self.assertIn("BOLETA", chosen["text"])


class TestOcrVllmDiscardsCaption(unittest.TestCase):
    def test_ocr_vllm_returns_empty_on_caption(self):
        import tempfile

        from agents import ocr_agent

        class _Resp:
            status_code = 200

            def json(self):
                return {
                    "response": "The image shows a receipt on a table. I can see some numbers."
                }

        handle = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        handle.write(b"\xff\xd8\xff")
        handle.close()
        try:
            with patch("requests.post", return_value=_Resp()), patch(
                "ollama_client._ollama_model_names", return_value=[]
            ), patch.object(ocr_agent, "_PIL_OK", False):
                text = ocr_agent.ocr_vllm_ollama(handle.name, "moondream")
        finally:
            os.unlink(handle.name)
        self.assertEqual(text, "")


class TestRapidOcrConfig(unittest.TestCase):
    def test_engine_params_are_valid_for_rapidocr_33(self):
        from agents.rapid_ocr import engine_param_attempts

        attempts = engine_param_attempts()
        nonempty = [p for p in attempts if p]
        if not nonempty:
            self.skipTest("rapidocr no instalado")
        for params in nonempty:
            self.assertNotIn("Global.model_root_dir", params)
            self.assertNotIn("Global.log_level", params)
        latin = attempts[0]
        self.assertTrue(latin)
        rec = str(latin.get("Rec.lang_type", "")).lower()
        self.assertIn("latin", rec)
        ver = str(latin.get("Rec.ocr_version", "")).lower()
        self.assertIn("v4", ver)
        self.assertGreaterEqual(int(latin.get("Global.max_side_len", 0)), 4000)

    def test_seeds_bundled_onnx_into_data_dir(self):
        import tempfile
        from pathlib import Path
        from agents.rapid_ocr import _seed_models

        src = Path(__file__).resolve().parents[1] / "data" / "ocr_models"
        onnxs = list(src.glob("*.onnx")) if src.is_dir() else []
        if len(onnxs) < 1:
            self.skipTest("data/ocr_models sin ONNX (se descargan al construir)")
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "ocr_models"
            dest.mkdir()
            _seed_models(dest)
            copied = list(dest.glob("*.onnx"))
            self.assertGreaterEqual(len(copied), 1)
            self.assertTrue(any(p.stat().st_size > 0 for p in copied))

    def test_backend_spec_bundles_ocr_models(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        spec = (root / "desktop" / "backend" / "backend.spec").read_text(encoding="utf-8")
        self.assertIn('datas.append((str(_ocr_models), "ocr_models"))', spec)
        rapid = (root / "server" / "agents" / "rapid_ocr.py").read_text(encoding="utf-8")
        self.assertIn("_seed_models", rapid)
    def test_stops_at_rapidocr_when_signal_is_good(self):
        from agents import ocr_agent

        with patch("agents.ocr_agent.ocr_rapidocr", return_value=BOLETA_TEXT), patch(
            "agents.ocr_agent.ocr_tesseract"
        ) as tess, patch("agents.ocr_agent.ocr_vllm_ollama") as vllm:
            result = ocr_agent.run_image_ocr("foto.jpg", allow_vllm=True)
        self.assertEqual(result["method"], "rapidocr")
        tess.assert_not_called()
        vllm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
