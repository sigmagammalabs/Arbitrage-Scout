"""Tests des Gemini-Backends -- ohne Netzwerk und ohne API-Key.

Gemini liefert bei Structured Outputs ein fertig geparstes Objekt. Verlaesst
man sich blind darauf, bricht der Lauf, sobald eine Antwort blockiert oder
abgeschnitten wird -- genau diese Randfaelle stehen hier.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import GeminiConfig  # noqa: E402
from llm_providers import GEMINI_AVAILABLE, GeminiProvider, LLMError  # noqa: E402
from matcher import ProductMatchResult  # noqa: E402

pytestmark = pytest.mark.skipif(
    not GEMINI_AVAILABLE, reason="google-genai nicht installiert"
)


class _Feedback:
    def __init__(self, block_reason: str | None) -> None:
        self.block_reason = block_reason


class _Candidate:
    def __init__(self, finish_reason: str | None) -> None:
        self.finish_reason = finish_reason


class _Response:
    def __init__(
        self,
        parsed: Any = None,
        text: str | None = None,
        block_reason: str | None = None,
        finish_reason: str | None = None,
    ) -> None:
        self.parsed = parsed
        self.text = text
        self.prompt_feedback = _Feedback(block_reason) if block_reason else None
        self.candidates = [_Candidate(finish_reason)] if finish_reason else []


def make_provider(**cfg: Any) -> GeminiProvider:
    provider = GeminiProvider.__new__(GeminiProvider)  # __init__ umgehen: kein Key noetig
    provider.config = GeminiConfig(**cfg)
    provider.name = "gemini"
    provider.supports_native_schema = True
    provider._client = None
    return provider


def test_geparstes_objekt_wird_direkt_uebernommen() -> None:
    provider = make_provider()
    expected = ProductMatchResult(is_match=True, confidence=0.9)
    assert provider._parse_response(_Response(parsed=expected), ProductMatchResult) is expected


def test_geparstes_dict_wird_validiert() -> None:
    provider = make_provider()
    payload = {"is_match": False, "confidence": 0.4, "mismatch_reason": "andere Variante"}
    result = provider._parse_response(_Response(parsed=payload), ProductMatchResult)
    assert result.is_match is False
    assert result.mismatch_reason == "andere Variante"


def test_rohtext_wird_nachverarbeitet() -> None:
    """Faellt `parsed` aus, muss der Rohtext den Lauf noch retten."""
    provider = make_provider()
    raw = '```json\n{"is_match": true, "confidence": 0.88}\n```'
    result = provider._parse_response(_Response(text=raw), ProductMatchResult)
    assert result.confidence == pytest.approx(0.88)


def test_blockierte_antwort_nennt_den_grund() -> None:
    provider = make_provider()
    with pytest.raises(LLMError) as exc:
        provider._parse_response(_Response(text="", block_reason="SAFETY"), ProductMatchResult)
    assert "SAFETY" in str(exc.value)
    assert exc.value.retryable is True


def test_abgeschnittene_antwort_nennt_finish_reason() -> None:
    provider = make_provider()
    with pytest.raises(LLMError) as exc:
        provider._parse_response(
            _Response(text="", finish_reason="MAX_TOKENS"), ProductMatchResult
        )
    assert "MAX_TOKENS" in str(exc.value)


def test_generation_config_traegt_das_schema() -> None:
    provider = make_provider()
    cfg = provider._generation_config("systemtext", ProductMatchResult)
    assert cfg.response_mime_type == "application/json"
    assert cfg.response_schema is ProductMatchResult
    assert cfg.system_instruction == "systemtext"
    assert cfg.temperature == 0.0


def test_retry_klassifikation() -> None:
    from google.genai import errors

    provider = make_provider()

    def as_error(cls: type, code: int) -> Exception:
        err = cls.__new__(cls)
        Exception.__init__(err, f"HTTP {code}")
        err.code = code
        return err

    assert provider.is_retryable(as_error(errors.ServerError, 503)) is True
    assert provider.is_retryable(as_error(errors.ClientError, 429)) is True
    assert provider.is_retryable(as_error(errors.ClientError, 400)) is False
    assert provider.is_retryable(TimeoutError("deadline exceeded")) is True
    assert provider.is_retryable(ValueError("invalid argument")) is False
