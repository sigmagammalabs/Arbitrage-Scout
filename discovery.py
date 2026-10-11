"""Produktfindung von der eBay-Seite: was laeuft dort gut, und was darf der
Einkauf hoechstens kosten?

Der normale Scout-Lauf geht vom Einkauf aus ("ich habe ein Angebot, lohnt es
sich?"). Dieses Modul dreht die Richtung um: Es sucht auf eBay nach Artikeln,
die sich nachweislich verkaufen, und rechnet fuer jeden aus, bis zu welchem
Einkaufspreis er die Schwellen aus ``config.yaml`` noch erreicht.

Was "gut laufend" hier bedeutet -- und was nicht:

* Grundlage ist ``estimatedSoldQuantity`` aus der Browse API: die geschaetzte
  Stueckzahl, die ueber *dieses eine Angebot* bisher verkauft wurde, geteilt
  durch sein Alter. Das ist ein Tempo-Schaetzwert, kein Marktvolumen.
* Tatsaechliche Verkaufspreise liefert eBay nur ueber die Marketplace Insights
  API, die fuer neue Nutzer gesperrt ist. Der Angebotspreis eines gut laufenden
  Angebots ist aber ein deutlich besseres Signal als der eines beliebigen.
* Optional kommen eBays eigene Bestseller-Listen je Kategorie dazu (Marketing
  API, Beta). Die brauchen die Zusatzberechtigung ``buy.marketing``.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import quote_plus

from pydantic import BaseModel, Field

from calculator import MarginCalculator
from config import CategoryConfig, DiscoveryConfig
from ebay_client import EbayClient, EbayError, EbayScopeError, _map_condition, build_query
from logging_utils import get_logger
from models import CandidatePair, Marketplace, Offer

logger = get_logger(__name__)


class DiscoveryError(RuntimeError):
    """Die Produktfindung kann nicht (weiter) laufen, z. B. Zugang verweigert."""


class HotItem(BaseModel):
    """Ein eBay-Angebot, das sich nachweislich verkauft."""

    item_id: str
    title: str
    price_eur: float
    shipping_eur: float = 0.0
    url: str | None = None
    seller: str | None = None

    sold_quantity: int = 0
    available_quantity: int | None = None
    listed_at: datetime | None = None
    days_listed: float | None = None
    sold_per_day: float | None = None

    gtin: str | None = None
    brand: str | None = None
    mpn: str | None = None
    epid: str | None = None
    condition_id: str | None = None
    ebay_category_id: str | None = None
    ebay_category_path: str | None = None

    category: str = "uncategorized"
    keyword: str | None = None
    origin: Literal["suche", "bestseller"] = "suche"

    # Mehrere Haendler mit demselben Produkt (gleiche EAN/ePID) werden zu einem
    # Eintrag zusammengefasst; diese Felder zeigen die Konkurrenzlage.
    competing_listings: int = 1
    lowest_competitor_price_eur: float | None = None

    max_purchase_price_eur: float = 0.0
    # Hoechstpreis, wenn man den guenstigsten Konkurrenten unterbieten bzw.
    # mit ihm gleichziehen muss. None, wenn es keinen guenstigeren gibt.
    max_purchase_at_lowest_price_eur: float | None = None
    research_links: dict[str, str] = Field(default_factory=dict)

    @property
    def total_price_eur(self) -> float:
        return round(self.price_eur + self.shipping_eur, 2)

    @property
    def group_key(self) -> str:
        return self.gtin or (f"epid:{self.epid}" if self.epid else f"item:{self.item_id}")

    def as_offer(self) -> Offer:
        """Als Verkaufsseite eines Kandidatenpaars."""
        return Offer(
            offer_id=f"ebay-{self.item_id}",
            marketplace=Marketplace.EBAY,
            title=self.title,
            price_eur=self.price_eur,
            shipping_eur=self.shipping_eur,
            url=self.url,  # type: ignore[arg-type]
            brand=self.brand,
            model_number=self.mpn,
            ean=self.gtin,
            condition=_map_condition(self.condition_id or ""),
            seller=self.seller,
        )


def research_links(title: str, gtin: str | None) -> dict[str, str]:
    """Fertige Suchlinks fuer die Einkaufsrecherche.

    Nur Links zum Anklicken -- der Scout ruft diese Seiten nicht selbst ab
    (kein Scraping). Mit EAN wird exakt gesucht, sonst per Titel.
    """
    exact = quote_plus(gtin or title)
    by_title = quote_plus(title[:80])
    return {
        "Amazon": f"https://www.amazon.de/s?k={exact}",
        "idealo": f"https://www.idealo.de/preisvergleich/MainSearchProductCategory.html?q={exact}",
        "Google Shopping": f"https://www.google.com/search?tbm=shop&q={exact}",
        "AliExpress": f"https://de.aliexpress.com/wholesale?SearchText={by_title}",
    }


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_hot_item(
    detail: dict[str, Any],
    *,
    category: str,
    keyword: str | None,
    origin: Literal["suche", "bestseller"] = "suche",
    now: datetime | None = None,
) -> HotItem | None:
    """Eine ``getItem``-Antwort in ein ``HotItem`` uebersetzen.

    ``None`` bei Angeboten ohne Euro-Preis oder Titel. Feldnamen laut eBays
    OpenAPI-Spezifikation der Browse API (v1.20.4).
    """
    price = detail.get("price") or {}
    price_eur = _to_float(price.get("value"))
    if price.get("currency") != "EUR" or price_eur is None:
        return None
    title = (detail.get("title") or "").strip()
    item_id = detail.get("itemId")
    if not title or not item_id:
        return None

    shipping = 0.0
    options = detail.get("shippingOptions") or []
    if options:
        cost = (options[0] or {}).get("shippingCost") or {}
        if cost.get("currency") in (None, "EUR"):
            shipping = _to_float(cost.get("value")) or 0.0

    # Meist ein Eintrag; bei mehreren (verschiedene Lieferoptionen) beschreiben
    # sie denselben Bestand -- deshalb das Maximum, nicht die Summe.
    sold, available = 0, None
    for entry in detail.get("estimatedAvailabilities") or []:
        sold = max(sold, int(entry.get("estimatedSoldQuantity") or 0))
        qty = entry.get("estimatedAvailableQuantity")
        if qty is not None:
            available = max(available or 0, int(qty))

    listed_at = _parse_datetime(detail.get("itemCreationDate"))
    days_listed = sold_per_day = None
    if listed_at is not None:
        current = now or datetime.now(timezone.utc)
        # Mindestens ein Tag: ein heute eingestelltes Angebot mit 5 Verkaeufen
        # soll nicht als "unendlich schnell" erscheinen.
        days_listed = max((current - listed_at).total_seconds() / 86400.0, 1.0)
        sold_per_day = round(sold / days_listed, 3)

    return HotItem(
        item_id=str(item_id),
        title=title,
        price_eur=price_eur,
        shipping_eur=shipping,
        url=detail.get("itemWebUrl"),
        seller=(detail.get("seller") or {}).get("username"),
        sold_quantity=sold,
        available_quantity=available,
        listed_at=listed_at,
        days_listed=round(days_listed, 1) if days_listed is not None else None,
        sold_per_day=sold_per_day,
        gtin=(str(detail["gtin"]).strip() or None) if detail.get("gtin") else None,
        brand=detail.get("brand"),
        mpn=detail.get("mpn"),
        epid=str(detail["epid"]) if detail.get("epid") else None,
        condition_id=str(detail["conditionId"]) if detail.get("conditionId") else None,
        ebay_category_id=str(detail["categoryId"]) if detail.get("categoryId") else None,
        ebay_category_path=detail.get("categoryPath"),
        category=category,
        keyword=keyword,
        origin=origin,
    )


class Discoverer:
    """Sucht gut laufende eBay-Angebote zu den Suchbegriffen der Kategorien."""

    def __init__(
        self,
        ebay: EbayClient,
        config: DiscoveryConfig,
        calculator: MarginCalculator,
        *,
        now: datetime | None = None,
    ) -> None:
        self.ebay = ebay
        self.config = config
        self.calculator = calculator
        self._now = now
        self._calls_at_start = ebay.stats.get("calls", 0)
        self._bestsellers_disabled = not config.use_bestseller_lists
        self._bestseller_categories_done: set[str] = set()
        self._seen_items: set[str] = set()
        self.stats = {
            "keywords": 0,
            "search_hits": 0,
            "detail_lookups": 0,
            "bestseller_products": 0,
            "dropped_low_sales": 0,
            "dropped_unprofitable": 0,
            "merged_duplicates": 0,
            "api_calls": 0,
            "budget_exhausted": 0,
        }

    # -- Budget ---------------------------------------------------------------
    def _budget_left(self) -> bool:
        used = self.ebay.stats.get("calls", 0) - self._calls_at_start
        self.stats["api_calls"] = used
        if used >= self.config.max_api_calls:
            if not self.stats["budget_exhausted"]:
                logger.warning(
                    "API-Budget von %d Aufrufen erreicht - Produktfindung stoppt hier.",
                    self.config.max_api_calls,
                )
            self.stats["budget_exhausted"] = 1
            return False
        return True

    # -- Hauptablauf ----------------------------------------------------------
    def run(self, categories: list[CategoryConfig]) -> list[HotItem]:
        if not categories:
            raise DiscoveryError("Keine Kategorien konfiguriert (search.categories in config.yaml).")

        found: list[HotItem] = []
        for category in categories:
            leaf_categories: Counter[str] = Counter()
            for keyword in category.keywords:
                if not self._budget_left():
                    break
                found.extend(self._by_keyword(category, keyword, leaf_categories))
            if leaf_categories and self._budget_left():
                found.extend(self._by_bestseller_list(category, leaf_categories.most_common(1)[0][0]))

        self._budget_left()  # Zaehler fuer die Statistik aktualisieren
        return self._rank(found)

    def _by_keyword(
        self, category: CategoryConfig, keyword: str, leaf_categories: Counter[str]
    ) -> list[HotItem]:
        self.stats["keywords"] += 1
        try:
            summaries = self.ebay.search_raw(
                query=keyword, limit=self.config.results_per_keyword, sort="best_match"
            )
        except EbayError as exc:
            if exc.fatal:
                raise DiscoveryError(str(exc)) from exc
            logger.warning("eBay-Suche '%s' uebersprungen: %s", keyword, exc)
            return []

        self.stats["search_hits"] += len(summaries)
        for summary in summaries:
            leaves = summary.get("leafCategoryIds") or []
            if leaves:
                leaf_categories[str(leaves[0])] += 1

        items: list[HotItem] = []
        for summary in summaries[: self.config.detail_lookups_per_keyword]:
            if not self._budget_left():
                break
            item = self._lookup(summary.get("itemId"), category.name, keyword, "suche")
            if item is not None:
                items.append(item)
        logger.info("eBay '%s': %d Treffer, %d mit Details", keyword, len(summaries), len(items))
        return items

    def _by_bestseller_list(self, category: CategoryConfig, ebay_category_id: str) -> list[HotItem]:
        """eBays Bestseller der Kategorie, in der die Suchtreffer ueberwiegend liegen."""
        if self._bestsellers_disabled or ebay_category_id in self._bestseller_categories_done:
            return []
        self._bestseller_categories_done.add(ebay_category_id)

        try:
            products = self.ebay.best_selling(ebay_category_id, self.config.bestsellers_per_category)
        except EbayScopeError as exc:
            # Einmal sagen, dann fuer den Rest des Laufs Ruhe geben.
            logger.warning(
                "Bestseller-Listen nicht verfuegbar (%s) - die Suche ueber "
                "Verkaufsmengen laeuft unveraendert weiter.", exc,
            )
            self._bestsellers_disabled = True
            return []
        except EbayError as exc:
            if exc.fatal:
                raise DiscoveryError(str(exc)) from exc
            logger.warning("Bestseller-Liste Kategorie %s uebersprungen: %s", ebay_category_id, exc)
            return []

        self.stats["bestseller_products"] += len(products)
        items: list[HotItem] = []
        for product in products:
            # Je Produkt zwei Aufrufe (Angebot suchen, Details holen).
            if not self._budget_left():
                break
            epid = product.get("epid")
            if not epid:
                continue
            try:
                listings = self.ebay.search_raw(epid=str(epid), limit=3, sort="best_match")
            except EbayError as exc:
                if exc.fatal:
                    raise DiscoveryError(str(exc)) from exc
                continue
            if not listings or not self._budget_left():
                continue
            item = self._lookup(listings[0].get("itemId"), category.name, None, "bestseller")
            if item is not None:
                items.append(item)
        logger.info("Bestseller Kategorie %s: %d Produkte, %d mit Angebot",
                    ebay_category_id, len(products), len(items))
        return items

    def _lookup(
        self, item_id: Any, category: str, keyword: str | None, origin: Literal["suche", "bestseller"]
    ) -> HotItem | None:
        if not item_id or str(item_id) in self._seen_items:
            return None
        self._seen_items.add(str(item_id))
        try:
            detail = self.ebay.get_item(str(item_id))
        except EbayError as exc:
            if exc.fatal:
                raise DiscoveryError(str(exc)) from exc
            # Haeufigster Grund: Varianten-Angebot, das einen anderen Endpunkt
            # braeuchte. Ein fehlendes Angebot ist kein Grund abzubrechen.
            logger.debug("Details zu %s nicht abrufbar: %s", item_id, exc)
            return None
        self.stats["detail_lookups"] += 1
        return parse_hot_item(detail, category=category, keyword=keyword, origin=origin, now=self._now)

    # -- Auswertung -----------------------------------------------------------
    def _rank(self, items: list[HotItem]) -> list[HotItem]:
        """Duplikate zusammenfassen, filtern, Hoechstpreis berechnen, sortieren."""
        groups: dict[str, list[HotItem]] = {}
        for item in items:
            groups.setdefault(item.group_key, []).append(item)

        ranked: list[HotItem] = []
        for members in groups.values():
            best = max(members, key=lambda i: (i.sold_per_day or 0.0, i.sold_quantity))
            if len(members) > 1:
                self.stats["merged_duplicates"] += len(members) - 1
                best.competing_listings = len(members)
                best.lowest_competitor_price_eur = min(m.total_price_eur for m in members)

            if best.sold_quantity < self.config.min_sold_quantity:
                self.stats["dropped_low_sales"] += 1
                continue
            if self.config.min_sold_per_day > 0 and (best.sold_per_day or 0.0) < self.config.min_sold_per_day:
                self.stats["dropped_low_sales"] += 1
                continue

            best.max_purchase_price_eur = self.calculator.max_purchase_price(
                sale_price_eur=best.price_eur, sale_shipping_income_eur=best.shipping_eur
            )
            if best.max_purchase_price_eur <= 0:
                # Verkauft sich, aber der Preis traegt Gebuehren, Versand und
                # Mindestgewinn nicht -- egal, wie billig der Einkauf waere.
                self.stats["dropped_unprofitable"] += 1
                continue

            # Der Preis des meistverkauften Angebots zeigt, was sich verkauft.
            # Liegt ein Konkurrent spuerbar darunter, ist dessen Preis die
            # vorsichtigere Rechengrundlage -- beide Werte ausweisen.
            lowest = best.lowest_competitor_price_eur
            if lowest is not None and lowest < best.total_price_eur - 0.01:
                best.max_purchase_at_lowest_price_eur = self.calculator.max_purchase_price(
                    sale_price_eur=lowest
                )

            best.research_links = research_links(best.title, best.gtin)
            ranked.append(best)

        ranked.sort(key=lambda i: (i.sold_per_day or 0.0, i.sold_quantity), reverse=True)
        return ranked[: self.config.max_results]


def amazon_pairs(item: HotItem, amazon: Any, limit: int) -> list[CandidatePair]:
    """Kandidatenpaare Amazon -> dieses eBay-Angebot (nur mit Creators-API-Zugang).

    Mit EAN wird exakt gesucht, sonst per bereinigtem Titel. Ob es wirklich
    dasselbe Produkt ist, entscheidet danach der Matcher, nicht diese Suche.
    """
    target = item.as_offer()
    query = item.gtin or build_query(target)
    offers = amazon.search(query)[:limit]
    return [
        CandidatePair(source=offer, target=target, category=item.category, keyword=item.keyword)
        for offer in offers
    ]
