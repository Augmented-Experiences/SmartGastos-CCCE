#!/usr/bin/env python3
"""
run_tests.py — Ejecuta toda la suite de tests unitarios
=========================================================
Uso:
  python run_tests.py              # Ejecutar todos los tests
  python run_tests.py -v           # Verbose
  python run_tests.py test_security # Solo tests de seguridad
"""

import sys
import os
import unittest
import tempfile

# Configurar entorno antes de importar tests
os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix="pyme_test_"))
os.environ.setdefault('OLLAMA_HOST', 'http://127.0.0.1:11434')

# Agregar server/ al path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'server'))

def main():
    # Directorio de tests
    test_dir = os.path.join(os.path.dirname(__file__), 'tests')

    # Si se especifica un módulo específico
    if len(sys.argv) > 1 and not sys.argv[1].startswith('-'):
        module = sys.argv[1]
        if not module.startswith('test_'):
            module = f'test_{module}'
        suite = unittest.TestLoader().loadTestsFromName(f'tests.{module}')
    else:
        # Descubrir todos los tests
        suite = unittest.TestLoader().discover(test_dir, pattern='test_*.py')

    # Ejecutar
    verbosity = 2 if '-v' in sys.argv else 1
    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)

    # Resumen
    print("\n" + "=" * 60)
    print(f"Tests ejecutados: {result.testsRun}")
    print(f"Exitosos: {result.testsRun - len(result.failures) - len(result.errors)}")
    print(f"Fallos: {len(result.failures)}")
    print(f"Errores: {len(result.errors)}")
    print("=" * 60)

    sys.exit(0 if result.wasSuccessful() else 1)

if __name__ == '__main__':
    main()
