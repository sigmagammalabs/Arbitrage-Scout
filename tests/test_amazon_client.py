"""Tests fuer den Amazon-Creators-Client -- ohne Netzwerk, ohne Zugangsdaten.

Items werden als einfache Namespaces nachgebaut (gleiche Attributpfade wie die
SDK-Modelle, per Introspektion des installierten Pakets ermittelt). Die
Fehlerabbildung nutzt die echten Fehlerklassen der Bibliothek.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import amazon_client  # noqa: E402
from config import ApiSourceConfig  # noqa: E402
from models import Condition, Marketplace  # noqa: E402

pytestmark = pytest.mark.skipif(
    not amazon_client.AMAZON_AVAILABLE, reason="python-amazon-paapi nicht installiert"
)


def listing(amount: float = 29.99, currency: str = "EUR", buy_box: bool = False, condition: str = "New") -> NS:
    return NS(
        price=NS(money=NS(amount=amount, currency=currency, display_amount=f"{amount} €")),
        is_buy_box_winner=buy_box,
        condition=NS(value=condition),
        availability=NS(type="IN_STOCK"),
    )


def amazon_item(listings: list[NS] | None = None, title: str | None = "BRITA Maxtra Pro 6er",
                eans: list[str] | None = None, brand: str | None = "BRITA") -> NS:
    return NS(
        asin="B0TEST1234",
        detail_page_url="https://www.amazon.de/dp/B0TEST1234?tag=test-21",
        item_info=NS(
            title=NS(display_value=title) if title is not None else None,
            by_line_info=NS(brand=NS(display_value=brand)) if brand else None,
            external_ids=NS(eans=NS(display_values=eans)) if eans else None,
        ),
        offers_v2=NS(listings=listings if listings is not None else [listing()]),
    )


# --- parse_item ----------------------------------------------------------------
def test_parse_uebernimmt_kernfelder() -> None:
    o = amazon_client.parse_item(amazon_item(eans=["4006387123456"]))
    assert o is not None
    assert o.marketplace == Marketplace.AMAZON
    assert o.offer_id == "amz-B0TEST1234"
    assert o.asin == "B0TEST1234"
    assert o.price_eur == pytest.approx(29.99)
    assert o.shipping_eur == 0.0
    assert o.brand == "BRITA"
    assert o.ean == "4006387123456"
    assert o.condition == Condition.NEW
    assert "amazon.de/dp/B0TEST1234" in str(o.url)


def test_buy_box_angebot_hat_vorrang() -> None:
    o = amazon_client.parse_item(amazon_item(listings=[listing(19.99), listing(24.99, buy_box=True)]))
    assert o is not None and o.price_eur == pytest.approx(24.99)


def test_ohne_buy_box_erstes_angebot() -> None:
    o = amazon_client.parse_item(amazon_item(listings=[listing(19.99), listing(24.99)]))
    assert o is not None and o.price_eur == pytest.approx(19.99)


def test_fremdwaehrung_wird_verworfen() -> None:
    assert amazon_client.parse_item(amazon_item(listings=[listing(currency="GBP")])) is None


def test_ohne_angebot_wird_verworfen() -> None:
    assert amazon_client.parse_item(amazon_item(listings=[])) is None


def test_ohne_titel_wird_verworfen() -> None:
    assert amazon_client.parse_item(amazon_item(title=None)) is None


def test_fehlende_optionale_ressourcen_sind_kein_fehler() -> None:
    o = amazon_client.parse_item(amazon_item(brand=None, eans=None))
    assert o is not None and o.brand is None and o.ean is None


def test_gebraucht_wird_erkannt() -> None:
    o = amazon_client.parse_item(amazon_item(listings=[listing(condition="Used")]))
    assert o is not None and o.condition == Condition.USED


# --- AmazonClient.search -------------------------------------------------------
class FakeApi:
    def __init__(self, result: Any = None, exc: Exception | None = None) -> None:
        self.result = result
        self.exc = exc
        self.calls: list[dict[str, Any]] = []

    def search_items(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return self.result


def make_client(api: FakeApi) -> amazon_client.AmazonClient:
    return amazon_client.AmazonClient("id", "secret", "3.2", "tag-21", ApiSourceConfig(), api=api)


def test_search_liefert_offers_und_fordert_ressourcen_an() -> None:
    api = FakeApi(result=NS(items=[amazon_item(), amazon_item(listings=[])]))
    offers = make_client(api).search("BRITA")
    assert len(offers) == 1  # zweites Item ohne Angebot verworfen
    call = api.calls[0]
    assert call["keywords"] == "BRITA"
    assert call["item_count"] == 10
    names = {r.name for r in call["resources"]}
    assert "OFFERS_V2_DOT_LISTINGS_DOT_PRICE" in names
    assert "ITEM_INFO_DOT_EXTERNAL_IDS" in names


def test_keine_treffer_ist_leere_liste() -> None:
    from amazon_creatorsapi import errors

    offers = make_client(FakeApi(exc=errors.ItemsNotFoundError("none"))).search("xyz")
    assert offers == []


@pytest.mark.parametrize("name", ["AccessDeniedError", "AuthenticationError", "AssociateValidationError"])
def test_zugangsfehler_sind_fatal(name: str) -> None:
    from amazon_creatorsapi import errors

    with pytest.raises(amazon_client.AmazonError) as exc:
        make_client(FakeApi(exc=getattr(errors, name)("denied"))).search("x")
    assert exc.value.fatal is True
    assert "10 qualifizierten" in str(exc.value)


def test_rate_limit_ist_nicht_fatal() -> None:
    from amazon_creatorsapi import errors

    with pytest.raises(amazon_client.AmazonError) as exc:
        make_client(FakeApi(exc=errors.TooManyRequestsError("slow down"))).search("x")
    assert exc.value.fatal is False


def test_konstruktion_ohne_netzwerk_mit_echter_bibliothek() -> None:
    """Der echte Client darf beim Anlegen nicht ins Netz gehen -- sonst
    scheitert schon build_source() bei kurzem Verbindungsausfall."""
    client = amazon_client.AmazonClient("id", "secret", "3.2", "tag-21", ApiSourceConfig())
    assert client._api.marketplace == "www.amazon.de"
