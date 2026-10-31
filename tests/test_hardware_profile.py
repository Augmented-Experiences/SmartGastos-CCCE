"""Perfiles de RAM para elegir modelo Ollama."""
import unittest
from unittest.mock import patch

from hardware_profile import select_profile


class TestHardwareProfile(unittest.TestCase):
    def test_6gb_uses_1b_without_vision(self):
        p = select_profile(6)
        self.assertEqual(p["id"], "liviano")
        self.assertEqual(p["model"], "llama3.2:1b")
        self.assertEqual(p["extra_models"], [])

    def test_access_block_warn_ok(self):
        from hardware_profile import access_for_ram
        self.assertEqual(access_for_ram(6.9)["level"], "block")
        self.assertEqual(access_for_ram(7.0)["level"], "warn")
        self.assertEqual(access_for_ram(8)["level"], "warn")
        self.assertEqual(access_for_ram(12)["level"], "warn")
        self.assertEqual(access_for_ram(16)["level"], "ok")
        self.assertEqual(access_for_ram(0)["level"], "ok")

    def test_access_override_env(self):
        from hardware_profile import access_for_ram
        with patch.dict("os.environ", {"SMARTSUITE_ALLOW_LOW_RAM": "1"}):
            self.assertEqual(access_for_ram(4)["level"], "ok")
            self.assertTrue(access_for_ram(4)["override"])

    def test_8gb_uses_3b_without_moondream(self):
        p = select_profile(8)
        self.assertEqual(p["id"], "estandar")
        self.assertEqual(p["model"], "llama3.2:3b")
        self.assertEqual(p["extra_models"], [])

    def test_12gb_stays_on_standard(self):
        p = select_profile(12)
        self.assertEqual(p["id"], "estandar")
        self.assertEqual(p["model"], "llama3.2:3b")

    def test_16gb_adds_moondream(self):
        p = select_profile(16)
        self.assertEqual(p["id"], "completo")
        self.assertEqual(p["model"], "llama3.2:3b")
        self.assertIn("moondream", p["extra_models"])

    def test_32gb_uses_8b(self):
        p = select_profile(32)
        self.assertEqual(p["id"], "maximo")
        self.assertEqual(p["model"], "llama3.1:8b")
        self.assertIn("moondream", p["extra_models"])

    def test_boundary_just_below_7_is_liviano(self):
        self.assertEqual(select_profile(6.9)["id"], "liviano")
        self.assertEqual(select_profile(7.0)["id"], "estandar")


if __name__ == "__main__":
    unittest.main()
