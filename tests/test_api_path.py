"""Tests des Online-Pfads mit einem eingeschleusten Fake-Provider.

Es wird kein Netzwerk angefasst und kein API-Key gebraucht. Geprueft wird, dass
Retry, Backoff und Abbruchverhalten so greifen, wie sie im Cronjob gebraucht
werden -- dort sieht niemand zu, wenn etwas endlos wiederholt.

Die Logik liegt im Matcher und gilt damit fuer jedes Backend gleich; dieser
Fake-Provider steht stellvertretend fuer Gemini wie Groq.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import LLMConfig, ProviderConfig  # noqa: E402
from llm_providers import LLMError, LLMProvider  # noqa: E402
from matcher import MatcherError, ProductMatcher, ProductMatchResult  # noqa: E402
from models import CandidatePair, Marketplace, Offer  # noqa: E402


class FakeProvider(LLMProvider):
    """Spielt eine Folge vorgegebener Antworten oder Ausnahmen ab."""

    name = "fake"

    def __init__(self, script: list[Any], config: ProviderConfig) -> None:
        super().__init__(config)
        self.script = list(script)
        self.calls = 0

    def complete_json(self, system: str, user: str, response_model: type[Any]) -> Any:
        self.calls += 1
        if not self.script:
            raise AssertionError("Mehr Aufrufe als vorgesehen")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_matcher(script: list[Any], **cfg: Any) -> ProductMatcher:
    defaults: dict[str, Any] = {
        "model": "fake-1",
        "max_retries": 2,
        "initial_backoff_seconds": 0.01,
        "max_backoff_seconds": 0.02,
        "requests_per_minute": 10_000,
    }
    provider = FakeProvider(script, ProviderConfig(**{**defaults, **cfg}))
    return ProductMatcher(provider, LLMConfig())


def make_pair(target_title: str = "Testartikel A") -> CandidatePair:
    return CandidatePair(
        source=Offer(
            offer_id="s", marketplace=Marketplace.AMAZON, title="Testartikel A", price_eur=10.0
        ),
        target=Offer(
            offer_id="t", marketplace=Marketplace.EBAY, title=target_title, price_eur=30.0
        ),
    )


def test_erfolgreicher_aufruf() -> None:
    expected = ProductMatchResult(is_match=True, confidence=0.95)
    matcher = make_matcher([expected])
    decision = matcher.match(make_pair())
    assert decision.accepted is True
    assert decision.offline is False
    assert decision.model == "fake/fake-1"
    assert matcher.provider.calls == 1
    assert matcher.stats["api_calls"] == 1


def test_rate_limit_wird_wiederholt() -> None:
    """429 ist die haeufigste Stoerung im Dauerbetrieb -- danach muss es weitergehen."""
    ok = ProductMatchResult(is_match=True, confidence=0.9)
    matcher = make_matcher([RuntimeError("429 Too Many Requests"), ok])
    assert matcher.match(make_pair()).accepted is True
    assert matcher.provider.calls == 2


def test_serverfehler_wird_wiederholt() -> None:
    ok = ProductMatchResult(is_match=True, confidence=0.9)
    matcher = make_matcher([RuntimeError("500 internal server error"), ok])
    assert matcher.match(make_pair()).accepted is True
    assert matcher.provider.calls == 2


def test_timeout_wird_wiederholt() -> None:
    ok = ProductMatchResult(is_match=False, confidence=0.9, mismatch_reason="anderes Modell")
    matcher = make_matcher([TimeoutError("deadline exceeded"), ok])
    assert matcher.match(make_pair()).accepted is False
    assert matcher.provider.calls == 2


def test_versuche_sind_begrenzt() -> None:
    """Nach max_retries+1 Fehlschlaegen wird aufgegeben, nicht endlos probiert."""
    matcher = make_matcher([RuntimeError("503 unavailable")] * 5, max_retries=2)
    with pytest.raises(MatcherError):
        matcher.match(make_pair())
    assert matcher.provider.calls == 3
    assert matcher.stats["errors"] == 1


def test_fachlicher_fehler_wird_nicht_wiederholt() -> None:
    """Ein ungueltiges Argument wird beim zweiten Versuch auch nicht gueltig."""
    matcher = make_matcher([ValueError("invalid argument: model not found")] * 3)
    with pytest.raises(MatcherError):
        matcher.match(make_pair())
    assert matcher.provider.calls == 1


def test_kaputte_antwort_wird_genau_einmal_wiederholt() -> None:
    """Unbrauchbares JSON: ein zweiter Versuch, dann Schluss.

    Kleine Modelle verhaspeln sich gelegentlich beim JSON -- einmal nachfragen
    lohnt, beliebig oft nicht.
    """
    broken = LLMError("Antwort passt nicht zum Schema", retryable=False)
    matcher = make_matcher([broken] * 4)
    with pytest.raises(MatcherError):
        matcher.match(make_pair())
    assert matcher.provider.calls == 2


def test_leere_antwort_gilt_als_wiederholbar() -> None:
    ok = ProductMatchResult(is_match=True, confidence=0.9)
    matcher = make_matcher([LLMError("Leere Antwort vom Modell.", retryable=True), ok])
    assert matcher.match(make_pair()).accepted is True
    assert matcher.provider.calls == 2


def test_match_many_ueberlebt_einzelfehler() -> None:
    """Ein kaputtes Paar darf den Cronlauf nicht beenden."""
    ok = ProductMatchResult(is_match=True, confidence=0.9)
    matcher = make_matcher([RuntimeError("500 internal")] * 3 + [ok], max_retries=2)

    results = matcher.match_many([make_pair(), make_pair("Anderer Artikel B")])
    assert len(results) == 2
    assert results[0][1].accepted is False
    assert results[0][1].model == "error"
    assert results[1][1].accepted is True


def test_cache_spart_den_zweiten_aufruf() -> None:
    """Identische Paare duerfen kein zweites Mal Geld kosten."""
    matcher = make_matcher([ProductMatchResult(is_match=True, confidence=0.9)])
    pair = make_pair()
    matcher.match(pair)
    decision = matcher.match(pair)
    assert decision.from_cache is True
    assert matcher.provider.calls == 1
    assert matcher.stats["cache_hits"] == 1
