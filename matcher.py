"""Semantischer Produktabgleich per LLM.

Der Preisvergleich allein ist wertlos, wenn die beiden Angebote nicht dasselbe
Produkt sind. Genau hier liegt das Geld -- und das Risiko: ein 5er-Pack gegen
ein Einzelstueck oder die 128-GB- gegen die 64-GB-Variante zu rechnen fuehrt
zuverlaessig zum Fehlkauf.

Dieses Modul haelt alles, was vom Backend unabhaengig ist: das Antwortschema,
den Prompt, Rate-Limiting, Wiederholungslogik, Ergebnis-Cache und die
Bewertung des Modellurteils. Der eigentliche Aufruf geht an einen Provider aus
:mod:`llm_providers` -- aktuell Google Gemini oder Groq.

Ohne API-Key faellt der Matcher auf eine Titel-Heuristik zurueck. Die reicht,
um die Pipeline zu pruefen, ausdruecklich nicht fuer Kaufentscheidungen.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import deque
from typing import Any

from pydantic import BaseModel, Field, field_validator

from config import LLMConfig, Settings
from llm_providers import LLMError, LLMProvider, build_provider
from logging_utils import get_logger
from models import CandidatePair, Offer

logger = get_logger(__name__)

_QUANTITY_PATTERN = re.compile(
    r"(\d{1,4})\s*(?:er[- ]?pack|x\b|stk\.?|st(?:ue|ü)ck|pcs|pieces|pack(?:ung)?|set)",
    re.IGNORECASE,
)


class MatcherError(RuntimeError):
    """Der Abgleich konnte nicht durchgefuehrt werden."""


# ---------------------------------------------------------------------------
# Antwortschema (Structured Output)
# ---------------------------------------------------------------------------
class ProductMatchResult(BaseModel):
    """Schema, gegen das das Modell antwortet.

    Feldreihenfolge ist Absicht: Erst die beobachtbaren Fakten (Mengen,
    Attribute), daraus abgeleitet das Urteil. Das gilt besonders fuer Groq im
    reinen JSON-Modus, wo das Schema nur als Prompt-Vorgabe wirkt und die
    Reihenfolge die Generierung tatsaechlich lenkt.
    """

    package_quantity_source: int = Field(
        default=1,
        ge=1,
        description="Anzahl Einzelstueck im Einkaufsangebot (Amazon/AliExpress). 1 wenn Einzelartikel.",
    )
    package_quantity_target: int = Field(
        default=1,
        ge=1,
        description="Anzahl Einzelstueck im Verkaufsangebot (eBay). 1 wenn Einzelartikel.",
    )
    brand_source: str | None = Field(
        default=None, description="Im Einkaufsangebot erkannte Marke, sonst null."
    )
    brand_target: str | None = Field(
        default=None, description="Im Verkaufsangebot erkannte Marke, sonst null."
    )
    model_variant_source: str | None = Field(
        default=None,
        description="Variantenkennzeichen der Quelle (Groesse, Farbe, Kapazitaet, Modellnummer).",
    )
    model_variant_target: str | None = Field(
        default=None, description="Variantenkennzeichen des Ziels."
    )
    key_attributes: list[str] = Field(
        default_factory=list,
        description="Bis zu 6 kaufentscheidende Attribute, die beide Angebote gemeinsam haben.",
    )
    differences: list[str] = Field(
        default_factory=list,
        description="Konkrete Unterschiede zwischen den Angeboten, leer wenn keine.",
    )
    is_match: bool = Field(
        description="true nur, wenn beide Angebote dasselbe physische Produkt in derselben Variante beschreiben."
    )
    confidence: float = Field(
        ge=0.0, le=1.0, description="Sicherheit des Urteils zwischen 0.0 und 1.0."
    )
    mismatch_reason: str | None = Field(
        default=None,
        description="Bei is_match=false: knappe Begruendung. Bei true: null.",
    )

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp(cls, v: Any) -> Any:
        """Modelle liefern gelegentlich 0-100 statt 0-1 oder Werte knapp ausserhalb.

        Ab 2.0 wird eine Prozentangabe unterstellt und durch 100 geteilt. Werte
        zwischen 1.0 und 2.0 sind dagegen ein Ueberschwinger auf der 0-1-Skala
        (eine Confidence von 1,7 % waere sinnfrei) und werden auf 1.0 gekappt.
        """
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.0
        if 2.0 <= f <= 100.0:
            f = f / 100.0
        return min(max(f, 0.0), 1.0)

    @field_validator("package_quantity_source", "package_quantity_target", mode="before")
    @classmethod
    def _quantity_sane(cls, v: Any) -> Any:
        try:
            n = int(v)
        except (TypeError, ValueError):
            return 1
        return max(1, min(n, 10_000))

    @property
    def quantity_mismatch(self) -> bool:
        return self.package_quantity_source != self.package_quantity_target


class MatchDecision(BaseModel):
    """Das Modellergebnis plus die Bewertung durch den Scout."""

    result: ProductMatchResult
    accepted: bool
    reason: str
    from_cache: bool = False
    offline: bool = False
    latency_ms: int = 0
    model: str = ""


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------
SYSTEM_INSTRUCTION = """\
Du bist ein praeziser Produktabgleich-Pruefer im E-Commerce-Arbitragehandel.
Deine Urteile loesen echte Wareneinkaeufe aus. Ein falsch positives Urteil
kostet Geld, ein falsch negatives kostet nur eine Gelegenheit. Entscheide
deshalb im Zweifel immer gegen einen Match.

Antworte ausschliesslich im vorgegebenen JSON-Schema, ohne Fliesstext.
"""

USER_PROMPT_TEMPLATE = """\
Pruefe, ob die beiden folgenden Angebote dasselbe physische Produkt in
derselben Variante beschreiben.

## Angebot A -- EINKAUF ({source_marketplace})
Titel: {source_title}
Preis: {source_price:.2f} EUR (zzgl. {source_shipping:.2f} EUR Versand)
Marke laut Anbieter: {source_brand}
Modell-/Teilenummer: {source_model}
EAN: {source_ean}
Zustand: {source_condition}
Attribute: {source_attributes}
Beschreibung: {source_description}

## Angebot B -- VERKAUF ({target_marketplace})
Titel: {target_title}
Preis: {target_price:.2f} EUR (zzgl. {target_shipping:.2f} EUR Versand)
Marke laut Anbieter: {target_brand}
Modell-/Teilenummer: {target_model}
EAN: {target_ean}
Zustand: {target_condition}
Attribute: {target_attributes}
Beschreibung: {target_description}

## Pruefregeln -- in dieser Reihenfolge abarbeiten

1. MENGE / GEBINDE (haeufigste Fehlerquelle)
   Bestimme fuer jedes Angebot die Anzahl enthaltener Einzelstuecke.
   Hinweise wie "5er-Pack", "3x", "Set of 10", "Doppelpack", "12 Stueck",
   "2-in-1" oder "inkl. 2 Ersatzfiltern" sind mengenrelevant.
   Kein Mengenhinweis bedeutet Menge 1 -- rate nicht.
   Unterschiedliche Mengen sind KEIN Ausschlussgrund; trage sie sauber in
   package_quantity_source und package_quantity_target ein. Der Preisrechner
   normiert das anschliessend selbst.

2. MODELLVARIANTE (zweithaeufigste Fehlerquelle)
   Speichergroesse, Leistung, Laenge, Volumen, Farbe, Groesse, Anschlusstyp,
   Generation, Baujahr, Kompatibilitaetsliste und Modellnummer muessen exakt
   uebereinstimmen. "Pro" ist nicht "Pro Max", "USB-C" ist nicht "USB-A",
   "128 GB" ist nicht "256 GB", 2. Generation ist nicht 3. Generation.
   Jede Abweichung: is_match=false.

3. MARKE UND IDENTITAET
   Markenware gegen No-Name-Nachbau ist kein Match, selbst bei identischer
   Bauform. Widersprechen sich vorhandene EANs oder Modellnummern, ist es
   kein Match. Stimmen sie ueberein, ist das ein starkes Indiz fuer einen Match.

4. LIEFERUMFANG
   Fehlendes Zubehoer, fehlender Akku, fehlendes Netzteil oder ein reines
   Ersatzteil statt des Komplettgeraets: kein Match.

5. ZUSTAND
   Neu gegen gebraucht oder generalueberholt ist kein Match.

## Ausgabe
- is_match nur dann true, wenn Regel 2 bis 5 alle erfuellt sind.
- confidence spiegelt die Belastbarkeit der Datenlage: duenne Titel ohne
  Attribute rechtfertigen keine hohe Sicherheit, auch wenn sie aehnlich klingen.
- mismatch_reason bei false in einem knappen deutschen Satz, sonst null.
- differences listet konkrete Abweichungen, auch wenn is_match=true bleibt.
"""


def guess_package_quantity(title: str) -> int:
    """Gebindegroesse aus dem Titel schaetzen ("6er Pack" -> 6).

    Reine Heuristik als Vorstufe: Sie ersetzt das LLM-Urteil nicht, erlaubt dem
    Scout aber, den Vorfilter mit realistischen Mengen zu rechnen, statt jedes
    Bundle-Angebot vorschnell zu verwerfen.
    """
    match = _QUANTITY_PATTERN.search(title or "")
    if not match:
        return 1
    try:
        return max(1, min(int(match.group(1)), 10_000))
    except ValueError:
        return 1


def _fmt(value: Any, fallback: str = "nicht angegeben") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text or fallback


def _fmt_attributes(attributes: dict[str, str]) -> str:
    if not attributes:
        return "keine strukturierten Attribute vorhanden"
    return "; ".join(f"{k}={v}" for k, v in list(attributes.items())[:20])


def _truncate(text: str | None, limit: int = 800) -> str:
    if not text:
        return "keine Beschreibung vorhanden"
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + " [...]"


def build_prompt(pair: CandidatePair) -> str:
    """Prompt aus einem Angebotspaar erzeugen."""
    s, t = pair.source, pair.target
    return USER_PROMPT_TEMPLATE.format(
        source_marketplace=s.marketplace.value,
        source_title=s.title,
        source_price=s.price_eur,
        source_shipping=s.shipping_eur,
        source_brand=_fmt(s.brand),
        source_model=_fmt(s.model_number),
        source_ean=_fmt(s.ean),
        source_condition=s.condition.value,
        source_attributes=_fmt_attributes(s.attributes),
        source_description=_truncate(s.description),
        target_marketplace=t.marketplace.value,
        target_title=t.title,
        target_price=t.price_eur,
        target_shipping=t.shipping_eur,
        target_brand=_fmt(t.brand),
        target_model=_fmt(t.model_number),
        target_ean=_fmt(t.ean),
        target_condition=t.condition.value,
        target_attributes=_fmt_attributes(t.attributes),
        target_description=_truncate(t.description),
    )


# ---------------------------------------------------------------------------
# Rate-Limiting
# ---------------------------------------------------------------------------
class RateLimiter:
    """Gleitendes Zeitfenster: hoechstens N Aufrufe pro Minute.

    Bremst vor dem Aufruf, statt sich auf 429-Antworten zu verlassen -- das
    schont das Kontingent und macht Cron-Laeufe berechenbar. Bei Groq ist das
    kein Luxus: der kostenlose Tarif bremst schon bei wenigen Anfragen.
    """

    def __init__(self, max_per_minute: int) -> None:
        self.max_per_minute = max(1, max_per_minute)
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            while self._calls and now - self._calls[0] > 60.0:
                self._calls.popleft()
            if len(self._calls) >= self.max_per_minute:
                sleep_for = 60.0 - (now - self._calls[0]) + 0.05
                if sleep_for > 0:
                    logger.debug("Rate-Limit erreicht, warte %.1f s", sleep_for)
                    time.sleep(sleep_for)
                    now = time.monotonic()
                    while self._calls and now - self._calls[0] > 60.0:
                        self._calls.popleft()
            self._calls.append(time.monotonic())


# ---------------------------------------------------------------------------
# Matcher
# ---------------------------------------------------------------------------
class ProductMatcher:
    """Kapselt saemtliche LLM-Aufrufe des Scouts.

    Ohne Provider (``provider=None``) arbeitet der Matcher offline: eine
    heuristische Einschaetzung aus Titelvergleich und Mengenerkennung. Gut
    genug, um die Pipeline zu testen, nicht fuer echte Kaufentscheidungen.
    """

    def __init__(
        self,
        provider: LLMProvider | None,
        llm_config: LLMConfig | None = None,
        *,
        cache_enabled: bool = True,
    ) -> None:
        self.provider = provider
        self.llm = llm_config or LLMConfig()
        self.offline = provider is None
        self._cache: dict[str, ProductMatchResult] = {}
        self._cache_enabled = cache_enabled
        self._limiter = (
            RateLimiter(provider.config.requests_per_minute) if provider else None
        )
        self.stats = {"api_calls": 0, "cache_hits": 0, "errors": 0, "offline_calls": 0}

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        offline: bool = False,
        provider_name: str | None = None,
    ) -> ProductMatcher:
        """Matcher aus den Settings bauen.

        Fehlt der API-Key des gewaehlten Providers, wird automatisch offline
        gearbeitet -- der Lauf bricht nicht ab, die Ergebnisse sind aber als
        heuristisch markiert.
        """
        name = (provider_name or settings.config.llm.provider).strip().lower()

        if not offline and not settings.secrets.has_key(name):
            logger.warning(
                "Kein API-Key fuer Provider '%s' gefunden - Matcher laeuft heuristisch (offline).",
                name,
            )
            offline = True

        if offline:
            return cls(None, settings.config.llm)

        try:
            provider = build_provider(settings, name)
        except LLMError as exc:
            raise MatcherError(str(exc)) from exc

        logger.info("LLM-Backend: %s", provider.describe())
        return cls(provider, settings.config.llm)

    # -- oeffentliche API -----------------------------------------------------
    def match(self, pair: CandidatePair) -> MatchDecision:
        """Ein Angebotspaar pruefen und eine Entscheidung zurueckgeben."""
        cache_key = self._cache_key(pair)
        if self._cache_enabled and cache_key in self._cache:
            self.stats["cache_hits"] += 1
            return self._decide(self._cache[cache_key], from_cache=True)

        started = time.monotonic()
        if self.offline:
            self.stats["offline_calls"] += 1
            result = self._heuristic_match(pair)
        else:
            result = self._call_provider(build_prompt(pair))

        if self._cache_enabled:
            self._cache[cache_key] = result

        latency = int((time.monotonic() - started) * 1000)
        return self._decide(result, latency_ms=latency)

    def match_many(self, pairs: list[CandidatePair]) -> list[tuple[CandidatePair, MatchDecision]]:
        """Mehrere Paare nacheinander pruefen.

        Bewusst sequenziell: Das Rate-Limit ist die Engstelle, nicht die CPU.
        Fehler bei einem Paar beenden den Lauf nicht.
        """
        out: list[tuple[CandidatePair, MatchDecision]] = []
        for pair in pairs:
            try:
                out.append((pair, self.match(pair)))
            except MatcherError as exc:
                self.stats["errors"] += 1
                logger.error("Abgleich fehlgeschlagen fuer %s: %s", pair.pair_id, exc)
                out.append((pair, self._error_decision(str(exc))))
        return out

    # -- Provider-Aufruf ------------------------------------------------------
    def _call_provider(self, prompt: str) -> ProductMatchResult:
        """Provider aufrufen, mit Rate-Limit und exponentiellem Backoff.

        Die Wiederholungslogik liegt bewusst hier und nicht im SDK: So gilt fuer
        jedes Backend dasselbe Verhalten und jeder Versuch steht im Log.
        """
        assert self.provider is not None  # durch self.offline ausgeschlossen
        retry = self.provider.config
        delay = retry.initial_backoff_seconds
        last_error: Exception | None = None
        schema_retry_used = False

        for attempt in range(1, retry.max_retries + 2):
            if self._limiter is not None:
                self._limiter.acquire()
            try:
                self.stats["api_calls"] += 1
                return self.provider.complete_json(
                    SYSTEM_INSTRUCTION, prompt, ProductMatchResult
                )
            except Exception as exc:
                last_error = exc
                if not self.provider.is_retryable(exc):
                    # Unbrauchbare Antwort einmal neu anfragen -- kleine Modelle
                    # verhaspeln sich gelegentlich beim JSON. Alles andere
                    # (ungueltiges Argument, unbekanntes Modell) bleibt endgueltig.
                    if isinstance(exc, LLMError) and not schema_retry_used:
                        schema_retry_used = True
                        logger.warning("Antwort unbrauchbar (Versuch %d): %s", attempt, exc)
                        continue
                    self.stats["errors"] += 1
                    raise MatcherError(f"{self.provider.name}-Aufruf fehlgeschlagen: {exc}") from exc

                logger.warning(
                    "%s voruebergehend nicht verfuegbar (Versuch %d/%d): %s",
                    self.provider.name,
                    attempt,
                    retry.max_retries + 1,
                    exc,
                )

            if attempt <= retry.max_retries:
                sleep_for = min(delay, retry.max_backoff_seconds)
                logger.debug("Warte %.1f s vor erneutem Versuch", sleep_for)
                time.sleep(sleep_for)
                delay *= 2

        self.stats["errors"] += 1
        raise MatcherError(
            f"{self.provider.name} nach {retry.max_retries + 1} Versuchen "
            f"nicht erreichbar: {last_error}"
        )

    # -- Bewertung ------------------------------------------------------------
    def _decide(
        self,
        result: ProductMatchResult,
        *,
        from_cache: bool = False,
        latency_ms: int = 0,
    ) -> MatchDecision:
        """Rohes Modellurteil gegen die Mindestsicherheit pruefen."""
        threshold = self.llm.min_confidence
        if not result.is_match:
            accepted, reason = False, result.mismatch_reason or "Kein Produktmatch."
        elif result.confidence < threshold:
            accepted = False
            reason = f"Confidence {result.confidence:.2f} unter Schwelle {threshold:.2f}."
        else:
            accepted = True
            reason = f"Match bestaetigt (Confidence {result.confidence:.2f})."
            if result.quantity_mismatch:
                reason += (
                    f" Gebinde abweichend: {result.package_quantity_source} zu "
                    f"{result.package_quantity_target} Stueck -- Preis wird normiert."
                )
        return MatchDecision(
            result=result,
            accepted=accepted,
            reason=reason,
            from_cache=from_cache,
            offline=self.offline,
            latency_ms=latency_ms,
            model=self.model_label,
        )

    @property
    def model_label(self) -> str:
        if self.provider is None:
            return "heuristic"
        return f"{self.provider.name}/{self.provider.model}"

    def _error_decision(self, message: str) -> MatchDecision:
        return MatchDecision(
            result=ProductMatchResult(
                is_match=False, confidence=0.0, mismatch_reason=message
            ),
            accepted=False,
            reason=f"Technischer Fehler: {message}",
            offline=self.offline,
            model="error",
        )

    # -- Offline-Heuristik ----------------------------------------------------
    def _heuristic_match(self, pair: CandidatePair) -> ProductMatchResult:
        """Grober Titelvergleich ohne API. Nur fuer Testlaeufe."""
        qty_source = pair.source.package_quantity or self._guess_quantity(pair.source)
        qty_target = pair.target.package_quantity or self._guess_quantity(pair.target)

        tokens_a = self._tokens(pair.source.title)
        tokens_b = self._tokens(pair.target.title)
        overlap = len(tokens_a & tokens_b)
        union = len(tokens_a | tokens_b) or 1
        similarity = overlap / union

        ean_conflict = bool(
            pair.source.ean and pair.target.ean and pair.source.ean != pair.target.ean
        )
        ean_agree = bool(
            pair.source.ean and pair.target.ean and pair.source.ean == pair.target.ean
        )

        is_match = ean_agree or (similarity >= 0.5 and not ean_conflict)
        confidence = 0.9 if ean_agree else round(min(similarity, 0.75), 2)
        reason = None
        if ean_conflict:
            reason = "EANs widersprechen sich."
        elif not is_match:
            reason = f"Titeluebereinstimmung nur {similarity:.0%} (heuristisch, ohne LLM)."

        return ProductMatchResult(
            package_quantity_source=qty_source,
            package_quantity_target=qty_target,
            brand_source=pair.source.brand,
            brand_target=pair.target.brand,
            model_variant_source=pair.source.model_number,
            model_variant_target=pair.target.model_number,
            key_attributes=sorted(tokens_a & tokens_b)[:6],
            differences=[] if is_match else ["Titel weichen deutlich ab"],
            is_match=is_match,
            confidence=confidence,
            mismatch_reason=reason,
        )

    @staticmethod
    def _guess_quantity(offer: Offer) -> int:
        return guess_package_quantity(offer.title)

    @staticmethod
    def _tokens(title: str) -> set[str]:
        words = re.findall(r"[a-z0-9]+", title.lower())
        return {w for w in words if len(w) > 2}

    # -- Sonstiges ------------------------------------------------------------
    @staticmethod
    def _cache_key(pair: CandidatePair) -> str:
        raw = "|".join(
            [
                pair.source.title,
                str(pair.source.package_quantity),
                pair.source.ean or "",
                pair.target.title,
                str(pair.target.package_quantity),
                pair.target.ean or "",
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def log_stats(self) -> None:
        logger.info(
            "Matcher-Statistik (%s): %d API-Aufrufe, %d Cache-Treffer, %d Offline, %d Fehler",
            self.model_label,
            self.stats["api_calls"],
            self.stats["cache_hits"],
            self.stats["offline_calls"],
            self.stats["errors"],
        )


if __name__ == "__main__":  # Smoke-Test: `python matcher.py`
    from config import load_settings
    from logging_utils import setup_logging
    from models import Marketplace

    settings = load_settings()
    setup_logging(settings)

    demo = CandidatePair(
        source=Offer(
            offer_id="amz-1",
            marketplace=Marketplace.AMAZON,
            title="BRITA Maxtra+ Filterkartuschen 6er Pack fuer Wasserfilter",
            price_eur=24.99,
            brand="BRITA",
        ),
        target=Offer(
            offer_id="ebay-1",
            marketplace=Marketplace.EBAY,
            title="BRITA Maxtra Plus Kartusche 1 Stueck Wasserfilter Ersatzfilter",
            price_eur=9.90,
            shipping_eur=3.99,
            brand="BRITA",
        ),
        category="haushalt",
    )

    matcher = ProductMatcher.from_settings(settings)
    decision = matcher.match(demo)
    print(f"Backend:     {decision.model}")
    print(f"Angenommen:  {decision.accepted}")
    print(f"Begruendung: {decision.reason}")
    print(f"Mengen:      Quelle {decision.result.package_quantity_source} / "
          f"Ziel {decision.result.package_quantity_target}")
    matcher.log_stats()
