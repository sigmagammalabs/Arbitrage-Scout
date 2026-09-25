"""Gemeinsame Datenmodelle.

Bewusst in einem eigenen Modul, damit ``calculator``, ``gemini_matcher``,
``sources`` und ``scout`` dieselben Strukturen teilen, ohne sich gegenseitig zu
importieren.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, HttpUrl, field_validator


class Marketplace(str, Enum):
    AMAZON = "amazon"
    ALIEXPRESS = "aliexpress"
    EBAY = "ebay"
    OTHER = "other"


class Condition(str, Enum):
    NEW = "new"
    USED = "used"
    REFURBISHED = "refurbished"
    UNKNOWN = "unknown"


class Offer(BaseModel):
    """Ein einzelnes Angebot auf einem Marktplatz.

    ``price_eur`` ist immer der Bruttopreis in Euro ohne Versand; Versand steht
    getrennt in ``shipping_eur``, weil eBay die Provision auf die Summe aus
    beidem berechnet.
    """

    offer_id: str
    marketplace: Marketplace
    title: str
    price_eur: float = Field(ge=0.0)
    shipping_eur: float = Field(default=0.0, ge=0.0)
    url: HttpUrl | None = None

    brand: str | None = None
    model_number: str | None = None
    ean: str | None = None
    asin: str | None = None

    # Wie viele Stueck enthaelt *dieses* Angebot laut Titel/Beschreibung?
    # ``None`` = unbekannt, wird dann von Gemini bestimmt.
    package_quantity: int | None = Field(default=None, ge=1)

    condition: Condition = Condition.UNKNOWN
    rating: float | None = Field(default=None, ge=0.0, le=5.0)
    review_count: int | None = Field(default=None, ge=0)
    seller: str | None = None
    description: str | None = None
    attributes: dict[str, str] = Field(default_factory=dict)
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {"populate_by_name": True}

    @field_validator("title")
    @classmethod
    def _title_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Angebotstitel darf nicht leer sein.")
        return v

    @property
    def landed_price_eur(self) -> float:
        """Preis inklusive Versand -- das, was tatsaechlich fliesst."""
        return round(self.price_eur + self.shipping_eur, 2)

    def short(self) -> str:
        return f"[{self.marketplace.value}] {self.title[:70]} @ {self.landed_price_eur:.2f} EUR"


class CandidatePair(BaseModel):
    """Ein zu pruefendes Paar: wo gekauft, wo verkauft wird."""

    source: Offer  # Einkauf (Amazon / AliExpress)
    target: Offer  # Verkauf (eBay)
    category: str = "uncategorized"
    keyword: str | None = None

    @property
    def pair_id(self) -> str:
        return f"{self.source.offer_id}->{self.target.offer_id}"
