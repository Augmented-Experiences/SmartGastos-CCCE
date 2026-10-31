"""
test_cross_platform.py — Compatibilidad del servidor y de las dependencias.
El instalador de escritorio no incluye el launcher Pinokio.
"""

import json
import os
import sys
import unittest
from pathlib import Path, PureWindowsPath, PurePosixPath
from unittest.mock import patch

# Agregar el directorio raíz al path
ROOT_DIR = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT_DIR))


class TestRequirements(unittest.TestCase):
    """Verifica que requirements.txt no tiene paquetes problemáticos."""

    def setUp(self):
        with open(ROOT_DIR / "requirements.txt", "r", encoding="utf-8") as f:
            self.packages = [l.strip().lower() for l in f.readlines()
                             if l.strip() and not l.startswith("#")]

    def test_no_linux_only_packages(self):
        """requirements.txt no tiene paquetes solo-Linux."""
        linux_only = ["python-magic-bin", "uvloop", "gunicorn"]
        for pkg in linux_only:
            for line in self.packages:
                self.assertNotIn(pkg, line,
                                 f"requirements.txt tiene paquete Linux-only: {pkg}")

    def test_has_essential_packages(self):
        """requirements.txt tiene los paquetes esenciales."""
        content = "\n".join(self.packages)
        essentials = ["fastapi", "uvicorn", "pillow", "pdf2image"]
        for pkg in essentials:
            self.assertIn(pkg, content,
                          f"Falta paquete esencial: {pkg}")


class TestServerApp(unittest.TestCase):
    """Verifica que server/app.py no tiene problemas de compatibilidad."""

    def setUp(self):
        with open(ROOT_DIR / "server" / "app.py", "r", encoding="utf-8") as f:
            self.content = f.read()

    def test_uses_127_0_0_1(self):
        """El proceso escucha en 127.0.0.1. localhost solo aparece en el origen CORS loopback."""
        self.assertIn("127.0.0.1", self.content)
        self.assertNotIn('host="localhost"', self.content)
        self.assertNotIn("host='localhost'", self.content)
        lines = self.content.split("\n")
        for i, line in enumerate(lines, 1):
            if "localhost" not in line or line.strip().startswith("#"):
                continue
            if "allow_origin_regex" in line:
                continue
            self.fail(f"server/app.py línea {i} usa 'localhost': {line.strip()}")

    def test_no_unix_specific_imports(self):
        """server/app.py no importa módulos Unix-only."""
        unix_modules = ["import fcntl", "import grp", "import pwd", "import resource"]
        for mod in unix_modules:
            self.assertNotIn(mod, self.content,
                             f"server/app.py importa módulo Unix-only: {mod}")


class TestNoOldJsFiles(unittest.TestCase):
    """Verifica que no existen archivos .js de scripts (scripts sueltos)."""

    def test_no_install_js(self):
        """No existe install.js."""
        self.assertFalse((ROOT_DIR / "install.js").exists(),
                         "install.js existe — debe eliminarse (conflicta con install.json)")

    def test_no_start_js(self):
        """No existe start.js."""
        self.assertFalse((ROOT_DIR / "start.js").exists(),
                         "start.js existe — debe eliminarse (conflicta con start.json)")

    def test_no_stop_js(self):
        """No existe stop.js."""
        self.assertFalse((ROOT_DIR / "stop.js").exists(),
                         "stop.js existe — debe eliminarse (conflicta con stop.json)")


if __name__ == "__main__":
    print(f"\n{'='*60}")
    print(f"  PRUEBAS DE COMPATIBILIDAD CROSS-PLATFORM")
    print(f"  SmartGastos escritorio")
    print(f"  Plataforma actual: {sys.platform}")
    print(f"{'='*60}\n")

    unittest.main(verbosity=2)
