"""Datenquellen fuer Angebotspaare.

Der Scout trennt *woher die Angebote kommen* strikt von *wie sie bewertet
werden*. Damit laesst sich die gesamte Pipeline offline testen, und ein echter
Marktplatz-Connector wird spaeter angesteckt, ohne ``scout.py`` anzufassen.

Aktuell implementiert:

* ``CsvOfferSource``  -- liest fertige Paare aus einer CSV (Standard).
* ``MockOfferSource`` -- eingebaute Beispieldaten fuer den ersten Testlauf.
* ``ApiOfferSource``  -- sucht automatisch: Einkaufsangebote aus der Amazon
  Creators API oder einer Einkaufsliste (``purchases.csv``), passende
  Verkaufsangebote ueber die eBay Browse API.

Bewusst NICHT enthalten ist Scraping gegen die Marktplaetze -- das verstoesst
gegen deren Nutzungsbedingungen und gehoert ueber die offiziellen APIs geloest.
"""

from __future__ import annotations

import csv
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Iterator

from amazon_client import AmazonClient, AmazonError
from config import CategoryConfig, ConfigError, Settings
from ebay_client import EbayClient, EbayError, build_query
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


# ---------------------------------------------------------------------------
# API: Einkaufsseite (CSV-Liste oder Amazon) + Verkaufsseite (eBay)
# ---------------------------------------------------------------------------
PURCHASE_FIELDS = [
    "category", "keyword", "marketplace", "id", "title", "price_eur", "shipping_eur",
    "brand", "model", "ean", "asin", "qty", "condition", "url", "description",
]
_PURCHASE_REQUIRED = {"title", "price_eur"}


class PurchaseProvider(ABC):
    """Liefert Einkaufsangebote je Kategorie, jeweils mit dem Suchbegriff."""

    name = "abstract"

    @abstractmethod
    def purchases(self, category: CategoryConfig) -> Iterator[tuple[Offer, str | None]]:
        raise NotImplementedError


class CsvPurchaseProvider(PurchaseProvider):
    """Selbst gepflegte Einkaufsliste: ein Angebot pro Zeile, mit Link.

    Gedacht fuer alles, wofuer es keine offene API gibt (AliExpress, Amazon ohne
    Creators-API-Freischaltung, Haendler-Shops): Angebot finden, Zeile anlegen,
    den Rest -- passende eBay-Angebote, Match, Kalkulation -- macht der Scout.
    """

    name = "csv"

    def __init__(self, path: Path) -> None:
        self.path = path
        self._rows: list[tuple[str, Offer, str | None]] | None = None

    def _load(self) -> list[tuple[str, Offer, str | None]]:
        if self._rows is not None:
            return self._rows
        if not self.path.is_file():
            raise SourceError(
                f"Einkaufsliste nicht gefunden: {self.path}\n"
                "Vorlage: python scout.py --write-example-data (schreibt "
                "data/purchases.example.csv)."
            )
        try:
            handle = self.path.open("r", encoding="utf-8-sig", newline="")
        except OSError as exc:
            raise SourceError(f"Einkaufsliste nicht lesbar ({self.path}): {exc}") from exc

        rows: list[tuple[str, Offer, str | None]] = []
        with handle:
            reader = csv.DictReader(handle)
            missing = _PURCHASE_REQUIRED - set(reader.fieldnames or [])
            if missing:
                raise SourceError(f"{self.path}: Pflichtspalten fehlen: {', '.join(sorted(missing))}")
            for line_no, row in enumerate(reader, start=2):
                def col(name: str) -> str:
                    return (row.get(name) or "").strip()

                try:
                    offer = Offer(
                        offer_id=col("id") or f"purchase-{line_no}",
                        marketplace=_to_enum(col("marketplace"), Marketplace, Marketplace.OTHER),
                        title=col("title"),
                        price_eur=_to_float(col("price_eur")),
                        shipping_eur=_to_float(col("shipping_eur")),
                        url=col("url") or None,  # type: ignore[arg-type]
                        brand=col("brand") or None,
                        model_number=col("model") or None,
                        ean=col("ean") or None,
                        asin=col("asin") or None,
                        package_quantity=_to_int(col("qty")),
                        condition=_to_enum(col("condition"), Condition, Condition.NEW),
                        description=col("description") or None,
                    )
                except Exception as exc:
                    logger.warning("%s Zeile %d uebersprungen: %s", self.path.name, line_no, exc)
                    continue
                if offer.price_eur <= 0:
                    logger.warning("%s Zeile %d uebersprungen: Preis fehlt.", self.path.name, line_no)
                    continue
                category = (col("category") or "uncategorized").lower()
                rows.append((category, offer, col("keyword") or None))
        self._rows = rows
        return rows

    def warn_unknown_categories(self, known: set[str]) -> None:
        """Zeilen, deren Kategorie nicht in config.yaml steht, werden nie
        geprueft -- lieber einmal laut sagen als still ignorieren."""
        unknown: dict[str, int] = {}
        for category, _offer, _kw in self._load():
            if category not in known:
                unknown[category] = unknown.get(category, 0) + 1
        for category, count in sorted(unknown.items()):
            logger.warning(
                "Einkaufsliste: %d Zeile(n) mit Kategorie '%s' -- nicht in "
                "search.categories, werden uebersprungen.",
                count,
                category,
            )

    def purchases(self, category: CategoryConfig) -> Iterator[tuple[Offer, str | None]]:
        for row_category, offer, keyword in self._load():
            if row_category == category.name:
                yield offer, keyword


class AmazonPurchaseProvider(PurchaseProvider):
    """Stichwortsuche ueber die Amazon Creators API, je Suchbegriff der Kategorie."""

    name = "amazon"

    def __init__(self, client: AmazonClient) -> None:
        self.client = client

    def purchases(self, category: CategoryConfig) -> Iterator[tuple[Offer, str | None]]:
        for keyword in category.keywords:
            try:
                offers = self.client.search(keyword)
            except AmazonError as exc:
                if exc.fatal:
                    raise SourceError(str(exc)) from exc
                logger.warning("Amazon-Suche '%s' uebersprungen: %s", keyword, exc)
                continue
            logger.info("Amazon '%s': %d Angebot(e)", keyword, len(offers))
            for offer in offers:
                yield offer, keyword


class ApiOfferSource(OfferSource):
    """Einkaufsangebote holen, zu jedem passende eBay-Angebote suchen.

    Der Generator ist lazy: ``search.max_candidates_per_run`` in scout.py
    bricht nicht nur die Pruefung, sondern auch die API-Aufrufe ab.
    """

    name = "api"

    def __init__(
        self,
        purchases: PurchaseProvider,
        ebay: EbayClient,
        categories: list[CategoryConfig],
    ) -> None:
        self.purchases = purchases
        self.ebay = ebay
        self.categories = categories

    def fetch_pairs(self, category: CategoryConfig | None = None) -> Iterator[CandidatePair]:
        cats = [category] if category else self.categories
        if not cats:
            raise SourceError("Keine Kategorien konfiguriert (search.categories in config.yaml).")

        if isinstance(self.purchases, CsvPurchaseProvider) and category is None:
            self.purchases.warn_unknown_categories({c.name for c in cats})

        seen: set[str] = set()
        for cat in cats:
            for offer, keyword in self.purchases.purchases(cat):
                # Dasselbe Produkt kann unter mehreren Suchbegriffen auftauchen.
                if offer.offer_id in seen:
                    continue
                seen.add(offer.offer_id)

                if cat.max_purchase_price_eur is not None and offer.landed_price_eur > cat.max_purchase_price_eur:
                    logger.debug(
                        "%s ueber Kategoriegrenze (%.2f > %.2f EUR)",
                        offer.offer_id, offer.landed_price_eur, cat.max_purchase_price_eur,
                    )
                    continue

                try:
                    targets = self._find_targets(offer)
                except EbayError as exc:
                    if exc.fatal:
                        raise SourceError(str(exc)) from exc
                    logger.warning("eBay-Suche fuer %s uebersprungen: %s", offer.offer_id, exc)
                    continue

                if not targets:
                    logger.info("Keine eBay-Angebote zu: %s", offer.short())
                for target in targets:
                    yield CandidatePair(source=offer, target=target, category=cat.name, keyword=keyword)

    def _find_targets(self, offer: Offer) -> list[Offer]:
        """EAN zuerst -- trifft exakt das Produkt, nicht nur aehnliche Titel.
        Ohne EAN oder ohne Treffer: Suche per Titel, der Matcher sortiert aus."""
        if offer.ean:
            hits = self.ebay.search(gtin=offer.ean)
            if hits:
                return hits
            logger.debug("EAN %s ohne eBay-Treffer, Suche per Titel.", offer.ean)
        return self.ebay.search(query=build_query(offer))


def write_purchase_example(path: Path) -> Path:
    """Vorlage fuer die Einkaufsliste (``sources.api.purchase_source: csv``).

    Werte sind Platzhalter zur Veranschaulichung des Formats -- vor dem
    Einsatz durch echte, selbst recherchierte Angebote ersetzen.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "category": "haushalt", "keyword": "Wasserfilter Kartusche",
            "marketplace": "amazon", "id": "", "title": "BRITA Maxtra Pro All-in-1 Filterkartusche 6er Pack",
            "price_eur": "34.99", "shipping_eur": "0", "brand": "BRITA", "model": "", "ean": "",
            "asin": "", "qty": "6", "condition": "new",
            "url": "https://www.amazon.de/dp/ASIN-HIER-EINTRAGEN", "description": "",
        },
        {
            "category": "elektronik-zubehoer", "keyword": "USB-C Hub",
            "marketplace": "aliexpress", "id": "", "title": "Ugreen USB C Hub 7 in 1 4K HDMI 100W PD",
            "price_eur": "19.80", "shipping_eur": "2.50", "brand": "Ugreen", "model": "", "ean": "",
            "asin": "", "qty": "1", "condition": "new",
            "url": "https://de.aliexpress.com/item/ID-HIER-EINTRAGEN.html", "description": "",
        },
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PURCHASE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
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
    if provider == "api":
        return _build_api_source(settings)
    raise SourceError(f"Unbekannter Provider: {provider}")


def _build_api_source(settings: Settings) -> ApiOfferSource:
    api_cfg = settings.config.sources.api
    http_cfg = settings.config.sources.http
    try:
        client_id, client_secret = settings.secrets.require_ebay()
    except ConfigError as exc:
        raise SourceError(str(exc)) from exc
    ebay = EbayClient(client_id, client_secret, api_cfg, http_cfg)

    purchases: PurchaseProvider
    if api_cfg.purchase_source == "amazon":
        try:
            cred_id, cred_secret, cred_version, partner_tag = settings.secrets.require_amazon()
            client = AmazonClient(
                cred_id, cred_secret, cred_version, partner_tag, api_cfg,
                timeout_seconds=http_cfg.timeout_seconds,
            )
        except (ConfigError, AmazonError) as exc:
            raise SourceError(str(exc)) from exc
        purchases = AmazonPurchaseProvider(client)
        logger.info("Datenquelle: Amazon Creators API (%s) -> eBay %s",
                    api_cfg.amazon_country, api_cfg.ebay_marketplace)
    else:
        path = settings.resolve(api_cfg.purchase_csv_path)
        purchases = CsvPurchaseProvider(path)
        logger.info("Datenquelle: Einkaufsliste %s -> eBay %s", path, api_cfg.ebay_marketplace)

    return ApiOfferSource(purchases, ebay, settings.config.search.categories)


if __name__ == "__main__":  # `python sources.py` schreibt die Beispieldatei
    from config import load_settings

    s = load_settings()
    out = write_example_csv(s.resolve("data/offers.example.csv"))
    print(f"Beispieldaten geschrieben: {out}")
