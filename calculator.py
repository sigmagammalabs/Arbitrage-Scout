"""Netto-Margenrechner.

Grundformel (Kleinunternehmer, Standardfall)::

    Reingewinn = eBay_Brutto
               - eBay_Gebuehren        (prozentual + fix)
               - Einkauf_Brutto        (auf die verkaufte Stueckzahl normiert)
               - Inlandsversand
               - Verpackung
               - Retourenpuffer

Optional zuschaltbar (Standard: aus, damit die Grundformel unveraendert gilt):
Umsatzsteuer bei Regelbesteuerung, Vorsteuerabzug, Einfuhrzoll, Wareneingangs-
versand und separate Zahlungsgebuehren.

Gerechnet wird durchgehend mit :class:`~decimal.Decimal`, damit Cent-Betraege
nicht durch Float-Rundung wandern.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, Field

from config import CostConfig, MarginConfig, Settings, TaxConfig
from logging_utils import get_logger
from models import CandidatePair

logger = get_logger(__name__)

CENT = Decimal("0.01")
ZERO = Decimal("0")


class CalculationError(ValueError):
    """Eingangswerte ergeben keine rechenbare Kalkulation."""


def _dec(value: float | int | str | Decimal) -> Decimal:
    """Float sicher in Decimal wandeln (ueber ``str``, sonst schleppt man den
    Float-Fehler mit)."""
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CalculationError(f"Kein gueltiger Geldbetrag: {value!r}") from exc


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _pct(value: float | Decimal) -> Decimal:
    return _dec(value) / Decimal("100")


class MarginBreakdown(BaseModel):
    """Vollstaendige Aufschluesselung einer Kalkulation.

    Alle Betraege in Euro, auf Cent gerundet. Absichtlich detailliert: bei einer
    abgelehnten Empfehlung will man sehen, welcher Posten sie gekippt hat.
    """

    # Einnahmen
    sale_price_eur: float
    sale_shipping_income_eur: float
    gross_revenue_eur: float

    # Abzuege Verkaufsseite
    vat_on_sale_eur: float
    ebay_fee_variable_eur: float
    ebay_fee_fixed_eur: float
    payment_fee_eur: float
    return_buffer_eur: float

    # Abzuege Beschaffungsseite
    purchase_price_eur: float
    purchase_shipping_eur: float
    import_duty_eur: float
    inbound_shipping_eur: float
    input_vat_credit_eur: float
    cost_of_goods_eur: float          # beschaffungsseitig, auf 1 Verkauf normiert

    # Abzuege Logistik
    outbound_shipping_eur: float
    packaging_eur: float

    # Ergebnis
    total_cost_eur: float
    net_profit_eur: float
    capital_employed_eur: float
    roi_percent: float
    margin_percent: float             # Gewinn bezogen auf den Umsatz

    # Mengennormierung
    quantity_source: int = 1
    quantity_target: int = 1
    units_purchased_per_sale: float = 1.0

    # Entscheidung
    is_recommended: bool = False
    rejection_reasons: list[str] = Field(default_factory=list)
    break_even_price_eur: float = 0.0
    min_viable_sale_price_eur: float = 0.0

    def summary(self) -> str:
        verdict = "KAUFEN" if self.is_recommended else "ABLEHNEN"
        return (
            f"{verdict}: Gewinn {self.net_profit_eur:.2f} EUR | "
            f"ROI {self.roi_percent:.1f} % | "
            f"Umsatz {self.gross_revenue_eur:.2f} EUR | "
            f"Kosten {self.total_cost_eur:.2f} EUR"
        )


class MarginCalculator:
    """Rechnet Kalkulationen gegen einen festen Satz an Kosten- und Steuerregeln."""

    def __init__(
        self,
        costs: CostConfig,
        margin: MarginConfig,
        tax: TaxConfig | None = None,
    ) -> None:
        self.costs = costs
        self.margin = margin
        self.tax = tax or TaxConfig()

    @classmethod
    def from_settings(cls, settings: Settings) -> MarginCalculator:
        cfg = settings.config
        return cls(costs=cfg.costs, margin=cfg.margin, tax=cfg.tax)

    # -- interne Hilfsgroessen ------------------------------------------------
    @property
    def _vat_share(self) -> Decimal:
        """Anteil der USt am Bruttopreis, z. B. 19 % -> 0,1596."""
        if not self.tax.vat_registered:
            return ZERO
        rate = _pct(self.tax.vat_rate_percent)
        return rate / (Decimal("1") + rate)

    @property
    def _variable_rate(self) -> Decimal:
        """Summe aller prozentualen Abzuege vom Bruttoumsatz."""
        return (
            self._vat_share
            + _pct(self.costs.ebay_fee_percent)
            + _pct(self.costs.payment_fee_percent)
            + _pct(self.costs.return_buffer_percent)
        )

    # -- Hauptrechnung --------------------------------------------------------
    def calculate(
        self,
        *,
        purchase_price_eur: float | Decimal,
        sale_price_eur: float | Decimal,
        purchase_shipping_eur: float | Decimal = 0.0,
        sale_shipping_income_eur: float | Decimal = 0.0,
        quantity_source: int = 1,
        quantity_target: int = 1,
    ) -> MarginBreakdown:
        """Eine Kalkulation durchrechnen.

        ``quantity_source`` / ``quantity_target`` normieren unterschiedliche
        Gebindegroessen: Wer ein 5er-Pack kauft und Einzelstuecke verkauft, traegt
        pro Verkauf nur ein Fuenftel des Einkaufspreises.

        ``sale_shipping_income_eur`` ist der vom Kaeufer gezahlte Versand. eBay
        berechnet die Provision auf Artikelpreis + Versand, deshalb zaehlt er zum
        Bruttoumsatz.
        """
        if quantity_source < 1 or quantity_target < 1:
            raise CalculationError(
                f"Stueckzahlen muessen >= 1 sein (Quelle={quantity_source}, Ziel={quantity_target})."
            )

        purchase = _dec(purchase_price_eur)
        purchase_ship = _dec(purchase_shipping_eur)
        sale = _dec(sale_price_eur)
        sale_ship = _dec(sale_shipping_income_eur)

        if purchase < ZERO or sale < ZERO or purchase_ship < ZERO or sale_ship < ZERO:
            raise CalculationError("Negative Betraege sind nicht zulaessig.")
        if sale == ZERO:
            raise CalculationError("Verkaufspreis 0 EUR ergibt keine Kalkulation.")

        # --- Einnahmen ---
        gross_revenue = sale + sale_ship

        # --- Verkaufsseitige Abzuege ---
        vat_on_sale = gross_revenue * self._vat_share
        ebay_variable = gross_revenue * _pct(self.costs.ebay_fee_percent)
        ebay_fixed = _dec(self.costs.ebay_fixed_fee_eur)
        payment_fee = gross_revenue * _pct(self.costs.payment_fee_percent)
        return_buffer = gross_revenue * _pct(self.costs.return_buffer_percent)

        # --- Beschaffungsseite, auf einen Verkauf normiert ---
        units_per_sale = _dec(quantity_target) / _dec(quantity_source)
        purchase_landed = purchase + purchase_ship
        import_duty = purchase_landed * _pct(self.costs.import_duty_percent)
        inbound = _dec(self.costs.inbound_shipping_eur)

        procurement_full = purchase_landed + import_duty + inbound
        input_vat_credit = ZERO
        if self.tax.vat_registered and self.tax.input_vat_deductible:
            rate = _pct(self.tax.vat_rate_percent)
            input_vat_credit = procurement_full * (rate / (Decimal("1") + rate))

        cost_of_goods = (procurement_full - input_vat_credit) * units_per_sale

        # --- Logistik Verkaufsseite ---
        outbound = _dec(self.costs.shipping_cost_domestic_eur)
        packaging = _dec(self.costs.packaging_cost_eur)

        total_cost = (
            vat_on_sale
            + ebay_variable
            + ebay_fixed
            + payment_fee
            + return_buffer
            + cost_of_goods
            + outbound
            + packaging
        )
        net_profit = gross_revenue - total_cost

        # Eingesetztes Kapital: was vor dem Verkauf tatsaechlich gebunden ist.
        capital = cost_of_goods + outbound + packaging
        roi = (net_profit / capital * Decimal("100")) if capital > ZERO else Decimal("0")
        margin_pct = net_profit / gross_revenue * Decimal("100")

        fixed_block = ebay_fixed + cost_of_goods + outbound + packaging
        break_even = self._required_price(fixed_block, ZERO)
        required_for_profit = self._required_price(fixed_block, _dec(self.margin.min_profit_eur))
        required_for_roi = self._required_price(
            fixed_block, capital * _pct(self.margin.min_roi_percent)
        )
        min_viable = max(required_for_profit, required_for_roi)

        breakdown = MarginBreakdown(
            sale_price_eur=float(_money(sale)),
            sale_shipping_income_eur=float(_money(sale_ship)),
            gross_revenue_eur=float(_money(gross_revenue)),
            vat_on_sale_eur=float(_money(vat_on_sale)),
            ebay_fee_variable_eur=float(_money(ebay_variable)),
            ebay_fee_fixed_eur=float(_money(ebay_fixed)),
            payment_fee_eur=float(_money(payment_fee)),
            return_buffer_eur=float(_money(return_buffer)),
            purchase_price_eur=float(_money(purchase)),
            purchase_shipping_eur=float(_money(purchase_ship)),
            import_duty_eur=float(_money(import_duty)),
            inbound_shipping_eur=float(_money(inbound)),
            input_vat_credit_eur=float(_money(input_vat_credit)),
            cost_of_goods_eur=float(_money(cost_of_goods)),
            outbound_shipping_eur=float(_money(outbound)),
            packaging_eur=float(_money(packaging)),
            total_cost_eur=float(_money(total_cost)),
            net_profit_eur=float(_money(net_profit)),
            capital_employed_eur=float(_money(capital)),
            roi_percent=float(roi.quantize(CENT, rounding=ROUND_HALF_UP)),
            margin_percent=float(margin_pct.quantize(CENT, rounding=ROUND_HALF_UP)),
            quantity_source=quantity_source,
            quantity_target=quantity_target,
            units_purchased_per_sale=float(units_per_sale.quantize(Decimal("0.0001"))),
            break_even_price_eur=float(_money(break_even)),
            min_viable_sale_price_eur=float(_money(min_viable)),
        )

        self._apply_thresholds(breakdown)
        return breakdown

    def _required_price(self, fixed_block: Decimal, target_profit: Decimal) -> Decimal:
        """Bruttopreis, der nach allen Abzuegen ``target_profit`` uebrig laesst."""
        k = Decimal("1") - self._variable_rate
        if k <= ZERO:  # durch die Konfigvalidierung eigentlich ausgeschlossen
            return Decimal("0")
        return (fixed_block + target_profit) / k

    def _apply_thresholds(self, b: MarginBreakdown) -> None:
        """Schwellenwerte pruefen und Ablehnungsgruende im Klartext hinterlegen."""
        reasons: list[str] = []
        if b.net_profit_eur < self.margin.min_profit_eur:
            reasons.append(
                f"Reingewinn {b.net_profit_eur:.2f} EUR < Mindestgewinn "
                f"{self.margin.min_profit_eur:.2f} EUR"
            )
        if b.roi_percent < self.margin.min_roi_percent:
            reasons.append(
                f"ROI {b.roi_percent:.1f} % < Mindest-ROI {self.margin.min_roi_percent:.1f} %"
            )
        b.rejection_reasons = reasons
        b.is_recommended = not reasons

    # -- Komfort --------------------------------------------------------------
    def evaluate_pair(
        self,
        pair: CandidatePair,
        *,
        quantity_source: int | None = None,
        quantity_target: int | None = None,
    ) -> MarginBreakdown:
        """Kalkulation direkt aus einem Angebotspaar.

        Die Stueckzahlen stammen bevorzugt aus dem Gemini-Match; fehlen sie, wird
        auf die Angaben des Angebots und zuletzt auf 1 zurueckgefallen.
        """
        qty_source = quantity_source or pair.source.package_quantity or 1
        qty_target = quantity_target or pair.target.package_quantity or 1

        return self.calculate(
            purchase_price_eur=pair.source.price_eur,
            purchase_shipping_eur=pair.source.shipping_eur,
            sale_price_eur=pair.target.price_eur,
            sale_shipping_income_eur=pair.target.shipping_eur,
            quantity_source=qty_source,
            quantity_target=qty_target,
        )


def format_breakdown(b: MarginBreakdown) -> str:
    """Mehrzeilige Aufschluesselung fuer Konsole und Log."""
    lines = [
        f"  Umsatz brutto           {b.gross_revenue_eur:>9.2f} EUR",
        f"  - USt                   {b.vat_on_sale_eur:>9.2f} EUR",
        f"  - eBay Provision        {b.ebay_fee_variable_eur:>9.2f} EUR",
        f"  - eBay Fixgebuehr       {b.ebay_fee_fixed_eur:>9.2f} EUR",
        f"  - Zahlungsgebuehr       {b.payment_fee_eur:>9.2f} EUR",
        f"  - Retourenpuffer        {b.return_buffer_eur:>9.2f} EUR",
        f"  - Wareneinsatz          {b.cost_of_goods_eur:>9.2f} EUR"
        f"  ({b.units_purchased_per_sale:g} x Einkauf)",
        f"  - Inlandsversand        {b.outbound_shipping_eur:>9.2f} EUR",
        f"  - Verpackung            {b.packaging_eur:>9.2f} EUR",
        f"  {'=' * 40}",
        f"  Reingewinn              {b.net_profit_eur:>9.2f} EUR",
        f"  ROI                     {b.roi_percent:>9.2f} %",
        f"  Break-even-Preis        {b.break_even_price_eur:>9.2f} EUR",
        f"  Mindestpreis (Ziel)     {b.min_viable_sale_price_eur:>9.2f} EUR",
    ]
    if b.rejection_reasons:
        lines.append("  Abgelehnt: " + "; ".join(b.rejection_reasons))
    return "\n".join(lines)


def _self_test() -> dict[str, Any]:
    """`python calculator.py` -- Rechenweg an einem Beispiel nachvollziehen."""
    from config import load_settings

    settings = load_settings()
    calc = MarginCalculator.from_settings(settings)

    result = calc.calculate(
        purchase_price_eur=18.90,
        purchase_shipping_eur=0.0,
        sale_price_eur=49.95,
        sale_shipping_income_eur=0.0,
    )
    print("Beispiel: Einkauf 18,90 EUR -> Verkauf 49,95 EUR")
    print(format_breakdown(result))
    print()
    print(result.summary())

    bundle = calc.calculate(
        purchase_price_eur=39.00,   # 5er-Pack
        sale_price_eur=14.99,       # Einzelverkauf
        quantity_source=5,
        quantity_target=1,
    )
    print("\nBeispiel Gebinde: 5er-Pack fuer 39,00 EUR -> Einzelverkauf 14,99 EUR")
    print(format_breakdown(bundle))
    return {"single": result.model_dump(), "bundle": bundle.model_dump()}


if __name__ == "__main__":
    _self_test()
