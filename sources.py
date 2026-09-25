"""Datenquellen fuer Angebotspaare.

Der Scout trennt *woher die Angebote kommen* strikt von *wie sie bewertet
werden*. Damit laesst sich die gesamte Pipeline offline testen, und ein echter
Marktplatz-Connector wird spaeter angesteckt, ohne ``scout.py`` anzufassen.

Aktuell implementiert:

* ``CsvOfferSource``  -- liest fertige Paare aus einer CSV (Standard).
* ``MockOfferSource`` -- eingebaute Beispieldaten fuer den ersten Testlauf.

Ein echter Connector (eBay Browse API, Amazon PA-API, Keepa, ...) implementiert
lediglich :meth:`OfferSource.fetch_pairs`. Bewusst NICHT enthalten ist Scraping
gegen die Marktplaetze -- das verstoesst gegen deren Nutzungsbedingungen und
gehoert ueber die offiziellen APIs geloest.
"""

from __future__ import annotations

import csv
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Iterator

from config import CategoryConfig, Settings
from logging_utils import get_logger
from models import CandidatePair, Condition, Marketplace, Offer

logger = get_logger(__name__)


class SourceError(RuntimeError):
    """Angebote konnten nicht geladen werden."""


class OfferSource(ABC):
    """Basisklasse aller Angebotsquellen."""

    name = "abstract"

    @abstractmethod
    def fetch_pairs(self, category: CategoryConfig | None = None) -> Iterator[CandidatePair]:
        """Angebotspaare liefern, optional auf eine Kategorie eingeschraenkt."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
_REQUIRED_COLUMNS = {
    "source_title",
    "source_price_eur",
    "target_title",
    "target_price_eur",
}


def _to_float(value: Any, default: float = 0.0) -> float:
    """Robuste Zahlenwandlung: akzeptiert "12,50", "12.50 EUR", leere Zellen."""
    if value is None:
        return default
    text = str(value).strip().replace("EUR", "").replace("€", "").strip()
    if not text:
        return default
    text = text.replace(" ", "")
    if "," in text and "." in text:  # 1.234,56 -> 1234.56
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return default


def _to_int(value: Any, default: int | None = None) -> int | None:
    if value is None or str(value).strip() == "":
        return default
    try:
        return max(1, int(float(str(value).strip())))
    except ValueError:
        return default


def _to_enum(value: Any, enum_cls: Any, default: Any) -> Any:
    if value is None or not str(value).strip():
        return default
    try:
        return enum_cls(str(value).strip().lower())
    except ValueError:
        return default


class CsvOfferSource(OfferSource):
    """Liest Angebotspaare aus einer CSV-Datei.

    Erwartete Spalten (``source_``/``target_``-Praefix), Pflicht sind nur Titel
    und Preis::

        category, keyword,
        source_marketplace, source_id, source_title, source_price_eur,
        source_shipping_eur, source_brand, source_model, source_ean,
        source_qty, source_condition, source_url,
        target_marketplace, target_id, ... (analog)
    """

    name = "csv"

    def __init__(self, path: Path) -> None:
        self.path = path

    def fetch_pairs(self, category: CategoryConfig | None = None) -> Iterator[CandidatePair]:
        if not self.path.is_file():
            raise SourceError(
                f"CSV-Datei nicht gefunden: {self.path}\n"
                "Lege sie an oder stelle in config.yaml auf sources.provider: mock um."
            )

        try:
            handle = self.path.open("r", encoding="utf-8-sig", newline="")
        except OSError as exc:
            raise SourceError(f"CSV nicht lesbar ({self.path}): {exc}") from exc

        with handle:
            reader = csv.DictReader(handle)
            columns = set(reader.fieldnames or [])
            missing = _REQUIRED_COLUMNS - columns
            if missing:
                raise SourceError(
                    f"{self.path}: Pflichtspalten fehlen: {', '.join(sorted(missing))}"
                )

            for line_no, row in enumerate(reader, start=2):
                row_category = (row.get("category") or "uncategorized").strip().lower()
                if category is not None and row_category != category.name:
                    continue
                try:
                    pair = self._build_pair(row, row_category, line_no)
                except Exception as exc:
                    # Eine kaputte Zeile darf den Lauf nicht beenden.
                    logger.warning("%s Zeile %d uebersprungen: %s", self.path.name, line_no, exc)
                    continue

                if (
                    category is not None
                    and category.max_purchase_price_eur is not None
                    and pair.source.landed_price_eur > category.max_purchase_price_eur
                ):
                    logger.debug(
                        "Zeile %d: Einkaufspreis %.2f EUR ueber Kategoriegrenze %.2f EUR",
                        line_no,
                        pair.source.landed_price_eur,
                        category.max_purchase_price_eur,
                    )
                    continue

                yield pair

    def _build_pair(self, row: dict[str, str], category: str, line_no: int) -> CandidatePair:
        return CandidatePair(
            source=self._build_offer(row, "source", line_no, Marketplace.AMAZON),
            target=self._build_offer(row, "target", line_no, Marketplace.EBAY),
            category=category,
            keyword=(row.get("keyword") or "").strip() or None,
        )

    @staticmethod
    def _build_offer(
        row: dict[str, str], prefix: str, line_no: int, default_marketplace: Marketplace
    ) -> Offer:
        def col(name: str) -> str:
            return (row.get(f"{prefix}_{name}") or "").strip()

        url = col("url") or None
        return Offer(
            offer_id=col("id") or f"{prefix}-{line_no}",
            marketplace=_to_enum(col("marketplace"), Marketplace, default_marketplace),
            title=col("title"),
            price_eur=_to_float(col("price_eur")),
            shipping_eur=_to_float(col("shipping_eur")),
            url=url,  # type: ignore[arg-type]  # Pydantic validiert die URL
            brand=col("brand") or None,
            model_number=col("model") or None,
            ean=col("ean") or None,
            asin=col("asin") or None,
            package_quantity=_to_int(col("qty")),
            condition=_to_enum(col("condition"), Condition, Condition.UNKNOWN),
            description=col("description") or None,
        )


# ---------------------------------------------------------------------------
# Mock
# ---------------------------------------------------------------------------
_MOCK_ROWS: list[dict[str, Any]] = [
    {
        # Kernfall: unterschiedliche Gebinde. Erst die Normierung auf die
        # verkaufte Stueckzahl macht daraus eine Empfehlung.
        "category": "haushalt",
        "keyword": "Wasserfilter Kartusche",
        "source": {
            "offer_id": "amz-brita-12",
            "marketplace": Marketplace.AMAZON,
            "title": "BRITA Maxtra+ Filterkartuschen, 12er Pack Vorteilspack",
            "price_eur": 39.99,
            "brand": "BRITA",
            "ean": "4006387079321",
            "condition": Condition.NEW,
        },
        "target": {
            "offer_id": "ebay-brita-3",
            "marketplace": Marketplace.EBAY,
            "title": "BRITA Maxtra Plus Filterkartuschen 3er Pack Ersatzfilter NEU",
            "price_eur": 34.90,
            "brand": "BRITA",
            "ean": "4006387079321",
            "condition": Condition.NEW,
        },
    },
    {
        # Glatter Einzelfall ohne Gebindethema.
        "category": "elektronik-zubehoer",
        "keyword": "USB-C Hub 8 in 1",
        "source": {
            "offer_id": "ali-hub-8in1",
            "marketplace": Marketplace.ALIEXPRESS,
            "title": "USB C Hub 8 in 1 Adapter HDMI 4K 60Hz 100W PD SD Kartenleser grau",
            "price_eur": 16.40,
            "shipping_eur": 2.10,
            "condition": Condition.NEW,
        },
        "target": {
            "offer_id": "ebay-hub-8in1",
            "marketplace": Marketplace.EBAY,
            "title": "USB C Hub 8 in 1 Adapter HDMI 4K 60Hz 100W PD SD Kartenleser grau",
            "price_eur": 44.90,
            "condition": Condition.NEW,
        },
    },
    {
        # Falle 1: Speichervariante weicht ab -- muss am Matcher scheitern,
        # obwohl die Marge verlockend aussieht.
        "category": "elektronik-zubehoer",
        "keyword": "Powerbank 20000mAh",
        "source": {
            "offer_id": "ali-pb-10000",
            "marketplace": Marketplace.ALIEXPRESS,
            "title": "Powerbank 10000mAh 22.5W Schnellladung USB-C schwarz",
            "price_eur": 12.80,
            "condition": Condition.NEW,
        },
        "target": {
            "offer_id": "ebay-pb-20000",
            "marketplace": Marketplace.EBAY,
            "title": "Powerbank 20000mAh 22.5W Schnellladen USB-C Externer Akku schwarz",
            "price_eur": 39.99,
            "condition": Condition.NEW,
        },
    },
    {
        # Falle 2: Produkte passen, aber die Marge traegt die Fixkosten nicht.
        "category": "kfz",
        "keyword": "OBD2 Diagnosegeraet",
        "source": {
            "offer_id": "amz-obd2",
            "marketplace": Marketplace.AMAZON,
            "title": "Vgate OBD2 Diagnosegeraet Bluetooth 5.0 ELM327 fuer Android und iOS",
            "price_eur": 27.99,
            "brand": "Vgate",
            "condition": Condition.NEW,
        },
        "target": {
            "offer_id": "ebay-obd2",
            "marketplace": Marketplace.EBAY,
            "title": "Vgate OBD2 Diagnosegeraet Bluetooth 5.0 ELM327 Android iOS",
            "price_eur": 42.50,
            "brand": "Vgate",
            "condition": Condition.NEW,
        },
    },
]


class MockOfferSource(OfferSource):
    """Fest eingebaute Beispieldaten -- fuer den ersten Lauf ohne jede Datei."""

    name = "mock"

    def fetch_pairs(self, category: CategoryConfig | None = None) -> Iterator[CandidatePair]:
        for row in _MOCK_ROWS:
            if category is not None and row["category"] != category.name:
                continue
            yield CandidatePair(
                source=Offer(**row["source"]),
                target=Offer(**row["target"]),
                category=row["category"],
                keyword=row.get("keyword"),
            )


def write_example_csv(path: Path) -> Path:
    """Beispiel-CSV aus den Mock-Daten schreiben -- Vorlage fuer eigene Daten."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "category", "keyword",
        "source_marketplace", "source_id", "source_title", "source_price_eur",
        "source_shipping_eur", "source_brand", "source_model", "source_ean",
        "source_qty", "source_condition", "source_url",
        "target_marketplace", "target_id", "target_title", "target_price_eur",
        "target_shipping_eur", "target_brand", "target_model", "target_ean",
        "target_qty", "target_condition", "target_url",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for pair in MockOfferSource().fetch_pairs():
            record = {"category": pair.category, "keyword": pair.keyword or ""}
            for prefix, offer in (("source", pair.source), ("target", pair.target)):
                record.update(
                    {
                        f"{prefix}_marketplace": offer.marketplace.value,
                        f"{prefix}_id": offer.offer_id,
                        f"{prefix}_title": offer.title,
                        f"{prefix}_price_eur": f"{offer.price_eur:.2f}",
                        f"{prefix}_shipping_eur": f"{offer.shipping_eur:.2f}",
                        f"{prefix}_brand": offer.brand or "",
                        f"{prefix}_model": offer.model_number or "",
                        f"{prefix}_ean": offer.ean or "",
                        f"{prefix}_qty": offer.package_quantity or "",
                        f"{prefix}_condition": offer.condition.value,
                        f"{prefix}_url": str(offer.url) if offer.url else "",
                    }
                )
            writer.writerow(record)
    return path


def build_source(settings: Settings) -> OfferSource:
    """Quelle gemaess ``sources.provider`` erzeugen."""
    provider = settings.config.sources.provider
    if provider == "mock":
        logger.info("Datenquelle: eingebaute Mock-Daten")
        return MockOfferSource()
    if provider == "csv":
        path = settings.resolve(settings.config.sources.csv.offers_path)
        logger.info("Datenquelle: CSV %s", path)
        return CsvOfferSource(path)
    raise SourceError(f"Unbekannter Provider: {provider}")


if __name__ == "__main__":  # `python sources.py` schreibt die Beispieldatei
    from config import load_settings

    s = load_settings()
    out = write_example_csv(s.resolve("data/offers.example.csv"))
    print(f"Beispieldaten geschrieben: {out}")
