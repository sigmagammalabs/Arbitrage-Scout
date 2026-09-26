"""Tests fuer die automatische Quelle (Einkaufsliste/Amazon -> eBay) und fuer
das Verhalten von Scout.run, wenn eine Quelle mitten im Lauf ausfaellt."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sources  # noqa: E402
from amazon_client import AmazonError  # noqa: E402
from config import AppConfig, CategoryConfig, Secrets, Settings  # noqa: E402
from ebay_client import EbayError  # noqa: E402
from models import CandidatePair, Marketplace, Offer  # noqa: E402


def cat(name: str = "haushalt", max_price: float | None = None, keywords: list[str] | None = None) -> CategoryConfig:
    return CategoryConfig(name=name, keywords=keywords or ["filter"], max_purchase_price_eur=max_price)


def purchase(offer_id: str = "p1", price: float = 20.0, ean: str | None = None, title: str = "BRITA Filter 6er") -> Offer:
    return Offer(offer_id=offer_id, marketplace=Marketplace.AMAZON, title=title, price_eur=price, ean=ean)


def ebay_offer(offer_id: str = "e1", price: float = 40.0) -> Offer:
    return Offer(offer_id=offer_id, marketplace=Marketplace.EBAY, title="BRITA Filter", price_eur=price)


class FakePurchases(sources.PurchaseProvider):
    def __init__(self, by_category: dict[str, list[Offer]]) -> None:
        self.by_category = by_category

    def purchases(self, category: CategoryConfig) -> Iterator[tuple[Offer, str | None]]:
        for o in self.by_category.get(category.name, []):
            yield o, "kw"


class FakeEbay:
    def __init__(self, by_gtin: dict[str, list[Offer]] | None = None,
                 by_query: list[Offer] | None = None, exc: Exception | None = None) -> None:
        self.by_gtin = by_gtin or {}
        self.by_query = by_query if by_query is not None else [ebay_offer()]
        self.exc = exc
        self.calls: list[dict[str, Any]] = []

    def search(self, *, query: str | None = None, gtin: str | None = None, limit: int | None = None) -> list[Offer]:
        self.calls.append({"query": query, "gtin": gtin})
        if self.exc:
            raise self.exc
        if gtin is not None:
            return self.by_gtin.get(gtin, [])
        return self.by_query


def source(purchases: dict[str, list[Offer]], ebay: FakeEbay, cats: list[CategoryConfig] | None = None) -> sources.ApiOfferSource:
    return sources.ApiOfferSource(FakePurchases(purchases), ebay, cats or [cat()])  # type: ignore[arg-type]


# --- ApiOfferSource --------------------------------------------------------------
def test_paart_jedes_einkaufsangebot_mit_ebay_treffern() -> None:
    ebay = FakeEbay(by_query=[ebay_offer("e1"), ebay_offer("e2")])
    pairs = list(source({"haushalt": [purchase()]}, ebay).fetch_pairs())
    assert [p.target.offer_id for p in pairs] == ["e1", "e2"]
    assert all(p.source.offer_id == "p1" and p.category == "haushalt" for p in pairs)


def test_ean_suche_hat_vorrang() -> None:
    ebay = FakeEbay(by_gtin={"4006": [ebay_offer("per-ean")]})
    pairs = list(source({"haushalt": [purchase(ean="4006")]}, ebay).fetch_pairs())
    assert [p.target.offer_id for p in pairs] == ["per-ean"]
    assert ebay.calls == [{"query": None, "gtin": "4006"}]


def test_ean_ohne_treffer_faellt_auf_titel_zurueck() -> None:
    ebay = FakeEbay(by_gtin={}, by_query=[ebay_offer("per-titel")])
    pairs = list(source({"haushalt": [purchase(ean="4006")]}, ebay).fetch_pairs())
    assert [p.target.offer_id for p in pairs] == ["per-titel"]
    assert ebay.calls[1]["query"] == "BRITA Filter 6er"


def test_preisgrenze_der_kategorie_spart_die_ebay_suche() -> None:
    ebay = FakeEbay()
    pairs = list(source({"haushalt": [purchase(price=99.0)]}, ebay, [cat(max_price=50.0)]).fetch_pairs())
    assert pairs == []
    assert ebay.calls == []


def test_doppelte_einkaufsangebote_werden_einmal_geprueft() -> None:
    ebay = FakeEbay(by_query=[ebay_offer()])
    pairs = list(source({"haushalt": [purchase("p1"), purchase("p1")]}, ebay).fetch_pairs())
    assert len(pairs) == 1
    assert len(ebay.calls) == 1


def test_nur_angefragte_kategorie() -> None:
    ebay = FakeEbay()
    src = source({"haushalt": [purchase("p1")], "kfz": [purchase("p2")]}, ebay, [cat("haushalt"), cat("kfz")])
    pairs = list(src.fetch_pairs(cat("kfz")))
    assert [p.source.offer_id for p in pairs] == ["p2"]


def test_voruebergehender_ebay_fehler_ueberspringt_nur_ein_angebot() -> None:
    class Flaky(FakeEbay):
        def search(self, **kw: Any) -> list[Offer]:
            self.calls.append(kw)
            if len(self.calls) == 1:
                raise EbayError("503", fatal=False)
            return [ebay_offer()]

    pairs = list(source({"haushalt": [purchase("p1"), purchase("p2")]}, Flaky()).fetch_pairs())
    assert [p.source.offer_id for p in pairs] == ["p2"]


def test_fataler_ebay_fehler_wird_zu_source_error() -> None:
    ebay = FakeEbay(exc=EbayError("403", fatal=True))
    with pytest.raises(sources.SourceError):
        list(source({"haushalt": [purchase()]}, ebay).fetch_pairs())


def test_quelle_ist_lazy() -> None:
    """Holt der Aufrufer nur ein Paar, darf nur eine eBay-Suche passieren --
    sonst verbrennt ein Limit von 10 Kandidaten trotzdem alle API-Aufrufe."""
    ebay = FakeEbay(by_query=[ebay_offer()])
    gen = source({"haushalt": [purchase(f"p{i}") for i in range(20)]}, ebay).fetch_pairs()
    next(gen)
    assert len(ebay.calls) == 1


def test_ohne_kategorien_klare_fehlermeldung() -> None:
    with pytest.raises(sources.SourceError):
        list(sources.ApiOfferSource(FakePurchases({}), FakeEbay(), []).fetch_pairs())  # type: ignore[arg-type]


# --- AmazonPurchaseProvider ------------------------------------------------------
class FakeAmazon:
    def __init__(self, results: dict[str, Any]) -> None:
        self.results = results

    def search(self, keywords: str) -> list[Offer]:
        r = self.results[keywords]
        if isinstance(r, Exception):
            raise r
        return r


def test_amazon_sucht_je_suchbegriff() -> None:
    provider = sources.AmazonPurchaseProvider(FakeAmazon({"a": [purchase("p1")], "b": [purchase("p2")]}))  # type: ignore[arg-type]
    got = list(provider.purchases(cat(keywords=["a", "b"])))
    assert [(o.offer_id, kw) for o, kw in got] == [("p1", "a"), ("p2", "b")]


def test_amazon_nicht_fataler_fehler_ueberspringt_suchbegriff() -> None:
    provider = sources.AmazonPurchaseProvider(
        FakeAmazon({"a": AmazonError("throttled"), "b": [purchase("p2")]})  # type: ignore[arg-type]
    )
    assert [o.offer_id for o, _ in provider.purchases(cat(keywords=["a", "b"]))] == ["p2"]


def test_amazon_zugangsfehler_bricht_ab() -> None:
    provider = sources.AmazonPurchaseProvider(FakeAmazon({"a": AmazonError("denied", fatal=True)}))  # type: ignore[arg-type]
    with pytest.raises(sources.SourceError):
        list(provider.purchases(cat(keywords=["a"])))


# --- CsvPurchaseProvider -----------------------------------------------------------
HEADER = "category,keyword,marketplace,id,title,price_eur,shipping_eur,brand,ean,qty,condition,url\n"


def test_einkaufsliste_wird_gelesen(tmp_path: Path) -> None:
    f = tmp_path / "purchases.csv"
    f.write_text(
        HEADER
        + "haushalt,filter,aliexpress,a1,Filter 6er,\"12,50\",2.00,BRITA,4006,6,new,https://example.com/a1\n",
        encoding="utf-8",
    )
    got = list(sources.CsvPurchaseProvider(f).purchases(cat()))
    assert len(got) == 1
    o, kw = got[0]
    assert o.marketplace == Marketplace.ALIEXPRESS
    assert o.price_eur == pytest.approx(12.50)
    assert o.shipping_eur == pytest.approx(2.00)
    assert o.ean == "4006" and o.package_quantity == 6
    assert str(o.url) == "https://example.com/a1"
    assert kw == "filter"


def test_zeilen_ohne_preis_werden_uebersprungen(tmp_path: Path) -> None:
    f = tmp_path / "purchases.csv"
    f.write_text(HEADER + "haushalt,,,,Ohne Preis,,,,,,,\nhaushalt,,,,Mit Preis,5,,,,,,\n", encoding="utf-8")
    titles = [o.title for o, _ in sources.CsvPurchaseProvider(f).purchases(cat())]
    assert titles == ["Mit Preis"]


def test_fehlende_einkaufsliste_nennt_die_vorlage(tmp_path: Path) -> None:
    with pytest.raises(sources.SourceError) as exc:
        list(sources.CsvPurchaseProvider(tmp_path / "fehlt.csv").purchases(cat()))
    assert "--write-example-data" in str(exc.value)


def test_unbekannte_kategorie_wird_gemeldet(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    f = tmp_path / "purchases.csv"
    f.write_text(HEADER + "garten,,,,Schlauch,9,,,,,,\n", encoding="utf-8")
    provider = sources.CsvPurchaseProvider(f)
    with caplog.at_level("WARNING"):
        provider.warn_unknown_categories({"haushalt"})
    assert "garten" in caplog.text


def test_vorlage_ist_mit_dem_leser_kompatibel(tmp_path: Path) -> None:
    """Die generierte Vorlage muss sich ohne Fehler einlesen lassen."""
    path = sources.write_purchase_example(tmp_path / "purchases.example.csv")
    provider = sources.CsvPurchaseProvider(path)
    assert len(list(provider.purchases(cat("haushalt")))) == 1
    assert len(list(provider.purchases(cat("elektronik-zubehoer")))) == 1


# --- build_source ------------------------------------------------------------------
def settings_with(**secrets: Any) -> Settings:
    cfg = AppConfig()
    cfg.sources.provider = "api"
    return Settings(config=cfg, secrets=Secrets(_env_file=None, **secrets), config_path=Path("config.yaml"))  # type: ignore[call-arg]


def test_api_quelle_ohne_ebay_keys_ist_source_error() -> None:
    with pytest.raises(sources.SourceError) as exc:
        sources.build_source(settings_with())
    assert "EBAY_APP_ID" in str(exc.value)


def test_api_quelle_amazon_ohne_keys_ist_source_error() -> None:
    s = settings_with(ebay_app_id="a", ebay_cert_id="b")
    s.config.sources.api.purchase_source = "amazon"
    with pytest.raises(sources.SourceError) as exc:
        sources.build_source(s)
    assert "AMAZON_CREATORS" in str(exc.value)


def test_api_quelle_mit_ebay_keys_und_liste() -> None:
    src = sources.build_source(settings_with(ebay_app_id="a", ebay_cert_id="b"))
    assert isinstance(src, sources.ApiOfferSource)
    assert isinstance(src.purchases, sources.CsvPurchaseProvider)


# --- Scout.run bei Ausfall der Quelle ---------------------------------------------
class ScriptedSource(sources.OfferSource):
    """Liefert n Paare, danach einen SourceError."""

    def __init__(self, good: int) -> None:
        self.good = good

    def fetch_pairs(self, category: CategoryConfig | None = None) -> Iterator[CandidatePair]:
        for i in range(self.good):
            yield CandidatePair(
                source=purchase(f"p{i}", price=10.0, title="BRITA Filter 12er Pack"),
                target=Offer(offer_id=f"e{i}", marketplace=Marketplace.EBAY,
                             title="BRITA Filter 12er Pack", price_eur=60.0),
                category="haushalt",
            )
        raise sources.SourceError("Token entzogen")


def make_scout(monkeypatch: pytest.MonkeyPatch, src: sources.OfferSource) -> Any:
    import scout as scout_module

    monkeypatch.setattr(scout_module, "build_source", lambda settings: src)
    s = Settings(config=AppConfig(), secrets=Secrets(_env_file=None), config_path=Path("config.yaml"))  # type: ignore[call-arg]
    return scout_module.Scout(s, offline=True)


def test_ausfall_mitten_im_lauf_behaelt_ergebnisse(monkeypatch: pytest.MonkeyPatch) -> None:
    sc = make_scout(monkeypatch, ScriptedSource(good=2))
    results = sc.run()
    assert sc.stats["seen"] == 2
    assert sc.stats["errors"] == 1
    assert len(results) == 2


def test_ausfall_vor_dem_ersten_paar_wird_gemeldet(monkeypatch: pytest.MonkeyPatch) -> None:
    sc = make_scout(monkeypatch, ScriptedSource(good=0))
    with pytest.raises(sources.SourceError):
        sc.run()


def test_limit_holt_kein_weiteres_paar(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nach Erreichen des Limits darf die Quelle nicht noch einmal abgefragt
    werden -- bei der API-Quelle waere das ein unnoetiger eBay-Aufruf."""
    pulled = []

    class Counting(sources.OfferSource):
        def fetch_pairs(self, category: CategoryConfig | None = None) -> Iterator[CandidatePair]:
            for i in range(10):
                pulled.append(i)
                yield CandidatePair(
                    source=purchase(f"p{i}"),
                    target=Offer(offer_id=f"e{i}", marketplace=Marketplace.EBAY, title="X", price_eur=5.0),
                )

    sc = make_scout(monkeypatch, Counting())
    sc.run(limit=3)
    assert len(pulled) == 3
