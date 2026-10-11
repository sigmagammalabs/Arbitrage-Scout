"""Tests fuer die Produktfindung von der eBay-Seite -- ohne Netzwerk.

Die Antwortstrukturen folgen eBays OpenAPI-Spezifikation der Browse API
(v1.20.4) und der Marketing API (v1_beta.2.0).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discovery  # noqa: E402
from calculator import MarginCalculator  # noqa: E402
from config import CategoryConfig, CostConfig, DiscoveryConfig, MarginConfig, TaxConfig  # noqa: E402
from ebay_client import EbayError, EbayScopeError  # noqa: E402
from models import Marketplace, Offer  # noqa: E402

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def calc() -> MarginCalculator:
    return MarginCalculator(costs=CostConfig(), margin=MarginConfig(), tax=TaxConfig())


def detail(item_id: str = "v1|1|0", price: float = 49.90, sold: int = 120, days: float = 30,
           gtin: str | None = "4006387000001", **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "itemId": item_id,
        "title": f"BRITA Maxtra Pro 6er Pack {item_id}",
        "price": {"value": f"{price:.2f}", "currency": "EUR"},
        "shippingOptions": [{"shippingCost": {"value": "0.00", "currency": "EUR"}}],
        "itemWebUrl": f"https://www.ebay.de/itm/{item_id.split('|')[1]}",
        "seller": {"username": "haendler"},
        "estimatedAvailabilities": [{"estimatedSoldQuantity": sold, "estimatedAvailableQuantity": 10}],
        "itemCreationDate": (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "brand": "BRITA",
        "mpn": "1050414",
        "conditionId": "1000",
        "categoryId": "20684",
        "categoryPath": "Haushalt|Wasserfilter",
    }
    if gtin:
        base["gtin"] = gtin
    base.update(overrides)
    return base


class FakeEbay:
    """Sucht aus einem Katalog; zaehlt Aufrufe wie der echte Client."""

    def __init__(self, details: list[dict[str, Any]], *, bestsellers: Any = None,
                 fail_items: dict[str, Exception] | None = None, search_exc: Exception | None = None) -> None:
        self.details = {d["itemId"]: d for d in details}
        self.order = [d["itemId"] for d in details]
        self.bestsellers = bestsellers
        self.fail_items = fail_items or {}
        self.search_exc = search_exc
        self.stats = {"calls": 0}
        self.log: list[tuple[str, Any]] = []

    def search_raw(self, *, query: str | None = None, gtin: str | None = None, epid: str | None = None,
                   limit: int | None = None, sort: str | None = None) -> list[dict[str, Any]]:
        self.stats["calls"] += 1
        self.log.append(("search", query or epid))
        if self.search_exc:
            raise self.search_exc
        ids = self.order
        if epid is not None:
            ids = [i for i in self.order if self.details[i].get("epid") == epid]
        return [{"itemId": i, "leafCategoryIds": ["20684"]} for i in ids][: limit or 50]

    def get_item(self, item_id: str) -> dict[str, Any]:
        self.stats["calls"] += 1
        self.log.append(("item", item_id))
        if item_id in self.fail_items:
            raise self.fail_items[item_id]
        return self.details[item_id]

    def best_selling(self, category_id: str, limit: int = 10) -> list[dict[str, Any]]:
        self.stats["calls"] += 1
        self.log.append(("bestseller", category_id))
        if isinstance(self.bestsellers, Exception):
            raise self.bestsellers
        return self.bestsellers or []


def run(ebay: FakeEbay, keywords: list[str] | None = None, **cfg: Any) -> tuple[list[discovery.HotItem], discovery.Discoverer]:
    config = DiscoveryConfig(**{"use_bestseller_lists": False, **cfg})
    d = discovery.Discoverer(ebay, config, calc(), now=NOW)  # type: ignore[arg-type]
    items = d.run([CategoryConfig(name="haushalt", keywords=keywords or ["wasserfilter"])])
    return items, d


# --- parse_hot_item --------------------------------------------------------------
def test_parse_uebernimmt_verkaufsmenge_und_tempo() -> None:
    item = discovery.parse_hot_item(detail(sold=120, days=30), category="haushalt", keyword="kw", now=NOW)
    assert item is not None
    assert item.sold_quantity == 120
    assert item.days_listed == pytest.approx(30.0)
    assert item.sold_per_day == pytest.approx(4.0)
    assert item.gtin == "4006387000001" and item.brand == "BRITA" and item.mpn == "1050414"
    assert item.available_quantity == 10


def test_mehrere_availability_eintraege_werden_nicht_addiert() -> None:
    d = detail(estimatedAvailabilities=[{"estimatedSoldQuantity": 40}, {"estimatedSoldQuantity": 40}])
    item = discovery.parse_hot_item(d, category="c", keyword=None, now=NOW)
    assert item is not None and item.sold_quantity == 40


def test_frisches_angebot_gilt_als_mindestens_ein_tag_alt() -> None:
    item = discovery.parse_hot_item(detail(sold=5, days=0.01), category="c", keyword=None, now=NOW)
    assert item is not None and item.sold_per_day == pytest.approx(5.0)


def test_ohne_erstelldatum_kein_tempo() -> None:
    d = detail()
    d.pop("itemCreationDate")
    item = discovery.parse_hot_item(d, category="c", keyword=None, now=NOW)
    assert item is not None and item.sold_per_day is None and item.sold_quantity == 120


def test_fremdwaehrung_und_fehlender_titel_werden_verworfen() -> None:
    foreign = detail()
    foreign["price"] = {"value": "10", "currency": "USD"}
    assert discovery.parse_hot_item(foreign, category="c", keyword=None) is None
    assert discovery.parse_hot_item(detail(title=" "), category="c", keyword=None) is None


def test_als_offer_fuer_den_matcher() -> None:
    item = discovery.parse_hot_item(detail(), category="c", keyword=None, now=NOW)
    assert item is not None
    offer = item.as_offer()
    assert offer.marketplace == Marketplace.EBAY
    assert offer.ean == "4006387000001" and offer.price_eur == pytest.approx(49.90)


# --- Recherche-Links -------------------------------------------------------------
def test_links_suchen_exakt_per_ean() -> None:
    links = discovery.research_links("BRITA Maxtra Pro 6er", "4006387000001")
    assert links["Amazon"] == "https://www.amazon.de/s?k=4006387000001"
    assert "4006387000001" in links["idealo"]
    assert "BRITA+Maxtra+Pro+6er" in links["AliExpress"]  # dort kennt niemand die EAN


def test_links_ohne_ean_per_titel_und_kodiert() -> None:
    links = discovery.research_links("Hub 8-in-1 & HDMI", None)
    assert links["Amazon"] == "https://www.amazon.de/s?k=Hub+8-in-1+%26+HDMI"


# --- Discoverer: Rangfolge und Filter --------------------------------------------
def test_rangfolge_nach_verkaufstempo() -> None:
    ebay = FakeEbay([
        detail("v1|1|0", sold=100, days=100, gtin="1"),   # 1/Tag
        detail("v1|2|0", sold=90, days=10, gtin="2"),     # 9/Tag
        detail("v1|3|0", sold=300, days=100, gtin="3"),   # 3/Tag
    ])
    items, _ = run(ebay)
    assert [i.item_id for i in items] == ["v1|2|0", "v1|3|0", "v1|1|0"]


def test_wenig_verkaeufe_werden_aussortiert() -> None:
    ebay = FakeEbay([detail("v1|1|0", sold=3, gtin="1"), detail("v1|2|0", sold=50, gtin="2")])
    items, d = run(ebay, min_sold_quantity=10)
    assert [i.item_id for i in items] == ["v1|2|0"]
    assert d.stats["dropped_low_sales"] == 1


def test_zu_billige_artikel_fallen_raus_egal_wie_gut_sie_laufen() -> None:
    """9,99 EUR traegt Versand, Gebuehren und Mindestgewinn nicht -- auch bei
    Einkauf zu 0 EUR. Solche Artikel sind Zeitverschwendung, kein Fund."""
    ebay = FakeEbay([detail("v1|1|0", price=9.99, sold=5000, gtin="1")])
    items, d = run(ebay)
    assert items == []
    assert d.stats["dropped_unprofitable"] == 1


def test_hoechstpreis_ist_mit_dem_rechner_konsistent() -> None:
    ebay = FakeEbay([detail("v1|1|0", price=49.90, gtin="1")])
    items, _ = run(ebay)
    limit = items[0].max_purchase_price_eur
    assert limit > 0
    assert calc().calculate(purchase_price_eur=limit, sale_price_eur=49.90).is_recommended is True
    assert calc().calculate(purchase_price_eur=limit + 0.05, sale_price_eur=49.90).is_recommended is False


def test_gleiche_ean_wird_zusammengefasst_mit_konkurrenzlage() -> None:
    ebay = FakeEbay([
        detail("v1|1|0", price=49.90, sold=200, gtin="777"),
        detail("v1|2|0", price=44.90, sold=20, gtin="777"),
        detail("v1|3|0", price=59.90, sold=30, gtin="777"),
    ])
    items, d = run(ebay)
    assert len(items) == 1
    assert items[0].item_id == "v1|1|0"  # das meistverkaufte vertritt die Gruppe
    assert items[0].competing_listings == 3
    assert items[0].lowest_competitor_price_eur == pytest.approx(44.90)
    assert d.stats["merged_duplicates"] == 2


def test_ohne_ean_bleiben_angebote_getrennt() -> None:
    ebay = FakeEbay([detail("v1|1|0", gtin=None), detail("v1|2|0", gtin=None)])
    items, _ = run(ebay)
    assert len(items) == 2


def test_max_results_begrenzt_die_liste() -> None:
    ebay = FakeEbay([detail(f"v1|{i}|0", gtin=str(i)) for i in range(1, 9)])
    items, _ = run(ebay, max_results=3)
    assert len(items) == 3


# --- Discoverer: Kostenkontrolle und Fehler ---------------------------------------
def test_details_nur_fuer_die_obersten_treffer() -> None:
    ebay = FakeEbay([detail(f"v1|{i}|0", gtin=str(i)) for i in range(1, 11)])
    _, d = run(ebay, detail_lookups_per_keyword=4)
    assert d.stats["detail_lookups"] == 4
    assert d.stats["api_calls"] == 5  # 1 Suche + 4 Details


def test_api_budget_stoppt_den_lauf() -> None:
    ebay = FakeEbay([detail(f"v1|{i}|0", gtin=str(i)) for i in range(1, 40)])
    items, d = run(ebay, keywords=["a", "b", "c"], detail_lookups_per_keyword=30, max_api_calls=10)
    assert d.stats["api_calls"] <= 10
    assert d.stats["budget_exhausted"] == 1
    assert items  # was bis dahin gefunden wurde, bleibt


def test_dasselbe_angebot_wird_nur_einmal_abgerufen() -> None:
    ebay = FakeEbay([detail("v1|1|0")])
    _, d = run(ebay, keywords=["a", "b"])
    assert d.stats["detail_lookups"] == 1


def test_nicht_abrufbares_angebot_wird_uebersprungen() -> None:
    ebay = FakeEbay(
        [detail("v1|1|0", gtin="1"), detail("v1|2|0", gtin="2")],
        fail_items={"v1|1|0": EbayError("HTTP 404", fatal=False)},
    )
    items, _ = run(ebay)
    assert [i.item_id for i in items] == ["v1|2|0"]


def test_fataler_fehler_bricht_ab() -> None:
    ebay = FakeEbay([], search_exc=EbayError("403", fatal=True))
    with pytest.raises(discovery.DiscoveryError):
        run(ebay)


def test_ohne_kategorien_klare_meldung() -> None:
    d = discovery.Discoverer(FakeEbay([]), DiscoveryConfig(), calc())  # type: ignore[arg-type]
    with pytest.raises(discovery.DiscoveryError):
        d.run([])


# --- Bestseller-Listen ------------------------------------------------------------
def test_bestseller_liefert_zusaetzliche_artikel() -> None:
    ebay = FakeEbay(
        [detail("v1|1|0", gtin="1"), detail("v1|9|0", gtin="9", sold=900, epid="E9")],
        bestsellers=[{"epid": "E9", "title": "Bestseller"}],
    )
    # Die Stichwortsuche schaut nur auf den ersten Treffer; den zweiten bringt
    # erst die Bestseller-Liste der Kategorie.
    items, d = run(ebay, use_bestseller_lists=True, detail_lookups_per_keyword=1)
    by_id = {i.item_id: i for i in items}
    assert by_id["v1|9|0"].origin == "bestseller"
    assert by_id["v1|1|0"].origin == "suche"
    assert ("bestseller", "20684") in ebay.log  # Kategorie aus den Suchtreffern abgeleitet
    assert d.stats["bestseller_products"] == 1


def test_fehlende_berechtigung_schaltet_bestseller_ab_und_laeuft_weiter(caplog: pytest.LogCaptureFixture) -> None:
    ebay = FakeEbay([detail("v1|1|0", gtin="1")], bestsellers=EbayScopeError("buy.marketing fehlt"))
    config = DiscoveryConfig(use_bestseller_lists=True)
    d = discovery.Discoverer(ebay, config, calc(), now=NOW)  # type: ignore[arg-type]
    cats = [CategoryConfig(name="a", keywords=["x"]), CategoryConfig(name="b", keywords=["y"])]
    with caplog.at_level("WARNING"):
        items = d.run(cats)
    assert len(items) == 1
    assert [e for e in ebay.log if e[0] == "bestseller"] == [("bestseller", "20684")]  # nur ein Versuch
    assert caplog.text.count("Bestseller-Listen nicht verfuegbar") == 1


def test_bestseller_abgeschaltet_macht_keinen_aufruf() -> None:
    ebay = FakeEbay([detail("v1|1|0")], bestsellers=[{"epid": "E1"}])
    run(ebay, use_bestseller_lists=False)
    assert not [e for e in ebay.log if e[0] == "bestseller"]


# --- Amazon-Gegenpruefung ----------------------------------------------------------
class FakeAmazon:
    def __init__(self, n: int = 5) -> None:
        self.queries: list[str] = []
        self.n = n

    def search(self, keywords: str) -> list[Offer]:
        self.queries.append(keywords)
        return [Offer(offer_id=f"amz-{i}", marketplace=Marketplace.AMAZON, title="BRITA", price_eur=20.0)
                for i in range(self.n)]


def test_amazon_paare_suchen_per_ean_und_begrenzen() -> None:
    item = discovery.parse_hot_item(detail(), category="haushalt", keyword="kw", now=NOW)
    assert item is not None
    amazon = FakeAmazon()
    pairs = discovery.amazon_pairs(item, amazon, limit=3)
    assert amazon.queries == ["4006387000001"]
    assert len(pairs) == 3
    assert pairs[0].source.marketplace == Marketplace.AMAZON
    assert pairs[0].target.marketplace == Marketplace.EBAY
    assert pairs[0].category == "haushalt"


def test_amazon_paare_ohne_ean_per_titel() -> None:
    item = discovery.parse_hot_item(detail(gtin=None, title="BRITA Maxtra (Pro) 6er!"), category="c", keyword=None, now=NOW)
    assert item is not None
    amazon = FakeAmazon(1)
    discovery.amazon_pairs(item, amazon, limit=3)
    assert amazon.queries == ["BRITA Maxtra Pro 6er"]


# --- Rechner: Hoechstpreis ---------------------------------------------------------
@pytest.mark.parametrize(("sale", "ship", "qs", "qt"), [(49.95, 0, 1, 1), (34.90, 0, 12, 3), (100, 0, 1, 1), (79.0, 4.99, 2, 1)])
def test_hoechstpreis_trifft_die_schwelle_genau(sale: float, ship: float, qs: int, qt: int) -> None:
    c = calc()
    limit = c.max_purchase_price(sale_price_eur=sale, sale_shipping_income_eur=ship, quantity_source=qs, quantity_target=qt)
    kw = dict(sale_price_eur=sale, sale_shipping_income_eur=ship, quantity_source=qs, quantity_target=qt)
    assert c.calculate(purchase_price_eur=limit, **kw).is_recommended is True
    # Knapp darueber (ein Cent je verkaufter Einheit mehr) reicht es nicht mehr.
    assert c.calculate(purchase_price_eur=limit + 0.02 * qs / qt, **kw).is_recommended is False


def test_hoechstpreis_null_wenn_verkaufspreis_nicht_reicht() -> None:
    assert calc().max_purchase_price(sale_price_eur=12.0) == 0.0
    assert calc().max_purchase_price(sale_price_eur=0.0) == 0.0


def test_hoechstpreis_beruecksichtigt_zoll_und_wareneingang() -> None:
    plain = calc().max_purchase_price(sale_price_eur=80.0)
    costly = MarginCalculator(
        costs=CostConfig(import_duty_percent=10.0, inbound_shipping_eur=3.0),
        margin=MarginConfig(), tax=TaxConfig(),
    )
    limit = costly.max_purchase_price(sale_price_eur=80.0)
    assert 0 < limit < plain
    assert costly.calculate(purchase_price_eur=limit, sale_price_eur=80.0).is_recommended is True


# --- Konkurrenzpreis ---------------------------------------------------------------
def test_hoechstpreis_auch_zum_preis_des_guenstigsten_konkurrenten() -> None:
    ebay = FakeEbay([
        detail("v1|1|0", price=49.90, sold=300, gtin="777"),
        detail("v1|2|0", price=44.90, sold=20, gtin="777"),
    ])
    items, _ = run(ebay)
    item = items[0]
    assert item.max_purchase_at_lowest_price_eur is not None
    assert 0 < item.max_purchase_at_lowest_price_eur < item.max_purchase_price_eur
    assert item.max_purchase_at_lowest_price_eur == calc().max_purchase_price(sale_price_eur=44.90)


def test_kein_zweiter_hoechstpreis_wenn_das_fundstueck_schon_das_guenstigste_ist() -> None:
    ebay = FakeEbay([
        detail("v1|1|0", price=44.90, sold=300, gtin="777"),
        detail("v1|2|0", price=49.90, sold=20, gtin="777"),
    ])
    items, _ = run(ebay)
    assert items[0].max_purchase_at_lowest_price_eur is None


def test_konkurrenzpreis_kann_den_artikel_unrentabel_machen() -> None:
    """Das Fundstueck bleibt in der Liste (es verkauft sich ja zum hoeheren
    Preis), aber der zweite Wert zeigt 0 -- das ist die Warnung."""
    ebay = FakeEbay([
        detail("v1|1|0", price=39.90, sold=300, gtin="777"),
        detail("v1|2|0", price=14.90, sold=20, gtin="777"),
    ])
    items, _ = run(ebay)
    assert items[0].max_purchase_price_eur > 0
    assert items[0].max_purchase_at_lowest_price_eur == 0.0
