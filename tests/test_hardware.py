"""
test_hardware.py — Tests unitarios para el módulo de hardware y semáforo de modelos
=====================================================================================
Cubre:
  - Detección de hardware
  - Estimación de parámetros por modelo
  - Estimación de tokens/segundo
  - Cálculo de grado de rendimiento (S/A/B/C/D/F)
  - Endpoint de readiness
  - Cache de rendimiento
"""

import unittest
import sys
import os
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'server'))


class TestDetectHardware(unittest.TestCase):
    """Tests para detección de hardware."""

    def test_returns_expected_fields(self):
        from hardware import detect_hardware
        hw = detect_hardware()
        expected_fields = ['ram_gb', 'gpu_name', 'gpu_vram_gb', 'cpu_cores',
                          'cpu_name', 'is_apple_silicon', 'os', 'arch']
        for field in expected_fields:
            self.assertIn(field, hw, f"Campo faltante: {field}")

    def test_ram_is_positive(self):
        from hardware import detect_hardware
        hw = detect_hardware()
        self.assertGreater(hw['ram_gb'], 0, "RAM debe ser positiva")

    def test_cpu_cores_positive(self):
        from hardware import detect_hardware
        hw = detect_hardware()
        self.assertGreater(hw['cpu_cores'], 0, "CPU cores debe ser positivo")


class TestEstimateParams(unittest.TestCase):
    """Tests para estimación de parámetros de modelos."""

    def setUp(self):
        from hardware import _estimate_params
        self.estimate = _estimate_params

    def test_known_models(self):
        """Modelos conocidos retornan parámetros correctos."""
        self.assertEqual(self.estimate("llama3.2:3b"), 3.0)
        self.assertEqual(self.estimate("llama3.1:8b"), 8.0)
        self.assertEqual(self.estimate("llama3.2:1b"), 1.0)

    def test_unknown_model_with_size_in_name(self):
        """Modelos desconocidos con tamaño en el nombre extraen el tamaño."""
        self.assertEqual(self.estimate("custom:13b"), 13.0)

    def test_completely_unknown_model(self):
        """Modelos completamente desconocidos retornan default 7.0."""
        self.assertEqual(self.estimate("unknown-model"), 7.0)


class TestEstimateTps(unittest.TestCase):
    """Tests para estimación de tokens/segundo."""

    def setUp(self):
        from hardware import _estimate_tps
        self.estimate = _estimate_tps

    def test_more_ram_better_performance(self):
        """Más RAM = mejor rendimiento para el mismo modelo."""
        hw_low = {'ram_gb': 4, 'gpu_vram_gb': 0, 'is_apple_silicon': False, 'cpu_cores': 4}
        hw_high = {'ram_gb': 32, 'gpu_vram_gb': 0, 'is_apple_silicon': False, 'cpu_cores': 4}
        tps_low = self.estimate(hw_low, 7.0)
        tps_high = self.estimate(hw_high, 7.0)
        self.assertGreaterEqual(tps_high, tps_low)

    def test_gpu_faster_than_cpu(self):
        """GPU debe ser más rápida que CPU-only."""
        hw_cpu = {'ram_gb': 16, 'gpu_vram_gb': 0, 'is_apple_silicon': False, 'cpu_cores': 8}
        hw_gpu = {'ram_gb': 16, 'gpu_vram_gb': 8, 'is_apple_silicon': False, 'cpu_cores': 8}
        tps_cpu = self.estimate(hw_cpu, 7.0)
        tps_gpu = self.estimate(hw_gpu, 7.0)
        self.assertGreater(tps_gpu, tps_cpu)

    def test_smaller_model_faster(self):
        """Modelos más pequeños deben ser más rápidos."""
        hw = {'ram_gb': 16, 'gpu_vram_gb': 0, 'is_apple_silicon': False, 'cpu_cores': 8}
        tps_small = self.estimate(hw, 1.0)
        tps_large = self.estimate(hw, 13.0)
        self.assertGreater(tps_small, tps_large)

    def test_always_positive(self):
        """TPS siempre debe ser positivo."""
        hw = {'ram_gb': 2, 'gpu_vram_gb': 0, 'is_apple_silicon': False, 'cpu_cores': 1}
        tps = self.estimate(hw, 70.0)
        self.assertGreater(tps, 0)


class TestComputeGrade(unittest.TestCase):
    """Tests para cálculo de grado de rendimiento."""

    def setUp(self):
        from hardware import _compute_grade
        self.grade = _compute_grade

    def test_high_tps_gets_s_or_a(self):
        """TPS alto obtiene grado S o A."""
        result = self.grade(35.0, 3.0, 16.0)
        self.assertIn(result['grade'], ['S', 'A'])

    def test_low_tps_gets_d_or_f(self):
        """TPS bajo obtiene grado D o F."""
        result = self.grade(1.5, 7.0, 16.0)
        self.assertIn(result['grade'], ['D', 'F'])

    def test_insufficient_ram_gets_f(self):
        """RAM insuficiente obtiene grado F."""
        result = self.grade(10.0, 70.0, 4.0)  # 70B model, 4GB RAM
        self.assertEqual(result['grade'], 'F')

    def test_result_has_all_fields(self):
        """El resultado tiene todos los campos esperados."""
        result = self.grade(15.0, 7.0, 16.0)
        for field in ['grade', 'color', 'label', 'description']:
            self.assertIn(field, result)


class TestGetReadiness(unittest.TestCase):
    """Tests para endpoint de readiness."""

    def test_returns_expected_fields(self):
        from hardware import get_readiness
        with tempfile.TemporaryDirectory() as tmpdir:
            result = get_readiness(Path(tmpdir))
            expected = ['ready', 'status', 'ollama', 'model_ready',
                       'default_model', 'available_models', 'models_count', 'message']
            for field in expected:
                self.assertIn(field, result, f"Campo faltante: {field}")

    def test_offline_ollama(self):
        """Cuando Ollama no está corriendo, ready=False."""
        from hardware import get_readiness
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch('hardware.check_ollama', return_value=False):
                result = get_readiness(Path(tmpdir))
                self.assertFalse(result['ready'])
                self.assertFalse(result['ollama'])


class TestHardwarePerformanceCache(unittest.TestCase):
    """Tests para cache de rendimiento de hardware."""

    def test_cache_is_created(self):
        from hardware import get_hardware_performance
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            with patch('hardware.get_available_models', return_value=['llama3.2:3b']):
                result = get_hardware_performance(data_dir, force=True)
                cache_file = data_dir / "hardware_perf_cache.json"
                self.assertTrue(cache_file.exists())

    def test_cache_is_used(self):
        from hardware import get_hardware_performance
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            # Crear cache manual
            cache = {"hardware": {"ram_gb": 16}, "models": [], "total_models": 0}
            cache_file = data_dir / "hardware_perf_cache.json"
            cache_file.write_text(json.dumps(cache))
            result = get_hardware_performance(data_dir, force=False)
            self.assertEqual(result['hardware']['ram_gb'], 16)


if __name__ == '__main__':
    unittest.main()
