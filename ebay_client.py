"""eBay Browse API: aktive Angebote suchen (Verkaufsseite des Scouts).

Nutzt einen Application Token (OAuth Client Credentials) -- dafuer genuegen
App ID und Cert ID aus dem eBay Developer Portal, kein Nutzer-Login.

Was die Browse API liefert und was nicht: nur *aktive* Angebote, keine
tatsaechlich erzielten Verkaufspreise. Der Preis eines aktiven Angebots ist
eine Obergrenze dessen, was sich realistisch erzielen laesst, nicht ein
garantierter Erloes. Verkaufte Angebote liefert nur die Marketplace Insights
API, die eBay nicht allgemein freigibt.
"""

from __future__ import annotations

import base64
import re
import time
from typing import Any
from urllib.parse import quote

import requests

from config import ApiSourceConfig, HttpSourceConfig
from logging_utils import get_logger
from models import Condition, Marketplace, Offer

logger = get_logger(__name__)

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
ITEM_URL = "https://api.ebay.com/buy/browse/v1/item/"
BEST_SELLING_URL = "https://api.ebay.com/buy/marketing/v1_beta/merchandised_product"
SCOPE = "https://api.ebay.com/oauth/api_scope"
# Zusatzberechtigung fuer die Bestseller-Listen. Nicht jedes Keyset hat sie;
# fehlt sie, antwortet eBay beim Token-Abruf mit invalid_scope.
MARKETING_SCOPE = "https://api.ebay.com/oauth/api_scope/buy.marketing"

# eBay-Zustands-IDs: 1000 = Neu. Weitere (1500 "Neu: Sonstige", 3000
# "Gebraucht" ...) bewusst nicht: der Scout kauft neu ein.
CONDITION_NEW_ID = "1000"
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# eBay erlaubt fuer q bis zu 100 Zeichen; lange Amazon-Titel verschlechtern
# die Relevanz zudem eher, als dass sie helfen.
_MAX_QUERY_CHARS = 90


def _map_condition(condition_id: str) -> Condition:
    """eBay-Zustands-ID auf das eigene Modell abbilden.

    1000 Neu; 1500/1750 Neu mit Abweichungen (Verpackung, Maengel) -- bewusst
    UNKNOWN statt NEW, damit der Matcher sie nicht einem originalverpackten
    Einkauf gleichsetzt; 2000-2500 generalueberholt; 2750-6000 gebraucht;
    7000 defekt/Ersatzteil.
    """
    if not condition_id.isdigit():
        return Condition.UNKNOWN
    code = int(condition_id)
    if code == 1000:
        return Condition.NEW
    if 2000 <= code <= 2500:
        return Condition.REFURBISHED
    if 2750 <= code <= 6000:
        return Condition.USED
    return Condition.UNKNOWN


class EbayError(RuntimeError):
    """Suche fehlgeschlagen. ``fatal`` = Konfiguration kaputt, weitere Versuche sinnlos."""

    def __init__(self, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal


class EbayScopeError(EbayError):
    """Dem Keyset fehlt eine Zusatzberechtigung. Nie fatal: die normale Suche
    funktioniert weiter, nur die betroffene Funktion entfaellt."""


def _scope_name(scope: str) -> str:
    return scope.rsplit("/", 1)[-1]


def build_query(offer: Offer) -> str:
    """Suchbegriff aus einem Einkaufsangebot.

    Marke voran (falls nicht schon im Titel), Sonderzeichen raus, auf eine
    sinnvolle Laenge gekuerzt -- an einer Wortgrenze, nicht mitten im Wort.
    """
    title = offer.title
    if offer.brand and offer.brand.lower() not in title.lower():
        title = f"{offer.brand} {title}"
    cleaned = re.sub(r"[^\w\s.+\-/]", " ", title, flags=re.UNICODE)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) <= _MAX_QUERY_CHARS:
        return cleaned
    cut = cleaned[:_MAX_QUERY_CHARS]
    return cut.rsplit(" ", 1)[0] if " " in cut else cut


class EbayClient:
    """Duenner Client fuer ``item_summary/search``.

    ``session`` ist injizierbar, damit Tests ohne Netzwerk laufen.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        api_config: ApiSourceConfig,
        http_config: HttpSourceConfig,
        *,
        session: Any = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self.api = api_config
        self.http = http_config
        self._session = session or requests.Session()
        # Je Berechtigung ein eigener Token: ein gemeinsamer Abruf mit mehreren
        # Scopes scheitert komplett, sobald dem Keyset einer davon fehlt.
        self._tokens: dict[str, tuple[str, float]] = {}
        self.stats = {"searches": 0, "token_refreshes": 0, "errors": 0, "calls": 0}

    # -- OAuth ---------------------------------------------------------------
    def _get_token(self, *, scope: str = SCOPE, force: bool = False) -> str:
        # 60 s Puffer: ein Token, das waehrend der Anfrage ablaeuft, liefert 401.
        cached = self._tokens.get(scope)
        if not force and cached and time.monotonic() < cached[1] - 60:
            return cached[0]

        basic = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode()
        try:
            resp = self._session.post(
                TOKEN_URL,
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"grant_type": "client_credentials", "scope": scope},
                timeout=self.http.timeout_seconds,
            )
        except requests.exceptions.RequestException as exc:
            raise EbayError(f"eBay-Token nicht abrufbar: {exc}") from exc

        if resp.status_code == 400 and "invalid_scope" in resp.text:
            raise EbayScopeError(f"Dem eBay-Keyset fehlt die Berechtigung {_scope_name(scope)}.")
        if resp.status_code in (400, 401):
            raise EbayError(
                f"eBay lehnt die Zugangsdaten ab ({resp.status_code}): {resp.text[:200]}. "
                "EBAY_APP_ID/EBAY_CERT_ID pruefen -- Production-, nicht Sandbox-Keys?",
                fatal=True,
            )
        if resp.status_code != 200:
            raise EbayError(f"eBay-Token: HTTP {resp.status_code}: {resp.text[:200]}")

        payload = resp.json()
        token = payload["access_token"]
        self._tokens[scope] = (token, time.monotonic() + float(payload.get("expires_in", 7200)))
        self.stats["token_refreshes"] += 1
        return token

    # -- Suche ---------------------------------------------------------------
    def _filters(self) -> str:
        parts = [
            "buyingOptions:{FIXED_PRICE}",
            "priceCurrency:EUR",
            "deliveryCountry:DE",
        ]
        if self.api.ebay_new_only:
            parts.append(f"conditionIds:{{{CONDITION_NEW_ID}}}")
        return ",".join(parts)

    def search_raw(
        self,
        *,
        query: str | None = None,
        gtin: str | None = None,
        epid: str | None = None,
        limit: int | None = None,
        sort: str | None = None,
    ) -> list[dict[str, Any]]:
        """Wie :meth:`search`, liefert aber die rohen ``itemSummaries`` --
        die Produktfindung braucht Felder (Kategorie, Erstelldatum), die im
        ``Offer``-Modell keinen Platz haben."""
        if not query and not gtin and not epid:
            raise ValueError("query, gtin oder epid ist erforderlich.")

        params: dict[str, Any] = {
            "limit": limit or self.api.ebay_results_per_offer,
            "filter": self._filters(),
        }
        if gtin:
            params["gtin"] = gtin
        if epid:
            params["epid"] = epid
        if query:
            params["q"] = query
        if sort is None and self.api.ebay_sort == "price":
            sort = "price"
        if sort and sort != "best_match":
            params["sort"] = sort

        data = self._request(SEARCH_URL, params)
        self.stats["searches"] += 1
        return list(data.get("itemSummaries", []) or [])

    def search(self, *, query: str | None = None, gtin: str | None = None, limit: int | None = None) -> list[Offer]:
        """Aktive Festpreis-Angebote suchen. ``gtin`` (EAN) ist praeziser als ``query``."""
        if not query and not gtin:
            raise ValueError("query oder gtin ist erforderlich.")
        raw = self.search_raw(query=query, gtin=gtin, limit=limit)
        return [o for o in (self._parse_item(i) for i in raw) if o]

    def get_item(self, item_id: str) -> dict[str, Any]:
        """Einzelangebot mit Details -- vor allem ``estimatedAvailabilities``
        (geschaetzte Verkaufsmenge), ``gtin``, ``brand``, ``mpn``.

        Kostet einen Aufruf je Angebot. Den Sammelabruf (20 je Aufruf) gibt es
        nur mit der Zusatzberechtigung buy.item.bulk.
        """
        # Die ID hat die Form v1|123|0 -- die Pipes muessen kodiert werden.
        return self._request(ITEM_URL + quote(item_id, safe=""), {})

    def best_selling(self, category_id: str, limit: int = 10) -> list[dict[str, Any]]:
        """eBays Bestseller-Produkte einer Kategorie (Marketing API, Beta).

        Wirft :class:`EbayScopeError`, wenn dem Keyset die Berechtigung fehlt.
        """
        data = self._request(
            BEST_SELLING_URL,
            {"metric_name": "BEST_SELLING", "category_id": category_id, "limit": limit},
            scope=MARKETING_SCOPE,
        )
        return list(data.get("merchandisedProducts", []) or [])

    def _request(self, url: str, params: dict[str, Any], *, scope: str = SCOPE) -> dict[str, Any]:
        refreshed = False
        delay = 2.0
        for attempt in range(1, self.http.max_retries + 2):
            headers = {
                "Authorization": f"Bearer {self._get_token(scope=scope)}",
                "X-EBAY-C-MARKETPLACE-ID": self.api.ebay_marketplace,
                "Accept-Language": "de-DE",
                "User-Agent": self.http.user_agent,
            }
            try:
                self.stats["calls"] += 1
                resp = self._session.get(
                    url, params=params, headers=headers, timeout=self.http.timeout_seconds
                )
            except requests.exceptions.RequestException as exc:
                if attempt > self.http.max_retries:
                    self.stats["errors"] += 1
                    raise EbayError(f"eBay-Suche: Netzwerkfehler: {exc}") from exc
                logger.warning("eBay-Suche: Netzwerkfehler (Versuch %d): %s", attempt, exc)
                time.sleep(delay)
                delay *= 2
                continue

            if resp.status_code == 200:
                return resp.json()

            # Token kann serverseitig vorzeitig verfallen -- einmal erneuern.
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                self._get_token(scope=scope, force=True)
                continue

            if resp.status_code in _RETRYABLE_STATUS and attempt <= self.http.max_retries:
                logger.warning("eBay-Suche: HTTP %s (Versuch %d), neuer Versuch.", resp.status_code, attempt)
                time.sleep(delay)
                delay *= 2
                continue

            self.stats["errors"] += 1
            # 403 mit Zusatz-Scope heisst "diese Funktion ist nicht freigeschaltet",
            # nicht "der Zugang ist kaputt" -- die normale Suche laeuft weiter.
            if resp.status_code == 403 and scope != SCOPE:
                raise EbayScopeError(f"eBay verweigert {_scope_name(scope)}: {resp.text[:160]}")
            raise EbayError(
                f"eBay-Suche: HTTP {resp.status_code}: {resp.text[:200]}",
                fatal=resp.status_code in (401, 403),
            )

        self.stats["errors"] += 1
        raise EbayError("eBay-Suche: Wiederholungen erschoepft.")

    @staticmethod
    def _parse_item(item: dict[str, Any]) -> Offer | None:
        """Ein ``itemSummary`` in ein ``Offer`` uebersetzen. ``None`` bei
        unbrauchbaren Eintraegen (kein Preis, andere Waehrung)."""
        price = item.get("price") or {}
        if price.get("currency") != "EUR":
            return None
        try:
            price_eur = float(price.get("value"))
        except (TypeError, ValueError):
            return None

        shipping = 0.0
        options = item.get("shippingOptions") or []
        if options:
            cost = (options[0] or {}).get("shippingCost") or {}
            if cost.get("currency") in (None, "EUR"):
                try:
                    shipping = float(cost.get("value") or 0.0)
                except (TypeError, ValueError):
                    shipping = 0.0

        condition = _map_condition(str(item.get("conditionId") or ""))

        try:
            return Offer(
                offer_id=f"ebay-{item.get('itemId', '?')}",
                marketplace=Marketplace.EBAY,
                title=item.get("title") or "",
                price_eur=price_eur,
                shipping_eur=shipping,
                url=item.get("itemWebUrl"),
                condition=condition,
                seller=(item.get("seller") or {}).get("username"),
                attributes={"epid": str(item["epid"])} if item.get("epid") else {},
            )
        except Exception as exc:  # leerer Titel, kaputte URL o. ae.
            logger.debug("eBay-Angebot %s verworfen: %s", item.get("itemId"), exc)
            return None
