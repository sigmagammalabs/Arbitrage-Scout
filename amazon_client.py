"""Amazon Creators API: Einkaufsangebote per Stichwortsuche.

Die Creators API ist der Nachfolger der Product Advertising API 5.0, die am
15.05.2026 abgeschaltet wurde (seitdem HTTP 403). Zugang bekommt nur, wer als
Amazon Associate endgueltig freigeschaltet ist und mindestens 10 qualifizierte
Verkaeufe in den letzten 30 Tagen vermittelt hat. Ohne diese Freischaltung
bleibt ``sources.api.purchase_source: csv`` der Weg.

OAuth, Token-Endpunkte je Credential-Version und Marktplatz-Header uebernimmt
das Paket ``python-amazon-paapi`` (Import-Name ``amazon_creatorsapi``).
"""

from __future__ import annotations

from typing import Any

from config import ApiSourceConfig
from logging_utils import get_logger
from models import Condition, Marketplace, Offer

logger = get_logger(__name__)

try:
    from amazon_creatorsapi import AmazonCreatorsApi
    from amazon_creatorsapi import errors as amazon_errors
    from amazon_creatorsapi.models import Availability as AmazonAvailability
    from amazon_creatorsapi.models import Condition as AmazonCondition
    from amazon_creatorsapi.models import SearchItemsResource

    AMAZON_AVAILABLE = True
except ImportError:  # pragma: no cover - haengt an der Installation
    AmazonCreatorsApi = None  # type: ignore[assignment,misc]
    amazon_errors = None  # type: ignore[assignment]
    AMAZON_AVAILABLE = False


class AmazonError(RuntimeError):
    """Suche fehlgeschlagen. ``fatal`` = Zugang oder Konfiguration kaputt."""

    def __init__(self, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal


def _dig(obj: Any, *path: str) -> Any:
    """Verschachtelte Attribute lesen, ``None`` sobald ein Glied fehlt.

    Die API liefert nur die angeforderten Ressourcen; alles andere ist None.
    """
    for name in path:
        if obj is None:
            return None
        obj = getattr(obj, name, None)
    return obj


def _resources() -> list[Any]:
    wanted = (
        "ITEM_INFO_DOT_TITLE",
        "ITEM_INFO_DOT_BY_LINE_INFO",
        "ITEM_INFO_DOT_EXTERNAL_IDS",
        "OFFERS_V2_DOT_LISTINGS_DOT_PRICE",
        "OFFERS_V2_DOT_LISTINGS_DOT_CONDITION",
        "OFFERS_V2_DOT_LISTINGS_DOT_AVAILABILITY",
        "OFFERS_V2_DOT_LISTINGS_DOT_IS_BUY_BOX_WINNER",
    )
    return [getattr(SearchItemsResource, name) for name in wanted]


def parse_item(item: Any) -> Offer | None:
    """Ein Creators-API-Item in ein ``Offer`` uebersetzen.

    Genommen wird das Buy-Box-Angebot -- das, was ein Kaeufer beim Klick auf
    "In den Einkaufswagen" tatsaechlich bezahlt. Fehlt es, das erste Angebot.
    Versandkosten liefert die API nicht; sie werden mit 0 angesetzt (bei Prime
    und Buy-Box-Angeboten meist zutreffend, sonst zu optimistisch).
    """
    listings = _dig(item, "offers_v2", "listings") or []
    listing = next(
        (lst for lst in listings if getattr(lst, "is_buy_box_winner", False)),
        listings[0] if listings else None,
    )
    money = _dig(listing, "price", "money")
    if money is None or getattr(money, "currency", None) != "EUR":
        return None
    try:
        price = float(money.amount)
    except (TypeError, ValueError):
        return None

    title = _dig(item, "item_info", "title", "display_value")
    if not title:
        return None

    eans = _dig(item, "item_info", "external_ids", "eans", "display_values") or []
    condition_value = str(_dig(listing, "condition", "value") or "").lower()
    condition = Condition.NEW if condition_value == "new" else (
        Condition.USED if condition_value == "used" else
        Condition.REFURBISHED if condition_value in ("refurbished", "collectible") else
        Condition.UNKNOWN
    )

    asin = getattr(item, "asin", None) or "?"
    try:
        return Offer(
            offer_id=f"amz-{asin}",
            marketplace=Marketplace.AMAZON,
            title=title,
            price_eur=price,
            shipping_eur=0.0,
            url=getattr(item, "detail_page_url", None),
            brand=_dig(item, "item_info", "by_line_info", "brand", "display_value"),
            ean=eans[0] if eans else None,
            asin=asin if asin != "?" else None,
            condition=condition,
        )
    except Exception as exc:
        logger.debug("Amazon-Item %s verworfen: %s", asin, exc)
        return None


class AmazonClient:
    """Stichwortsuche auf Amazon. ``api`` ist injizierbar fuer Tests."""

    def __init__(
        self,
        credential_id: str,
        credential_secret: str,
        credential_version: str,
        partner_tag: str,
        api_config: ApiSourceConfig,
        *,
        timeout_seconds: float = 20.0,
        api: Any = None,
    ) -> None:
        self.config = api_config
        if api is not None:
            self._api = api
        else:
            if not AMAZON_AVAILABLE:
                raise AmazonError(
                    "Paket 'python-amazon-paapi' fehlt: pip install python-amazon-paapi",
                    fatal=True,
                )
            self._api = AmazonCreatorsApi(
                credential_id=credential_id,
                credential_secret=credential_secret,
                version=credential_version,
                tag=partner_tag,
                country=api_config.amazon_country,
                timeout=(5.0, timeout_seconds),
            )
        self.stats = {"searches": 0, "errors": 0}

    def search(self, keywords: str) -> list[Offer]:
        try:
            result = self._api.search_items(
                keywords=keywords,
                item_count=self.config.amazon_results_per_keyword,
                condition=AmazonCondition.NEW if AMAZON_AVAILABLE else None,
                resources=_resources() if AMAZON_AVAILABLE else None,
                availability=AmazonAvailability.AVAILABLE if AMAZON_AVAILABLE else None,
            )
        except Exception as exc:
            not_found = getattr(amazon_errors, "ItemsNotFoundError", None) if amazon_errors else None
            if not_found is not None and isinstance(exc, not_found):
                self.stats["searches"] += 1
                return []
            raise self._translate(exc) from exc

        self.stats["searches"] += 1
        items = getattr(result, "items", None) or []
        return [o for o in (parse_item(i) for i in items) if o]

    def _translate(self, exc: Exception) -> AmazonError:
        """Bibliotheksfehler auf fatal/nicht fatal abbilden."""
        self.stats["errors"] += 1
        if amazon_errors is not None:
            fatal_types = tuple(
                t for t in (
                    getattr(amazon_errors, "AccessDeniedError", None),
                    getattr(amazon_errors, "AssociateValidationError", None),
                    getattr(amazon_errors, "AuthenticationError", None),
                ) if t is not None
            )
            if fatal_types and isinstance(exc, fatal_types):
                return AmazonError(
                    f"Amazon Creators API verweigert den Zugang: {exc}. Voraussetzung: "
                    "freigeschalteter Associates-Account mit >=10 qualifizierten "
                    "Verkaeufen in 30 Tagen; Credential-Version pruefen.",
                    fatal=True,
                )
        return AmazonError(f"Amazon-Suche fehlgeschlagen: {exc}", fatal=False)
