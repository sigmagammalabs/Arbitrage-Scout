"""Tests fuer den Matcher -- ohne echte API-Aufrufe.

Geprueft werden Schema-Robustheit, Prompt-Aufbau, Antwort-Parsing und die
Offline-Heuristik. Der Netzwerkpfad wird mit einem Dummy-Client simuliert.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import GeminiConfig  # noqa: E402
from gemini_matcher import (  # noqa: E402
    GeminiMatcher,
    MatcherError,
    ProductMatchResult,
    build_prompt,
)
from models import CandidatePair, Condition, Marketplace, Offer  # noqa: E402


def make_pair(
    source_title: str = "BRITA Maxtra+ Filterkartuschen 6er Pack",
    target_title: str = "BRITA Maxtra Plus Filterkartusche 1 Stueck",
    **kwargs: object,
) -> CandidatePair:
    return CandidatePair(
        source=Offer(
            offer_id="s1",
            marketplace=Marketplace.AMAZON,
            title=source_title,
            price_eur=24.99,
            brand=kwargs.get("source_brand"),  # type: ignore[arg-type]
            ean=kwargs.get("source_ean"),  # type: ignore[arg-type]
            condition=Condition.NEW,
        ),
        target=Offer(
            offer_id="t1",
            marketplace=Marketplace.EBAY,
            title=target_title,
            price_eur=11.90,
            shipping_eur=3.99,
            brand=kwargs.get("target_brand"),  # type: ignore[arg-type]
            ean=kwargs.get("target_ean"),  # type: ignore[arg-type]
            condition=Condition.NEW,
        ),
        category="haushalt",
    )


@pytest.fixture
def offline_matcher() -> GeminiMatcher:
    return GeminiMatcher(api_key=None, config=GeminiConfig(), offline=True)


# --- Schema ----------------------------------------------------------------
def test_confidence_in_prozent_wird_normiert() -> None:
    r = ProductMatchResult(is_match=True, confidence=85)
    assert r.confidence == pytest.approx(0.85)


def test_confidence_wird_begrenzt() -> None:
    assert ProductMatchResult(is_match=True, confidence=1.7).confidence == 1.0
    assert ProductMatchResult(is_match=True, confidence=-3).confidence == 0.0


def test_unsinnige_menge_faellt_auf_eins_zurueck() -> None:
    r = ProductMatchResult(is_match=True, confidence=0.9, package_quantity_source=0)
    assert r.package_quantity_source == 1


def test_gebindeabweichung_wird_erkannt() -> None:
    r = ProductMatchResult(
        is_match=True, confidence=0.9, package_quantity_source=6, package_quantity_target=1
    )
    assert r.quantity_mismatch is True


# --- Prompt ----------------------------------------------------------------
def test_prompt_enthaelt_beide_angebote_und_regeln() -> None:
    prompt = build_prompt(make_pair())
    assert "BRITA Maxtra+ Filterkartuschen 6er Pack" in prompt
    assert "BRITA Maxtra Plus Filterkartusche 1 Stueck" in prompt
    assert "MENGE / GEBINDE" in prompt
    assert "MODELLVARIANTE" in prompt


# --- Antwort-Parsing -------------------------------------------------------
class _Response:
    def __init__(self, parsed: object = None, text: str | None = None) -> None:
        self.parsed = parsed
        self.text = text
        self.prompt_feedback = None
        self.candidates = []


def test_parsed_objekt_wird_direkt_uebernommen(offline_matcher: GeminiMatcher) -> None:
    expected = ProductMatchResult(is_match=True, confidence=0.9)
    assert offline_matcher._parse_response(_Response(parsed=expected)) is expected


def test_markdown_umhuellte_antwort_wird_gelesen(offline_matcher: GeminiMatcher) -> None:
    raw = '```json\n{"is_match": true, "confidence": 0.91}\n```'
    result = offline_matcher._parse_response(_Response(text=raw))
    assert result.is_match is True
    assert result.confidence == pytest.approx(0.91)


def test_json_mit_umgebendem_text_wird_gelesen(offline_matcher: GeminiMatcher) -> None:
    raw = 'Hier das Ergebnis: {"is_match": false, "confidence": 0.2} -- Ende.'
    result = offline_matcher._parse_response(_Response(text=raw))
    assert result.is_match is False


def test_leere_antwort_wirft(offline_matcher: GeminiMatcher) -> None:
    with pytest.raises(MatcherError):
        offline_matcher._parse_response(_Response(text=""))


def test_antwort_ohne_json_wirft(offline_matcher: GeminiMatcher) -> None:
    with pytest.raises(MatcherError):
        offline_matcher._parse_response(_Response(text="Kann ich nicht beantworten."))


# --- Entscheidungslogik ----------------------------------------------------
def test_zu_geringe_confidence_wird_abgelehnt() -> None:
    matcher = GeminiMatcher(
        api_key=None, config=GeminiConfig(min_confidence=0.8), offline=True
    )
    decision = matcher._decide(ProductMatchResult(is_match=True, confidence=0.5))
    assert decision.accepted is False
    assert "Confidence" in decision.reason


def test_gebindehinweis_landet_in_der_begruendung(offline_matcher: GeminiMatcher) -> None:
    decision = offline_matcher._decide(
        ProductMatchResult(
            is_match=True,
            confidence=0.95,
            package_quantity_source=6,
            package_quantity_target=1,
        )
    )
    assert decision.accepted is True
    assert "Gebinde abweichend" in decision.reason


# --- Offline-Heuristik -----------------------------------------------------
def test_heuristik_erkennt_gebindegroesse(offline_matcher: GeminiMatcher) -> None:
    decision = offline_matcher.match(make_pair())
    assert decision.result.package_quantity_source == 6
    assert decision.result.package_quantity_target == 1
    assert decision.offline is True


def test_heuristik_lehnt_widerspruechliche_ean_ab(offline_matcher: GeminiMatcher) -> None:
    pair = make_pair(source_ean="4006387079321", target_ean="1234567890123")
    decision = offline_matcher.match(pair)
    assert decision.accepted is False
    assert "EAN" in (decision.result.mismatch_reason or "")


def test_gleiche_ean_ergibt_hohe_confidence(offline_matcher: GeminiMatcher) -> None:
    pair = make_pair(source_ean="4006387079321", target_ean="4006387079321")
    decision = offline_matcher.match(pair)
    assert decision.result.confidence >= 0.8
    assert decision.accepted is True


def test_cache_verhindert_zweiten_aufruf(offline_matcher: GeminiMatcher) -> None:
    pair = make_pair()
    offline_matcher.match(pair)
    offline_matcher.match(pair)
    assert offline_matcher.stats["cache_hits"] == 1
    assert offline_matcher.stats["offline_calls"] == 1


# --- Fehlerklassifikation --------------------------------------------------
def test_netzwerkfehler_gilt_als_wiederholbar(offline_matcher: GeminiMatcher) -> None:
    assert offline_matcher._is_retryable(TimeoutError("timed out")) is True
    assert offline_matcher._is_retryable(ConnectionError("connection reset")) is True
    assert offline_matcher._is_retryable(RuntimeError("429 rate limit exceeded")) is True


def test_fachlicher_fehler_gilt_nicht_als_wiederholbar(offline_matcher: GeminiMatcher) -> None:
    assert offline_matcher._is_retryable(ValueError("invalid argument")) is False


def test_ohne_sdk_und_ohne_key_kein_online_matcher() -> None:
    with pytest.raises(MatcherError):
        GeminiMatcher(api_key=None, config=GeminiConfig(), offline=False)
