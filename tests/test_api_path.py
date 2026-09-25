"""Tests des Online-Pfads mit einem eingeschleusten Fake-Client.

Es wird kein Netzwerk angefasst und kein API-Key gebraucht. Geprueft wird, dass
Retry, Backoff und Abbruchverhalten so greifen, wie sie im Cronjob gebraucht
werden -- dort sieht niemand zu, wenn etwas endlos wiederholt.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import GeminiConfig  # noqa: E402
from gemini_matcher import (  # noqa: E402
    GENAI_AVAILABLE,
    GeminiMatcher,
    MatcherError,
    ProductMatchResult,
)
from models import CandidatePair, Marketplace, Offer  # noqa: E402

pytestmark = pytest.mark.skipif(
    not GENAI_AVAILABLE, reason="google-genai nicht installiert"
)


class _FakeResponse:
    def __init__(self, parsed: Any) -> None:
        self.parsed = parsed
        self.text = None
        self.prompt_feedback = None
        self.candidates = []


class _FakeModels:
    """Spielt eine Folge vorgegebener Antworten oder Ausnahmen ab."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls = 0

    def generate_content(self, **kwargs: Any) -> Any:
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeClient:
    def __init__(self, script: list[Any]) -> None:
        self.models = _FakeModels(script)


def make_matcher(script: list[Any], **cfg: Any) -> GeminiMatcher:
    """Matcher im Online-Modus, aber mit Fake-Client statt echtem SDK."""
    defaults = {"max_retries": 2, "initial_backoff_seconds": 0.01, "max_backoff_seconds": 0.02}
    matcher = GeminiMatcher(
        api_key=None, config=GeminiConfig(**{**defaults, **cfg}), offline=True
    )
    matcher.offline = False  # Online-Pfad erzwingen, Client bleibt der Fake
    matcher._client = _FakeClient(script)
    return matcher


def make_pair() -> CandidatePair:
    return CandidatePair(
        source=Offer(
            offer_id="s", marketplace=Marketplace.AMAZON, title="Testartikel A", price_eur=10.0
        ),
        target=Offer(
            offer_id="t", marketplace=Marketplace.EBAY, title="Testartikel A", price_eur=30.0
        ),
    )


def test_erfolgreicher_aufruf() -> None:
    expected = ProductMatchResult(is_match=True, confidence=0.95)
    matcher = make_matcher([_FakeResponse(expected)])
    decision = matcher.match(make_pair())
    assert decision.accepted is True
    assert matcher._client.models.calls == 1
    assert matcher.stats["api_calls"] == 1


def test_generation_config_traegt_das_schema() -> None:
    matcher = make_matcher([])
    cfg = matcher._generation_config()
    assert cfg.response_mime_type == "application/json"
    assert cfg.response_schema is ProductMatchResult
    assert cfg.temperature == 0.0


def test_rate_limit_wird_wiederholt() -> None:
    """429 ist die haeufigste Stoerung im Dauerbetrieb -- danach muss es weitergehen."""
    ok = ProductMatchResult(is_match=True, confidence=0.9)
    matcher = make_matcher([RuntimeError("429 Too Many Requests"), _FakeResponse(ok)])
    decision = matcher.match(make_pair())
    assert decision.accepted is True
    assert matcher._client.models.calls == 2


def test_timeout_wird_wiederholt() -> None:
    ok = ProductMatchResult(is_match=False, confidence=0.9, mismatch_reason="anderes Modell")
    matcher = make_matcher([TimeoutError("deadline exceeded"), _FakeResponse(ok)])
    assert matcher.match(make_pair()).accepted is False
    assert matcher._client.models.calls == 2


def test_versuche_sind_begrenzt() -> None:
    """Nach max_retries+1 Fehlschlaegen wird aufgegeben, nicht endlos probiert."""
    matcher = make_matcher([RuntimeError("503 unavailable")] * 5, max_retries=2)
    with pytest.raises(MatcherError):
        matcher.match(make_pair())
    assert matcher._client.models.calls == 3
    assert matcher.stats["errors"] == 1


def test_fachlicher_fehler_wird_nicht_wiederholt() -> None:
    """Ein ungueltiges Argument wird beim zweiten Versuch auch nicht gueltig."""
    matcher = make_matcher([ValueError("invalid argument: model not found")] * 3)
    with pytest.raises(MatcherError):
        matcher.match(make_pair())
    assert matcher._client.models.calls == 1


def test_kaputte_antwort_wird_genau_einmal_wiederholt() -> None:
    """Unbrauchbares JSON: ein zweiter Versuch, dann Schluss."""
    matcher = make_matcher([_FakeResponse(None)] * 4)
    with pytest.raises(MatcherError):
        matcher.match(make_pair())
    assert matcher._client.models.calls == 2


def test_match_many_ueberlebt_einzelfehler() -> None:
    """Ein kaputtes Paar darf den Cronlauf nicht beenden."""
    ok = ProductMatchResult(is_match=True, confidence=0.9)
    matcher = make_matcher(
        [RuntimeError("500 internal")] * 3 + [_FakeResponse(ok)], max_retries=2
    )
    pair_a = make_pair()
    pair_b = make_pair()
    pair_b.target.title = "Anderer Artikel B"  # eigener Cache-Schluessel

    results = matcher.match_many([pair_a, pair_b])
    assert len(results) == 2
    assert results[0][1].accepted is False
    assert results[0][1].model == "error"
    assert results[1][1].accepted is True
