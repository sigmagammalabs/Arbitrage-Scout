"""Tests fuer den Margenrechner -- die Stelle, an der Rechenfehler Geld kosten.

Aufruf: ``python -m pytest tests -q`` (aus dem Projektverzeichnis).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calculator import CalculationError, MarginCalculator  # noqa: E402
from config import CostConfig, MarginConfig, TaxConfig  # noqa: E402


@pytest.fixture
def calc() -> MarginCalculator:
    return MarginCalculator(
        costs=CostConfig(
            shipping_cost_domestic_eur=5.49,
            packaging_cost_eur=1.20,
            ebay_fee_percent=12.0,
            ebay_fixed_fee_eur=0.35,
            return_buffer_percent=4.0,
        ),
        margin=MarginConfig(min_roi_percent=25.0, min_profit_eur=12.0),
        tax=TaxConfig(vat_registered=False),
    )


def test_grundformel(calc: MarginCalculator) -> None:
    """Verkauf 100,00 EUR, Einkauf 40,00 EUR -- jeder Posten von Hand nachgerechnet."""
    b = calc.calculate(purchase_price_eur=40.0, sale_price_eur=100.0)

    assert b.gross_revenue_eur == 100.0
    assert b.ebay_fee_variable_eur == 12.0     # 12 % von 100
    assert b.ebay_fee_fixed_eur == 0.35
    assert b.return_buffer_eur == 4.0          # 4 % von 100
    assert b.cost_of_goods_eur == 40.0
    assert b.outbound_shipping_eur == 5.49
    assert b.packaging_eur == 1.20
    # 100 - 12 - 0.35 - 4 - 40 - 5.49 - 1.20
    assert b.net_profit_eur == pytest.approx(36.96)
    assert b.capital_employed_eur == pytest.approx(46.69)
    assert b.roi_percent == pytest.approx(79.16, abs=0.01)
    assert b.is_recommended is True
    assert b.rejection_reasons == []


def test_versand_zaehlt_zum_umsatz(calc: MarginCalculator) -> None:
    """eBay berechnet die Provision auf Artikelpreis plus Versand."""
    b = calc.calculate(
        purchase_price_eur=10.0, sale_price_eur=50.0, sale_shipping_income_eur=4.99
    )
    assert b.gross_revenue_eur == 54.99
    assert b.ebay_fee_variable_eur == pytest.approx(6.60, abs=0.01)


def test_gebinde_wird_normiert(calc: MarginCalculator) -> None:
    """5er-Pack einkaufen, Einzelstueck verkaufen: nur 1/5 des Einkaufs zaehlt."""
    b = calc.calculate(
        purchase_price_eur=50.0,
        sale_price_eur=30.0,
        quantity_source=5,
        quantity_target=1,
    )
    assert b.units_purchased_per_sale == pytest.approx(0.2)
    assert b.cost_of_goods_eur == pytest.approx(10.0)


def test_gebinde_umgekehrt(calc: MarginCalculator) -> None:
    """Einzeln einkaufen, als Dreierpack verkaufen: dreifacher Wareneinsatz."""
    b = calc.calculate(
        purchase_price_eur=8.0,
        sale_price_eur=40.0,
        quantity_source=1,
        quantity_target=3,
    )
    assert b.cost_of_goods_eur == pytest.approx(24.0)


def test_einkaufsversand_zaehlt_zum_wareneinsatz(calc: MarginCalculator) -> None:
    b = calc.calculate(
        purchase_price_eur=20.0, purchase_shipping_eur=3.50, sale_price_eur=60.0
    )
    assert b.cost_of_goods_eur == pytest.approx(23.50)


def test_schwellen_lehnen_duenne_marge_ab(calc: MarginCalculator) -> None:
    b = calc.calculate(purchase_price_eur=30.0, sale_price_eur=45.0)
    assert b.is_recommended is False
    assert len(b.rejection_reasons) >= 1


def test_break_even_ergibt_null_gewinn(calc: MarginCalculator) -> None:
    """Zum Break-even-Preis verkauft, bleibt exakt nichts uebrig."""
    b = calc.calculate(purchase_price_eur=25.0, sale_price_eur=100.0)
    at_break_even = calc.calculate(
        purchase_price_eur=25.0, sale_price_eur=b.break_even_price_eur
    )
    assert at_break_even.net_profit_eur == pytest.approx(0.0, abs=0.02)


def test_mindestpreis_erfuellt_beide_schwellen(calc: MarginCalculator) -> None:
    b = calc.calculate(purchase_price_eur=25.0, sale_price_eur=100.0)
    at_min = calc.calculate(
        purchase_price_eur=25.0, sale_price_eur=b.min_viable_sale_price_eur
    )
    assert at_min.is_recommended is True
    assert at_min.net_profit_eur >= 12.0 - 0.02
    assert at_min.roi_percent >= 25.0 - 0.05


def test_umsatzsteuer_bei_regelbesteuerung() -> None:
    calc = MarginCalculator(
        costs=CostConfig(),
        margin=MarginConfig(),
        tax=TaxConfig(vat_registered=True, vat_rate_percent=19.0),
    )
    b = calc.calculate(purchase_price_eur=20.0, sale_price_eur=119.0)
    assert b.vat_on_sale_eur == pytest.approx(19.0, abs=0.01)


def test_vorsteuerabzug_senkt_wareneinsatz() -> None:
    calc = MarginCalculator(
        costs=CostConfig(),
        margin=MarginConfig(),
        tax=TaxConfig(vat_registered=True, input_vat_deductible=True, vat_rate_percent=19.0),
    )
    b = calc.calculate(purchase_price_eur=119.0, sale_price_eur=250.0)
    assert b.input_vat_credit_eur == pytest.approx(19.0, abs=0.01)
    assert b.cost_of_goods_eur == pytest.approx(100.0, abs=0.01)


def test_ungueltige_eingaben(calc: MarginCalculator) -> None:
    with pytest.raises(CalculationError):
        calc.calculate(purchase_price_eur=10.0, sale_price_eur=0.0)
    with pytest.raises(CalculationError):
        calc.calculate(purchase_price_eur=-1.0, sale_price_eur=10.0)
    with pytest.raises(CalculationError):
        calc.calculate(purchase_price_eur=10.0, sale_price_eur=20.0, quantity_source=0)


def test_unmoegliche_gebuehren_werden_abgelehnt() -> None:
    with pytest.raises(ValueError):
        CostConfig(ebay_fee_percent=90.0, return_buffer_percent=15.0)


def test_vorsteuer_ohne_registrierung_abgelehnt() -> None:
    with pytest.raises(ValueError):
        TaxConfig(vat_registered=False, input_vat_deductible=True)
