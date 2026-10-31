"""
conftest.py — Configuración global de tests
=============================================
Configura variables de entorno y paths necesarios
para ejecutar los tests sin un servidor real.
"""

import os
import sys
import tempfile

# Agregar server/ al path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'server'))

# Configurar DATA_DIR temporal para tests
_test_tmpdir = tempfile.mkdtemp(prefix="pyme_ledger_test_")
os.environ.setdefault('DATA_DIR', _test_tmpdir)
os.environ.setdefault('OLLAMA_HOST', 'http://127.0.0.1:11434')
