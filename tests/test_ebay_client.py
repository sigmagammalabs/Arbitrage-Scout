"""Tests fuer den eBay-Browse-Client -- ohne Netzwerk, mit Fake-Session."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ebay_client  # noqa: E402
from config import ApiSourceConfig, HttpSourceConfig  # noqa: E402
from models import Condition, Marketplace, Offer  # noqa: E402


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict[str, Any] | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or str(self._payload)

    def json(self) -> dict[str, Any]:
        return self._payload


TOKEN_OK = FakeResponse(200, {"access_token": "tok-1", "expires_in": 7200})


class FakeSession:
    """Spielt vorgegebene Antworten ab und protokolliert die Aufrufe."""

    def __init__(self, gets: list[Any], posts: list[Any] | None = None) -> None:
        self.gets = list(gets)
        self.posts = list(posts) if posts is not None else [TOKEN_OK]
        self.get_calls: list[dict[str, Any]] = []
        self.post_calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> Any:
        self.post_calls.append({"url": url, **kwargs})
        item = self.posts.pop(0) if len(self.posts) > 1 else self.posts[0]
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url: str, **kwargs: Any) -> Any:
        self.get_calls.append({"url": url, **kwargs})
        item = self.gets.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def item(**overrides: Any) -> dict[str, Any]:
    base = {
        "itemId": "v1|123|0",
        "title": "BRITA Maxtra Pro All-in-1 6er Pack",
        "price": {"value": "39.90", "currency": "EUR"},
        "shippingOptions": [{"shippingCost": {"value": "4.99", "currency": "EUR"}}],
        "itemWebUrl": "https://www.ebay.de/itm/123",
        "conditionId": "1000",
        "seller": {"username": "haendler42"},
    }
    base.update(overrides)
    return base


def make_client(session: FakeSession, **api_overrides: Any) -> ebay_client.EbayClient:
    return ebay_client.EbayClient(
        "app-id", "cert-id",
        ApiSourceConfig(**api_overrides),
        HttpSourceConfig(max_retries=2),
        session=session,
    )


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ebay_client.time, "sleep", lambda s: None)


# --- build_query ---------------------------------------------------------------
def offer(title: str, brand: str | None = None) -> Offer:
    return Offer(offer_id="s", marketplace=Marketplace.AMAZON, title=title, price_eur=10.0, brand=brand)


def test_query_stellt_fehlende_marke_voran() -> None:
    assert ebay_client.build_query(offer("Maxtra Kartusche", brand="BRITA")) == "BRITA Maxtra Kartusche"


def test_query_doppelt_marke_nicht() -> None:
    assert ebay_client.build_query(offer("BRITA Maxtra", brand="brita")) == "BRITA Maxtra"


def test_query_entfernt_sonderzeichen() -> None:
    assert ebay_client.build_query(offer("Hub (8-in-1) | HDMI® 4K!")) == "Hub 8-in-1 HDMI 4K"


def test_query_kuerzt_an_wortgrenze() -> None:
    q = ebay_client.build_query(offer("Wort " * 40))
    assert len(q) <= 90
    assert not q.endswith(" ")
    assert q.split()[-1] == "Wort"


# --- Zustand -------------------------------------------------------------------
@pytest.mark.parametrize(
    ("condition_id", "expected"),
    [
        ("1000", Condition.NEW),
        ("1500", Condition.UNKNOWN),  # "Neu: Sonstige" -- bewusst nicht NEW
        ("2000", Condition.REFURBISHED),
        ("2500", Condition.REFURBISHED),
        ("3000", Condition.USED),
        ("7000", Condition.UNKNOWN),
        ("", Condition.UNKNOWN),
    ],
)
def test_zustand_mapping(condition_id: str, expected: Condition) -> None:
    assert ebay_client._map_condition(condition_id) == expected


# --- Parsing -------------------------------------------------------------------
def test_parse_item_uebernimmt_preis_versand_und_link() -> None:
    o = ebay_client.EbayClient._parse_item(item())
    assert o is not None
    assert o.marketplace == Marketplace.EBAY
    assert o.price_eur == pytest.approx(39.90)
    assert o.shipping_eur == pytest.approx(4.99)
    assert str(o.url) == "https://www.ebay.de/itm/123"
    assert o.condition == Condition.NEW
    assert o.seller == "haendler42"


def test_parse_item_ohne_versandangabe_ist_versandfrei() -> None:
    o = ebay_client.EbayClient._parse_item(item(shippingOptions=[]))
    assert o is not None and o.shipping_eur == 0.0


def test_parse_item_verwirft_fremdwaehrung() -> None:
    assert ebay_client.EbayClient._parse_item(item(price={"value": "10", "currency": "USD"})) is None


def test_parse_item_verwirft_leeren_titel() -> None:
    assert ebay_client.EbayClient._parse_item(item(title="")) is None


# --- Token ---------------------------------------------------------------------
def test_token_wird_wiederverwendet() -> None:
    session = FakeSession(gets=[FakeResponse(200, {"itemSummaries": []})] * 2)
    client = make_client(session)
    client.search(query="a")
    client.search(query="b")
    assert len(session.post_calls) == 1
    assert client.stats["token_refreshes"] == 1


def test_token_anfrage_nutzt_client_credentials() -> None:
    session = FakeSession(gets=[FakeResponse(200, {})])
    make_client(session).search(query="x")
    call = session.post_calls[0]
    assert call["url"] == ebay_client.TOKEN_URL
    assert call["data"]["grant_type"] == "client_credentials"
    assert call["headers"]["Authorization"].startswith("Basic ")


def test_abgelehnte_zugangsdaten_sind_fatal() -> None:
    session = FakeSession(gets=[], posts=[FakeResponse(401, text="invalid_client")])
    with pytest.raises(ebay_client.EbayError) as exc:
        make_client(session).search(query="x")
    assert exc.value.fatal is True


# --- Suche ---------------------------------------------------------------------
def test_suche_per_gtin_und_filter() -> None:
    session = FakeSession(gets=[FakeResponse(200, {"itemSummaries": [item()]})])
    results = make_client(session).search(gtin="4006387123456")
    params = session.get_calls[0]["params"]
    headers = session.get_calls[0]["headers"]
    assert params["gtin"] == "4006387123456"
    assert "q" not in params
    assert "conditionIds:{1000}" in params["filter"]
    assert "buyingOptions:{FIXED_PRICE}" in params["filter"]
    assert headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_DE"
    assert headers["Authorization"] == "Bearer tok-1"
    assert len(results) == 1


def test_gebraucht_erlaubt_laesst_zustandsfilter_weg() -> None:
    session = FakeSession(gets=[FakeResponse(200, {})])
    make_client(session, ebay_new_only=False).search(query="x")
    assert "conditionIds" not in session.get_calls[0]["params"]["filter"]


def test_sortierung_nach_preis() -> None:
    session = FakeSession(gets=[FakeResponse(200, {})])
    make_client(session, ebay_sort="price").search(query="x")
    assert session.get_calls[0]["params"]["sort"] == "price"


def test_suche_ohne_kriterium_ist_programmierfehler() -> None:
    with pytest.raises(ValueError):
        make_client(FakeSession(gets=[])).search()


def test_401_bei_suche_erneuert_token_einmal() -> None:
    session = FakeSession(
        gets=[FakeResponse(401), FakeResponse(200, {"itemSummaries": [item()]})],
        posts=[TOKEN_OK, FakeResponse(200, {"access_token": "tok-2", "expires_in": 7200})],
    )
    client = make_client(session)
    assert len(client.search(query="x")) == 1
    assert session.get_calls[1]["headers"]["Authorization"] == "Bearer tok-2"


def test_429_wird_wiederholt() -> None:
    session = FakeSession(gets=[FakeResponse(429), FakeResponse(200, {"itemSummaries": [item()]})])
    assert len(make_client(session).search(query="x")) == 1
    assert len(session.get_calls) == 2


def test_dauerhafte_serverfehler_geben_auf() -> None:
    session = FakeSession(gets=[FakeResponse(503)] * 5)
    with pytest.raises(ebay_client.EbayError) as exc:
        make_client(session).search(query="x")
    assert exc.value.fatal is False
    assert len(session.get_calls) == 3  # max_retries=2 -> 3 Versuche


def test_403_ist_fatal() -> None:
    session = FakeSession(gets=[FakeResponse(403, text="Insufficient permissions")])
    with pytest.raises(ebay_client.EbayError) as exc:
        make_client(session).search(query="x")
    assert exc.value.fatal is True
