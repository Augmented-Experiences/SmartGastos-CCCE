"""Tipo de cambio: los totales analíticos quedan en moneda local."""
from analytics.analyzer import local_amount, fx_rate


class _Doc:
    def __init__(self, monto_total=0, tipo_cambio=1):
        self.monto_total = monto_total
        self.tipo_cambio = tipo_cambio


def test_local_amount_multiplies_by_rate():
    doc = _Doc(monto_total=10, tipo_cambio=950)
    assert fx_rate(doc) == 950
    assert local_amount(doc, "monto_total") == 9500


def test_missing_or_invalid_rate_counts_as_one():
    assert local_amount(_Doc(monto_total=40, tipo_cambio=None), "monto_total") == 40
    assert local_amount(_Doc(monto_total=40, tipo_cambio=0), "monto_total") == 40
    assert local_amount(_Doc(monto_total=40, tipo_cambio=float("inf")), "monto_total") == 40


def test_infinite_amount_does_not_propagate():
    assert local_amount(_Doc(monto_total=float("inf"), tipo_cambio=2), "monto_total") == 0
