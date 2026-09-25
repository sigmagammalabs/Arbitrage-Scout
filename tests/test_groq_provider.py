"""Tests des Groq-Backends -- ohne Netzwerk und ohne API-Key.

Im Mittelpunkt stehen die beiden Eigenheiten, die Groq von Gemini unterscheiden:
die Antwort kommt als Chat-Completion statt als geparstes Objekt, und echte
Schema-Erzwingung beherrscht nur ein Teil der Modelle.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import GroqConfig  # noqa: E402
from llm_providers import GROQ_AVAILABLE, GroqProvider, LLMError  # noqa: E402
from matcher import ProductMatchResult  # noqa: E402

pytestmark = pytest.mark.skipif(not GROQ_AVAILABLE, reason="groq nicht installiert")

VALID_JSON = '{"is_match": true, "confidence": 0.93, "package_quantity_source": 6}'


# --- Fakes, die die Form der SDK-Antwort nachbilden -------------------------
class _Message:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str | None, finish_reason: str = "stop") -> None:
        self.message = _Message(content)
        self.finish_reason = finish_reason


class _Completion:
    def __init__(self, content: str | None, finish_reason: str = "stop") -> None:
        self.choices = [_Choice(content, finish_reason)] if content is not None or finish_reason else []


class _FakeCompletions:
    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeClient:
    def __init__(self, script: list[Any]) -> None:
        self.chat = type("Chat", (), {"completions": _FakeCompletions(script)})()


def make_provider(script: list[Any], **cfg: Any) -> GroqProvider:
    provider = GroqProvider.__new__(GroqProvider)  # __init__ umgehen: kein Key noetig
    config = GroqConfig(**cfg)
    provider.config = config
    provider.name = "groq"
    provider.supports_native_schema = config.json_schema_mode
    provider._client = _FakeClient(script)
    provider._schema_mode_failed = False
    return provider


def sent(provider: GroqProvider) -> list[dict[str, Any]]:
    return provider._client.chat.completions.calls


# --- JSON-Modus -------------------------------------------------------------
def test_json_modus_liefert_ergebnis() -> None:
    provider = make_provider([_Completion(VALID_JSON)])
    result = provider.complete_json("sys", "user", ProductMatchResult)
    assert result.is_match is True
    assert result.package_quantity_source == 6
    assert sent(provider)[0]["response_format"] == {"type": "json_object"}


def test_json_modus_traegt_das_schema_in_den_systemprompt() -> None:
    """Ohne serverseitige Erzwingung muss das Schema im Prompt stehen."""
    provider = make_provider([_Completion(VALID_JSON)])
    provider.complete_json("Grundanweisung", "user", ProductMatchResult)
    system = sent(provider)[0]["messages"][0]["content"]
    assert "Grundanweisung" in system
    assert "package_quantity_source" in system
    assert "mismatch_reason" in system


def test_markdown_antwort_wird_gerettet() -> None:
    provider = make_provider([_Completion(f"```json\n{VALID_JSON}\n```")])
    assert provider.complete_json("sys", "user", ProductMatchResult).is_match is True


def test_uebergabeparameter_stammen_aus_der_konfiguration() -> None:
    provider = make_provider(
        [_Completion(VALID_JSON)], model="llama-3.1-8b-instant", max_output_tokens=512
    )
    provider.complete_json("sys", "user", ProductMatchResult)
    call = sent(provider)[0]
    assert call["model"] == "llama-3.1-8b-instant"
    assert call["max_tokens"] == 512
    assert call["temperature"] == 0.0


# --- Schema-Modus -----------------------------------------------------------
def test_schema_modus_schickt_json_schema() -> None:
    provider = make_provider([_Completion(VALID_JSON)], json_schema_mode=True)
    provider.complete_json("sys", "user", ProductMatchResult)
    fmt = sent(provider)[0]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "ProductMatchResult"
    assert "properties" in fmt["json_schema"]["schema"]


def test_modell_ohne_schema_faellt_auf_json_modus_zurueck() -> None:
    """Das eigentliche Sicherheitsnetz: ein Modell ohne json_schema-Unterstuetzung
    darf den Lauf nicht kosten."""
    import groq

    rejection = groq.BadRequestError.__new__(groq.BadRequestError)
    Exception.__init__(rejection, "response_format json_schema is not supported")

    provider = make_provider([rejection, _Completion(VALID_JSON)], json_schema_mode=True)
    result = provider.complete_json("sys", "user", ProductMatchResult)

    assert result.is_match is True
    assert sent(provider)[0]["response_format"]["type"] == "json_schema"
    assert sent(provider)[1]["response_format"] == {"type": "json_object"}
    assert provider._schema_mode_failed is True


def test_echte_stoerung_wird_nicht_als_schema_ablehnung_missdeutet() -> None:
    provider = make_provider([TimeoutError("timed out")], json_schema_mode=True)
    with pytest.raises(TimeoutError):
        provider.complete_json("sys", "user", ProductMatchResult)
    assert provider._schema_mode_failed is False


# --- Fehlerfaelle -----------------------------------------------------------
def test_leerer_inhalt_ist_wiederholbar() -> None:
    provider = make_provider([_Completion("")])
    with pytest.raises(LLMError) as exc:
        provider.complete_json("sys", "user", ProductMatchResult)
    assert exc.value.retryable is True


def test_abgeschnittene_antwort_ist_nicht_wiederholbar() -> None:
    """finish_reason=length heisst: das Token-Budget ist zu klein. Ein erneuter
    Versuch liefert dasselbe abgeschnittene JSON."""
    provider = make_provider([_Completion('{"is_match": tr', finish_reason="length")])
    with pytest.raises(LLMError) as exc:
        provider.complete_json("sys", "user", ProductMatchResult)
    assert exc.value.retryable is False
    assert "abgeschnitten" in str(exc.value)


def test_retry_klassifikation() -> None:
    import groq

    provider = make_provider([])

    def as_error(cls: type, message: str) -> Exception:
        err = cls.__new__(cls)
        Exception.__init__(err, message)
        return err

    assert provider.is_retryable(as_error(groq.RateLimitError, "rate limited")) is True
    assert provider.is_retryable(as_error(groq.InternalServerError, "boom")) is True
    assert provider.is_retryable(as_error(groq.APIConnectionError, "offline")) is True
    assert provider.is_retryable(as_error(groq.BadRequestError, "bad model")) is False
    assert provider.is_retryable(TimeoutError("timed out")) is True
    assert provider.is_retryable(ValueError("invalid argument")) is False
