"""Semantischer Produktabgleich per Google Gemini.

Der Preisvergleich allein ist wertlos, wenn die beiden Angebote nicht dasselbe
Produkt sind. Genau hier liegt das Geld -- und das Risiko: ein 5er-Pack gegen
ein Einzelstueck oder die 128-GB- gegen die 64-GB-Variante zu rechnen fuehrt
zuverlaessig zum Fehlkauf.

Das Modul nutzt das aktuelle ``google-genai`` SDK mit Structured Outputs, d. h.
Gemini antwortet gegen ein Pydantic-Schema statt in Freitext. Dazu kommen:
Rate-Limiting, exponentielles Backoff bei 429/5xx, ein Ergebnis-Cache und ein
Offline-Modus fuer Testlaeufe ohne API-Kosten.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import deque
from typing import Any

from pydantic import BaseModel, Field, field_validator

from config import GeminiConfig, Settings
from logging_utils import get_logger
from models import CandidatePair, Offer

logger = get_logger(__name__)

# Das SDK ist optional importierbar, damit Kalkulation und Tests auch ohne
# installiertes Paket laufen. Fehlt es, schlaegt erst der erste Aufruf fehl.
try:
    from google import genai
    from google.genai import errors as genai_errors
    from google.genai import types as genai_types

    GENAI_AVAILABLE = True
except ImportError:  # pragma: no cover - haengt an der Installation
    genai = None  # type: ignore[assignment]
    genai_errors = None  # type: ignore[assignment]
    genai_types = None  # type: ignore[assignment]
    GENAI_AVAILABLE = False


# HTTP-Codes, bei denen ein erneuter Versuch sinnvoll ist.
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
_RETRYABLE_STATUS_PATTERN = re.compile(
    r"\b(?:" + "|".join(str(c) for c in sorted(_RETRYABLE_STATUS)) + r")\b"
)
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
    """Schema, gegen das Gemini antwortet.

    Feldreihenfolge ist Absicht: Das Modell fuellt erst die beobachtbaren Fakten
    (Mengen, Attribute) und leitet daraus das Urteil ab.
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
    """Das Gemini-Ergebnis plus die Bewertung durch den Scout."""

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
    schont das Kontingent und macht Cron-Laeufe berechenbar.
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
class GeminiMatcher:
    """Kapselt saemtliche LLM-Aufrufe des Scouts.

    Im Offline-Modus (``offline=True``, z. B. bei ``--dry-run`` ohne API-Key)
    liefert der Matcher eine heuristische Einschaetzung aus Titelvergleich und
    Mengenerkennung -- gut genug, um die Pipeline zu testen, ausdruecklich nicht
    gut genug fuer echte Kaufentscheidungen.
    """

    def __init__(
        self,
        api_key: str | None,
        config: GeminiConfig,
        *,
        offline: bool = False,
        cache_enabled: bool = True,
    ) -> None:
        self.config = config
        self.offline = offline
        self._cache: dict[str, ProductMatchResult] = {}
        self._cache_enabled = cache_enabled
        self._limiter = RateLimiter(config.requests_per_minute)
        self._client: Any = None
        self.stats = {"api_calls": 0, "cache_hits": 0, "errors": 0, "offline_calls": 0}

        if not self.offline:
            if not GENAI_AVAILABLE:
                raise MatcherError(
                    "Paket 'google-genai' fehlt. Installation: pip install google-genai "
                    "-- oder den Matcher mit offline=True betreiben."
                )
            if not api_key:
                raise MatcherError("Kein GEMINI_API_KEY uebergeben.")
            self._client = genai.Client(api_key=api_key)

    @classmethod
    def from_settings(cls, settings: Settings, *, offline: bool = False) -> GeminiMatcher:
        """Matcher aus den Settings bauen.

        Ohne API-Key wird automatisch offline gearbeitet -- der Lauf bricht nicht
        ab, die Ergebnisse sind aber als heuristisch markiert.
        """
        if not offline and not settings.secrets.has_gemini_key:
            logger.warning("Kein GEMINI_API_KEY gefunden - Matcher laeuft heuristisch (offline).")
            offline = True
        api_key = None if offline else settings.secrets.require_gemini_key()
        return cls(api_key=api_key, config=settings.config.gemini, offline=offline)

    # -- oeffentliche API -----------------------------------------------------
    def match(self, pair: CandidatePair) -> MatchDecision:
        """Ein Angebotspaar pruefen und eine Entscheidung zurueckgeben."""
        cache_key = self._cache_key(pair)
        if self._cache_enabled and cache_key in self._cache:
            self.stats["cache_hits"] += 1
            result = self._cache[cache_key]
            return self._decide(result, from_cache=True, offline=self.offline)

        started = time.monotonic()
        if self.offline:
            self.stats["offline_calls"] += 1
            result = self._heuristic_match(pair)
        else:
            result = self._call_api(build_prompt(pair))

        if self._cache_enabled:
            self._cache[cache_key] = result

        latency = int((time.monotonic() - started) * 1000)
        return self._decide(result, latency_ms=latency, offline=self.offline)

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

    # -- API-Aufruf -----------------------------------------------------------
    def _call_api(self, prompt: str) -> ProductMatchResult:
        """Gemini mit Structured Output aufrufen, inklusive Backoff."""
        cfg = self._generation_config()
        delay = self.config.initial_backoff_seconds
        last_error: Exception | None = None

        for attempt in range(1, self.config.max_retries + 2):
            self._limiter.acquire()
            try:
                self.stats["api_calls"] += 1
                response = self._client.models.generate_content(
                    model=self.config.model,
                    contents=prompt,
                    config=cfg,
                )
                return self._parse_response(response)
            except MatcherError as exc:
                # Unbrauchbare Antwort: einmal neu anfragen, danach aufgeben.
                last_error = exc
                logger.warning("Antwort unbrauchbar (Versuch %d): %s", attempt, exc)
                if attempt > 1:
                    break
            except Exception as exc:
                last_error = exc
                if not self._is_retryable(exc):
                    raise MatcherError(f"Gemini-Aufruf fehlgeschlagen: {exc}") from exc
                logger.warning(
                    "Gemini voruebergehend nicht verfuegbar (Versuch %d/%d): %s",
                    attempt,
                    self.config.max_retries + 1,
                    exc,
                )

            if attempt <= self.config.max_retries:
                sleep_for = min(delay, self.config.max_backoff_seconds)
                logger.debug("Warte %.1f s vor erneutem Versuch", sleep_for)
                time.sleep(sleep_for)
                delay *= 2

        self.stats["errors"] += 1
        raise MatcherError(
            f"Gemini nach {self.config.max_retries + 1} Versuchen nicht erreichbar: {last_error}"
        )

    def _generation_config(self) -> Any:
        """``GenerateContentConfig`` inklusive Response-Schema."""
        kwargs: dict[str, Any] = {
            "system_instruction": SYSTEM_INSTRUCTION,
            "temperature": self.config.temperature,
            "max_output_tokens": self.config.max_output_tokens,
            "response_mime_type": "application/json",
            "response_schema": ProductMatchResult,
        }
        # thinking_config kennt nur ein Teil der Modelle; ein Fehlschlag hier darf
        # den Lauf nicht kosten.
        if self.config.thinking_budget >= 0:
            try:
                kwargs["thinking_config"] = genai_types.ThinkingConfig(
                    thinking_budget=self.config.thinking_budget
                )
            except Exception:  # pragma: no cover - SDK-/Modellabhaengig
                logger.debug("ThinkingConfig nicht unterstuetzt, wird uebersprungen.")
        try:
            kwargs["http_options"] = genai_types.HttpOptions(
                timeout=int(self.config.timeout_seconds * 1000)
            )
        except Exception:  # pragma: no cover
            logger.debug("HttpOptions-Timeout nicht unterstuetzt, SDK-Default gilt.")
        return genai_types.GenerateContentConfig(**kwargs)

    def _parse_response(self, response: Any) -> ProductMatchResult:
        """Antwort in das Schema ueberfuehren.

        Der bevorzugte Weg ist ``response.parsed``; faellt das aus (abgeschnittene
        Antwort, in Markdown eingepackt), wird der Rohtext nachverarbeitet.
        """
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, ProductMatchResult):
            return parsed
        if isinstance(parsed, dict):
            return ProductMatchResult.model_validate(parsed)

        text = (getattr(response, "text", None) or "").strip()
        if not text:
            reason = self._blocking_reason(response)
            raise MatcherError(f"Leere Antwort von Gemini{reason}")

        try:
            return ProductMatchResult.model_validate_json(text)
        except Exception:
            pass

        cleaned = self._extract_json(text)
        if cleaned is None:
            raise MatcherError(f"Antwort enthaelt kein JSON: {text[:200]!r}")
        try:
            return ProductMatchResult.model_validate(cleaned)
        except Exception as exc:
            raise MatcherError(f"Antwort passt nicht zum Schema: {exc}") from exc

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any] | None:
        """JSON aus Markdown-Fences oder umgebendem Text herausloesen."""
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        candidate = fence.group(1) if fence else None
        if candidate is None:
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end <= start:
                return None
            candidate = text[start : end + 1]
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _blocking_reason(response: Any) -> str:
        """Sicherheitsfilter o. ae. aus der Antwort auslesen, falls vorhanden."""
        feedback = getattr(response, "prompt_feedback", None)
        reason = getattr(feedback, "block_reason", None)
        if reason:
            return f" (blockiert: {reason})"
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            finish = getattr(candidates[0], "finish_reason", None)
            if finish:
                return f" (finish_reason={finish})"
        return ""

    def _is_retryable(self, exc: Exception) -> bool:
        """Netzwerkfehler und serverseitige Ueberlast sind wiederholbar."""
        if genai_errors is not None:
            if isinstance(exc, getattr(genai_errors, "ServerError", ())):
                return True
            if isinstance(exc, getattr(genai_errors, "ClientError", ())):
                return int(getattr(exc, "code", 0) or 0) in _RETRYABLE_STATUS
            if isinstance(exc, getattr(genai_errors, "APIError", ())):
                return int(getattr(exc, "code", 0) or 0) in _RETRYABLE_STATUS
        if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
            return True
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if isinstance(code, int) and code in _RETRYABLE_STATUS:
            return True

        # Letzter Ausweg: Fehler ohne Typ oder Code anhand des Textes einordnen.
        text = str(exc).lower()
        if _RETRYABLE_STATUS_PATTERN.search(text):
            return True
        return any(
            token in text
            for token in (
                "timeout", "timed out", "deadline", "connection", "unavailable",
                "rate limit", "resource_exhausted", "too many requests",
                "internal error", "internal server", "overloaded", "try again",
            )
        )

    # -- Bewertung ------------------------------------------------------------
    def _decide(
        self,
        result: ProductMatchResult,
        *,
        from_cache: bool = False,
        offline: bool = False,
        latency_ms: int = 0,
    ) -> MatchDecision:
        """Rohes Modellurteil gegen die Mindestsicherheit pruefen."""
        threshold = self.config.min_confidence
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
            offline=offline,
            latency_ms=latency_ms,
            model="heuristic" if offline else self.config.model,
        )

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
            "Matcher-Statistik: %d API-Aufrufe, %d Cache-Treffer, %d Offline, %d Fehler",
            self.stats["api_calls"],
            self.stats["cache_hits"],
            self.stats["offline_calls"],
            self.stats["errors"],
        )


if __name__ == "__main__":  # Smoke-Test: `python gemini_matcher.py`
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

    matcher = GeminiMatcher.from_settings(settings)
    decision = matcher.match(demo)
    print(f"Modell:      {decision.model}")
    print(f"Angenommen:  {decision.accepted}")
    print(f"Begruendung: {decision.reason}")
    print(f"Mengen:      Quelle {decision.result.package_quantity_source} / "
          f"Ziel {decision.result.package_quantity_target}")
    matcher.log_stats()
