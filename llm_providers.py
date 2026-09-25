"""Austauschbare LLM-Backends fuer den Produktabgleich.

Der Scout braucht vom Modell genau eine Sache: eine JSON-Antwort, die gegen ein
Pydantic-Schema validiert. Wie sie zustande kommt, ist Sache des Providers.

Unterstuetzt werden:

* ``GeminiProvider`` -- Google Gemini ueber ``google-genai``. Kennt echte
  Structured Outputs: das Pydantic-Modell wird als ``response_schema``
  uebergeben, das SDK liefert ein fertiges Objekt zurueck.
* ``GroqProvider``   -- Groq ueber das ``groq``-SDK (OpenAI-kompatibel).
  Sehr schnell und guenstig, dafuer greift je nach Modell nur der JSON-Modus
  statt einer echten Schema-Erzwingung. Das Schema wandert dann als Beschreibung
  in den Prompt, die Validierung uebernimmt Pydantic.

Die Provider sind bewusst generisch gehalten (``response_model`` als Parameter),
damit dieses Modul nichts ueber Produkte, Angebote oder Margen wissen muss.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any, TypeVar

from pydantic import BaseModel

from config import GeminiConfig, GroqConfig, ProviderConfig, Settings
from logging_utils import get_logger

logger = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)

# HTTP-Codes, bei denen ein erneuter Versuch sinnvoll ist.
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}
_STATUS_IN_TEXT = re.compile(
    r"\b(?:" + "|".join(str(code) for code in sorted(RETRYABLE_STATUS)) + r")\b"
)
_RETRYABLE_TOKENS = (
    "timeout", "timed out", "deadline", "connection", "unavailable",
    "rate limit", "resource_exhausted", "too many requests",
    "internal error", "internal server", "overloaded", "try again",
    "service is busy", "capacity",
)


class LLMError(RuntimeError):
    """Aufruf fehlgeschlagen. ``retryable`` sagt, ob ein neuer Versuch lohnt."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


# ---------------------------------------------------------------------------
# Gemeinsame Hilfsfunktionen
# ---------------------------------------------------------------------------
def extract_json(text: str) -> dict[str, Any] | None:
    """JSON aus Markdown-Fences oder umgebendem Fliesstext herausloesen.

    Nicht jedes Modell haelt sich an "nur JSON" -- vor allem im reinen
    JSON-Objekt-Modus ohne Schema-Erzwingung.
    """
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


def parse_json_payload(text: str, response_model: type[T]) -> T:
    """Rohtext in das Zielschema ueberfuehren, mit Rettungsversuch."""
    text = (text or "").strip()
    if not text:
        raise LLMError("Leere Antwort vom Modell.", retryable=True)

    try:
        return response_model.model_validate_json(text)
    except Exception:
        pass

    data = extract_json(text)
    if data is None:
        raise LLMError(f"Antwort enthaelt kein JSON: {text[:200]!r}")
    try:
        return response_model.model_validate(data)
    except Exception as exc:
        raise LLMError(f"Antwort passt nicht zum Schema: {exc}") from exc


def status_code_of(exc: Exception) -> int | None:
    for attr in ("status_code", "code", "http_status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def looks_retryable(exc: Exception) -> bool:
    """Letzter Ausweg: Fehler ohne brauchbaren Typ anhand von Code und Text einordnen."""
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    code = status_code_of(exc)
    if code is not None:
        return code in RETRYABLE_STATUS
    text = str(exc).lower()
    if _STATUS_IN_TEXT.search(text):
        return True
    return any(token in text for token in _RETRYABLE_TOKENS)


def schema_hint(response_model: type[BaseModel]) -> str:
    """Kompakte Schemabeschreibung fuer Modelle ohne native Schema-Erzwingung."""
    schema = response_model.model_json_schema()
    return json.dumps(schema, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Basisklasse
# ---------------------------------------------------------------------------
class LLMProvider(ABC):
    """Gemeinsame Schnittstelle aller Backends."""

    name: str = "abstract"
    supports_native_schema: bool = False

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    @property
    def model(self) -> str:
        return self.config.model

    @abstractmethod
    def complete_json(self, system: str, user: str, response_model: type[T]) -> T:
        """Prompt schicken und die Antwort als ``response_model`` zurueckgeben."""
        raise NotImplementedError

    def is_retryable(self, exc: Exception) -> bool:
        if isinstance(exc, LLMError):
            return exc.retryable
        return looks_retryable(exc)

    def describe(self) -> str:
        mode = "Schema" if self.supports_native_schema else "JSON-Modus"
        return f"{self.name}/{self.model} ({mode})"


# ---------------------------------------------------------------------------
# Google Gemini
# ---------------------------------------------------------------------------
try:
    from google import genai as _genai
    from google.genai import errors as _genai_errors
    from google.genai import types as _genai_types

    GEMINI_AVAILABLE = True
except ImportError:  # pragma: no cover - haengt an der Installation
    _genai = None  # type: ignore[assignment]
    _genai_errors = None  # type: ignore[assignment]
    _genai_types = None  # type: ignore[assignment]
    GEMINI_AVAILABLE = False


class GeminiProvider(LLMProvider):
    """Google Gemini mit echten Structured Outputs."""

    name = "gemini"
    supports_native_schema = True

    def __init__(self, api_key: str, config: GeminiConfig) -> None:
        super().__init__(config)
        if not GEMINI_AVAILABLE:
            raise LLMError(
                "Paket 'google-genai' fehlt. Installation: pip install google-genai"
            )
        self.config: GeminiConfig = config
        self._client = _genai.Client(api_key=api_key)

    def complete_json(self, system: str, user: str, response_model: type[T]) -> T:
        response = self._client.models.generate_content(
            model=self.config.model,
            contents=user,
            config=self._generation_config(system, response_model),
        )
        return self._parse_response(response, response_model)

    def _generation_config(self, system: str, response_model: type[BaseModel]) -> Any:
        kwargs: dict[str, Any] = {
            "system_instruction": system,
            "temperature": self.config.temperature,
            "max_output_tokens": self.config.max_output_tokens,
            "response_mime_type": "application/json",
            "response_schema": response_model,
        }
        # thinking_config kennt nur ein Teil der Modelle; ein Fehlschlag hier
        # darf den Lauf nicht kosten.
        if self.config.thinking_budget >= 0:
            try:
                kwargs["thinking_config"] = _genai_types.ThinkingConfig(
                    thinking_budget=self.config.thinking_budget
                )
            except Exception:  # pragma: no cover - SDK-/Modellabhaengig
                logger.debug("ThinkingConfig nicht unterstuetzt, wird uebersprungen.")
        try:
            kwargs["http_options"] = _genai_types.HttpOptions(
                timeout=int(self.config.timeout_seconds * 1000)
            )
        except Exception:  # pragma: no cover
            logger.debug("HttpOptions-Timeout nicht unterstuetzt, SDK-Default gilt.")
        return _genai_types.GenerateContentConfig(**kwargs)

    def _parse_response(self, response: Any, response_model: type[T]) -> T:
        """Bevorzugt ``response.parsed``; sonst den Rohtext nachverarbeiten."""
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, response_model):
            return parsed
        if isinstance(parsed, dict):
            return response_model.model_validate(parsed)

        text = (getattr(response, "text", None) or "").strip()
        if not text:
            raise LLMError(
                f"Leere Antwort von Gemini{self._blocking_reason(response)}", retryable=True
            )
        return parse_json_payload(text, response_model)

    @staticmethod
    def _blocking_reason(response: Any) -> str:
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

    def is_retryable(self, exc: Exception) -> bool:
        if isinstance(exc, LLMError):
            return exc.retryable
        if _genai_errors is not None:
            if isinstance(exc, getattr(_genai_errors, "ServerError", ())):
                return True
            for cls_name in ("ClientError", "APIError"):
                cls = getattr(_genai_errors, cls_name, None)
                if cls is not None and isinstance(exc, cls):
                    return int(getattr(exc, "code", 0) or 0) in RETRYABLE_STATUS
        return looks_retryable(exc)


# ---------------------------------------------------------------------------
# Groq
# ---------------------------------------------------------------------------
try:
    import groq as _groq

    GROQ_AVAILABLE = True
except ImportError:  # pragma: no cover - haengt an der Installation
    _groq = None  # type: ignore[assignment]
    GROQ_AVAILABLE = False


class GroqProvider(LLMProvider):
    """Groq ueber die OpenAI-kompatible Chat-Completions-Schnittstelle.

    Zwei Betriebsarten, per ``groq.json_schema_mode`` umschaltbar:

    * ``false`` (Standard) -- ``response_format={"type": "json_object"}``. Das
      funktioniert mit jedem Groq-Modell; die Struktur wird ueber den Prompt
      vorgegeben und von Pydantic validiert.
    * ``true`` -- echtes ``json_schema``. Erzwingt die Struktur, wird aber nur
      von einem Teil der Modelle unterstuetzt. Lehnt das Modell den Aufruf ab,
      faellt der Provider automatisch auf den JSON-Modus zurueck, statt den
      Lauf zu verlieren.
    """

    name = "groq"

    def __init__(self, api_key: str, config: GroqConfig) -> None:
        super().__init__(config)
        if not GROQ_AVAILABLE:
            raise LLMError("Paket 'groq' fehlt. Installation: pip install groq")
        self.config: GroqConfig = config
        self.supports_native_schema = config.json_schema_mode
        # Die Wiederholungslogik liegt im Matcher, damit sie fuer alle Provider
        # gleich aussieht und protokolliert wird -- das SDK soll nicht zusaetzlich
        # im Verborgenen nachfassen.
        self._client = _groq.Groq(
            api_key=api_key,
            timeout=config.timeout_seconds,
            max_retries=0,
        )
        self._schema_mode_failed = False

    def complete_json(self, system: str, user: str, response_model: type[T]) -> T:
        use_schema = self.config.json_schema_mode and not self._schema_mode_failed

        if use_schema:
            try:
                text = self._call(system, user, self._schema_format(response_model))
                return parse_json_payload(text, response_model)
            except Exception as exc:
                if self._is_schema_rejection(exc):
                    # Einmalig merken: ab jetzt laeuft dieser Prozess im JSON-Modus.
                    self._schema_mode_failed = True
                    logger.warning(
                        "Modell %s lehnt json_schema ab (%s) - Umschaltung auf den "
                        "reinen JSON-Modus fuer diesen Lauf.",
                        self.config.model,
                        exc,
                    )
                else:
                    raise

        system_with_schema = (
            f"{system}\n\n"
            "Antworte ausschliesslich mit einem JSON-Objekt nach diesem Schema "
            "(keine Markdown-Fences, kein Fliesstext):\n"
            f"{schema_hint(response_model)}"
        )
        text = self._call(system_with_schema, user, {"type": "json_object"})
        return parse_json_payload(text, response_model)

    def _call(self, system: str, user: str, response_format: dict[str, Any]) -> str:
        completion = self._client.chat.completions.create(
            model=self.config.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=self.config.temperature,
            max_tokens=self.config.max_output_tokens,
            response_format=response_format,
        )
        choices = getattr(completion, "choices", None) or []
        if not choices:
            raise LLMError("Groq lieferte keine Antwortalternative.", retryable=True)

        choice = choices[0]
        finish = getattr(choice, "finish_reason", None)
        content = getattr(getattr(choice, "message", None), "content", None)
        if not content:
            raise LLMError(
                f"Groq lieferte leeren Inhalt (finish_reason={finish}).", retryable=True
            )
        if finish == "length":
            # Abgeschnittenes JSON ist unrettbar; mehr Tokens muessen her.
            raise LLMError(
                "Groq-Antwort wurde abgeschnitten (max_output_tokens zu klein).",
                retryable=False,
            )
        return content

    @staticmethod
    def _schema_format(response_model: type[BaseModel]) -> dict[str, Any]:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": response_model.__name__,
                "schema": response_model.model_json_schema(),
            },
        }

    @staticmethod
    def _is_schema_rejection(exc: Exception) -> bool:
        """Unterscheidet "Modell kann kein json_schema" von echten Stoerungen."""
        if _groq is not None and isinstance(exc, getattr(_groq, "BadRequestError", ())):
            return True
        if status_code_of(exc) in (400, 422):
            return True
        text = str(exc).lower()
        return "json_schema" in text and ("not support" in text or "invalid" in text)

    def is_retryable(self, exc: Exception) -> bool:
        if isinstance(exc, LLMError):
            return exc.retryable
        if _groq is not None:
            for cls_name in ("RateLimitError", "APIConnectionError",
                             "APITimeoutError", "InternalServerError"):
                cls = getattr(_groq, cls_name, None)
                if cls is not None and isinstance(exc, cls):
                    return True
            bad_request = getattr(_groq, "BadRequestError", None)
            if bad_request is not None and isinstance(exc, bad_request):
                return False
            status_error = getattr(_groq, "APIStatusError", None)
            if status_error is not None and isinstance(exc, status_error):
                return (status_code_of(exc) or 0) in RETRYABLE_STATUS
        return looks_retryable(exc)


# ---------------------------------------------------------------------------
# Auswahl
# ---------------------------------------------------------------------------
PROVIDER_NAMES = ("gemini", "groq")


def provider_config(settings: Settings, name: str) -> ProviderConfig:
    if name == "gemini":
        return settings.config.gemini
    if name == "groq":
        return settings.config.groq
    raise LLMError(f"Unbekannter Provider: {name}")


def build_provider(settings: Settings, name: str | None = None) -> LLMProvider:
    """Provider gemaess ``llm.provider`` erzeugen (oder den explizit genannten)."""
    name = (name or settings.config.llm.provider).strip().lower()

    if name == "gemini":
        return GeminiProvider(settings.secrets.require_gemini_key(), settings.config.gemini)
    if name == "groq":
        return GroqProvider(settings.secrets.require_groq_key(), settings.config.groq)
    raise LLMError(
        f"Unbekannter Provider: {name}. Moeglich sind: {', '.join(PROVIDER_NAMES)}"
    )
