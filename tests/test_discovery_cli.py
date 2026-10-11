"""Ende-zu-Ende-Tests fuer ``scout.py --discover`` sowie die dafuer ergaenzten
Teile von eBay-Client, Telegram-Format und Listener -- alles ohne Netzwerk."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ebay_client  # noqa: E402
import listener  # noqa: E402
import notify  # noqa: E402
import scout  # noqa: E402
from config import (  # noqa: E402
    ApiSourceConfig,
    AppConfig,
    CategoryConfig,
    HttpSourceConfig,
    Secrets,
    Settings,
)
from models import Marketplace, Offer  # noqa: E402
from test_discovery import NOW, FakeEbay, detail  # noqa: E402
from test_ebay_client import TOKEN_OK, FakeResponse, FakeSession  # noqa: E402

import discovery  # noqa: E402  (nach den Test-Helfern, die es ebenfalls laden)


# --- CLI ---------------------------------------------------------------------------
@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """Startet scout.main() mit Fake-eBay; Ausgaben landen in tmp_path."""

    def run(argv: list[str], *, ebay: Any = None, amazon: Any = None, **secrets: Any) -> int:
        cfg = AppConfig()
        # Explizit setzen: AppConfig() liest sonst, was ein frueherer Test per
        # load_settings() als YAML hinterlassen hat -- oder gar nichts.
        cfg.search.categories = [CategoryConfig(name="haushalt", keywords=["wasserfilter"])]
        cfg.discovery.use_bestseller_lists = False
        cfg.logging.console = False
        settings = Settings(
            config=cfg,
            secrets=Secrets(_env_file=None, **secrets),  # type: ignore[call-arg]
            config_path=tmp_path / "config.yaml",
            base_dir=tmp_path,
        )
        monkeypatch.setattr(scout, "load_settings", lambda path=None: settings)
        monkeypatch.setattr(scout, "build_ebay_client", lambda s: ebay or FakeEbay([]))
        if amazon is not None:
            monkeypatch.setattr(scout, "build_amazon_client", lambda s: amazon)
        return scout.main(["--discover", "--no-lock", *argv])

    return run


CATALOG = [
    detail("v1|1|0", price=49.90, sold=300, days=30, gtin="111"),
    detail("v1|2|0", price=9.99, sold=900, days=30, gtin="222"),   # zu billig
    detail("v1|3|0", price=79.00, sold=2, days=30, gtin="333"),    # verkauft sich nicht
]


def test_discover_zeigt_funde_mit_hoechstpreis_und_links(cli: Any, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli(["--no-export"], ebay=FakeEbay(CATALOG)) == scout.EXIT_OK
    out = capsys.readouterr().out
    assert "1 gut laufende Artikel" in out
    assert "v1|1|0" in out and "v1|2|0" not in out and "v1|3|0" not in out
    assert "Max. Einkauf" in out
    assert "https://www.amazon.de/s?k=111" in out
    assert "1 zu wenig Verkaeufe" in out and "1 Preis traegt die Kosten nicht" in out
    assert "data/purchases.csv" in out  # Hinweis auf den naechsten Schritt ohne Amazon-Zugang


def test_discover_exportiert_csv_und_json(cli: Any, tmp_path: Path) -> None:
    assert cli(["--quiet"], ebay=FakeEbay(CATALOG)) == scout.EXIT_OK
    csv_files = list((tmp_path / "data").glob("discovery-*.csv"))
    json_files = list((tmp_path / "data").glob("discovery-*.json"))
    assert len(csv_files) == 1 and len(json_files) == 1

    rows = list(csv.DictReader(csv_files[0].open(encoding="utf-8")))
    assert len(rows) == 1
    assert rows[0]["rank"] == "1" and rows[0]["gtin"] == "111"
    assert float(rows[0]["max_purchase_price_eur"]) > 0
    assert rows[0]["link_amazon"] == "https://www.amazon.de/s?k=111"

    payload = json.loads(json_files[0].read_text(encoding="utf-8"))
    assert payload["count"] == 1 and payload["items"][0]["sold_quantity"] == 300


def test_no_export_schreibt_nichts(cli: Any, tmp_path: Path) -> None:
    cli(["--no-export", "--quiet"], ebay=FakeEbay(CATALOG))
    assert not list((tmp_path / "data").glob("discovery-*")) if (tmp_path / "data").exists() else True


def test_limit_begrenzt_die_funde(cli: Any, capsys: pytest.CaptureFixture[str]) -> None:
    many = [detail(f"v1|{i}|0", gtin=str(i)) for i in range(1, 8)]
    cli(["--no-export", "--limit", "2"], ebay=FakeEbay(many))
    assert "2 gut laufende Artikel" in capsys.readouterr().out


def test_unbekannte_kategorie_ist_konfigurationsfehler(cli: Any) -> None:
    assert cli(["--category", "gibtsnicht"], ebay=FakeEbay(CATALOG)) == scout.EXIT_CONFIG


def test_fehlende_ebay_keys_sind_konfigurationsfehler(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.logging.console = False
    settings = Settings(config=cfg, secrets=Secrets(_env_file=None),  # type: ignore[call-arg]
                        config_path=tmp_path / "c.yaml", base_dir=tmp_path)
    monkeypatch.setattr(scout, "load_settings", lambda path=None: settings)
    assert scout.main(["--discover", "--no-lock", "--quiet"]) == scout.EXIT_CONFIG


def test_telegram_bekommt_die_fundliste(cli: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    monkeypatch.setattr(notify, "send_message", lambda settings, text, **kw: sent.append(text) or True)
    cli(["--no-export", "--quiet", "--notify-telegram"], ebay=FakeEbay(CATALOG),
        telegram_bot_token="1:a", telegram_chat_id="42")
    assert len(sent) == 1
    assert "Produktfindung" in sent[0] and "max. Einkauf" in sent[0]
    assert '<a href="https://www.amazon.de/s?k=111">Amazon</a>' in sent[0]


class CheapAmazon:
    """Liefert zu jeder Suche dasselbe Produkt deutlich guenstiger."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, keywords: str) -> list[Offer]:
        self.queries.append(keywords)
        return [Offer(offer_id="amz-B0X", marketplace=Marketplace.AMAZON,
                      title="BRITA Maxtra Pro 6er Pack v1|1|0", price_eur=15.00, ean="111",
                      url="https://www.amazon.de/dp/B0X")]  # type: ignore[arg-type]


AMAZON_SECRETS = dict(
    amazon_creators_credential_id="i", amazon_creators_credential_secret="s",
    amazon_creators_credential_version="3.2", amazon_partner_tag="t-21",
)


def test_mit_amazon_zugang_wird_die_einkaufsquelle_bestaetigt(
    cli: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []
    monkeypatch.setattr(notify, "send_message", lambda settings, text, **kw: sent.append(text) or True)
    amazon = CheapAmazon()
    code = cli(["--no-export", "--notify-telegram"], ebay=FakeEbay(CATALOG), amazon=amazon,
               telegram_bot_token="1:a", telegram_chat_id="42", **AMAZON_SECRETS)
    out = capsys.readouterr().out
    assert code == scout.EXIT_OK
    assert amazon.queries == ["111"]  # exakt per EAN gesucht
    assert "Gegenpruefung auf Amazon" in out
    assert "https://www.amazon.de/dp/B0X" in out
    assert "data/purchases.csv" not in out  # der Hinweis fuer "ohne Amazon" entfaellt
    assert "mit Amazon-Einkaufsquelle bestaetigt" in sent[0]


def test_amazon_ausfall_laesst_die_fundliste_stehen(cli: Any, capsys: pytest.CaptureFixture[str]) -> None:
    from amazon_client import AmazonError

    class Broken:
        def search(self, keywords: str) -> list[Offer]:
            raise AmazonError("denied", fatal=True)

    code = cli(["--no-export"], ebay=FakeEbay(CATALOG), amazon=Broken(), **AMAZON_SECRETS)
    assert code == scout.EXIT_OK
    assert "1 gut laufende Artikel" in capsys.readouterr().out


# --- Telegram-Format -----------------------------------------------------------------
def hot(**overrides: Any) -> discovery.HotItem:
    item = discovery.parse_hot_item(detail(), category="haushalt", keyword="kw", now=NOW)
    assert item is not None
    item.max_purchase_price_eur = 22.87
    item.research_links = discovery.research_links(item.title, item.gtin)
    for key, value in overrides.items():
        setattr(item, key, value)
    return item


def test_format_nennt_tempo_hoechstpreis_und_konkurrenz() -> None:
    text = notify.format_discovery_summary(
        [hot(competing_listings=3, lowest_competitor_price_eur=44.90)], [], {"api_calls": 7}, run_id="r1"
    )
    assert "120 verkauft, ~4.0/Tag" in text
    assert "max. Einkauf <b>22.87 €</b>" in text
    assert "3 Anbieter, guenstigster 44.90 €" in text
    assert "7 eBay-Aufrufe" in text


def test_format_leere_fundliste() -> None:
    assert "Keine gut laufenden Artikel" in notify.format_discovery_summary([], [], {}, run_id="r1")


def test_format_escaped_titel() -> None:
    text = notify.format_discovery_summary([hot(title="<b>Fake</b> & Co")], [], {}, run_id="r1")
    assert "&lt;b&gt;Fake&lt;/b&gt; &amp; Co" in text


def test_format_bleibt_unter_dem_telegram_limit() -> None:
    items = [hot(item_id=f"v1|{i}|0", title="Sehr langer Titel " * 5) for i in range(60)]
    text = notify.format_discovery_summary(items, [], {}, run_id="r1", max_items=60)
    assert len(text) <= notify.MAX_MESSAGE_LENGTH


# --- Listener ----------------------------------------------------------------------
def test_listener_discover_startet_die_produktfindung(tmp_path: Path) -> None:
    started: list[str] = []

    class P:
        pid = 1

        def poll(self) -> None:
            return None

    state = listener.ListenerState(
        tmp_path / "scout.lock",
        spawn=lambda: started.append("scan") or P(),
        spawn_discovery=lambda: started.append("discover") or P(),
    )
    reply = listener.dispatch("/discover", state)
    assert started == ["discover"]
    assert "Produktfindung gestartet" in reply


def test_spawn_discover_haengt_das_flag_an(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(listener.subprocess, "Popen", lambda args, **kw: captured.update(args=args))
    listener._spawn_scan(python_bin="py", discover=True)
    assert captured["args"][-1] == "--discover"
    listener._spawn_scan(python_bin="py")
    assert "--discover" not in captured["args"]


# --- eBay-Client: neue Abrufe -----------------------------------------------------
@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ebay_client.time, "sleep", lambda s: None)


def client(session: FakeSession) -> ebay_client.EbayClient:
    return ebay_client.EbayClient("id", "secret", ApiSourceConfig(), HttpSourceConfig(max_retries=1), session=session)


def test_get_item_kodiert_die_pipes_in_der_id() -> None:
    session = FakeSession(gets=[FakeResponse(200, {"itemId": "v1|123|0"})])
    assert client(session).get_item("v1|123|0")["itemId"] == "v1|123|0"
    assert session.get_calls[0]["url"] == "https://api.ebay.com/buy/browse/v1/item/v1%7C123%7C0"


def test_search_raw_per_epid_und_ohne_preissortierung() -> None:
    session = FakeSession(gets=[FakeResponse(200, {"itemSummaries": [{"itemId": "x"}]})])
    c = client(session)
    c.api.ebay_sort = "price"
    assert c.search_raw(epid="E1", sort="best_match") == [{"itemId": "x"}]
    params = session.get_calls[0]["params"]
    assert params["epid"] == "E1" and "sort" not in params


def test_best_selling_nutzt_eigenen_token_und_endpunkt() -> None:
    session = FakeSession(
        gets=[FakeResponse(200, {"merchandisedProducts": [{"epid": "E1"}]})],
        posts=[FakeResponse(200, {"access_token": "tok-marketing", "expires_in": 7200})],
    )
    assert client(session).best_selling("20684", limit=5) == [{"epid": "E1"}]
    assert session.post_calls[0]["data"]["scope"] == ebay_client.MARKETING_SCOPE
    call = session.get_calls[0]
    assert call["url"] == ebay_client.BEST_SELLING_URL
    assert call["params"] == {"metric_name": "BEST_SELLING", "category_id": "20684", "limit": 5}
    assert call["headers"]["Authorization"] == "Bearer tok-marketing"


def test_fehlende_berechtigung_beim_token_ist_scope_error_nicht_fatal() -> None:
    session = FakeSession(gets=[], posts=[FakeResponse(400, text='{"error":"invalid_scope"}')])
    with pytest.raises(ebay_client.EbayScopeError) as exc:
        client(session).best_selling("20684")
    assert exc.value.fatal is False
    assert "buy.marketing" in str(exc.value)


def test_403_auf_bestseller_ist_scope_error_nicht_fatal() -> None:
    session = FakeSession(gets=[FakeResponse(403, text="Insufficient permissions")])
    with pytest.raises(ebay_client.EbayScopeError) as exc:
        client(session).best_selling("20684")
    assert exc.value.fatal is False


def test_token_werden_je_berechtigung_getrennt_gehalten() -> None:
    """Der Marketing-Token darf den normalen nicht ersetzen -- sonst schlaegt
    nach einem Bestseller-Abruf die normale Suche fehl."""
    session = FakeSession(
        gets=[FakeResponse(200, {}), FakeResponse(200, {}), FakeResponse(200, {})],
        posts=[TOKEN_OK, FakeResponse(200, {"access_token": "tok-marketing", "expires_in": 7200})],
    )
    c = client(session)
    c.search_raw(query="a")
    c.best_selling("1")
    c.search_raw(query="b")
    auth = [call["headers"]["Authorization"] for call in session.get_calls]
    assert auth == ["Bearer tok-1", "Bearer tok-marketing", "Bearer tok-1"]
    assert len(session.post_calls) == 2
