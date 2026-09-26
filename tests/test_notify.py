"""Tests fuer den Telegram-Versand -- ohne Netzwerk, ohne echten Bot-Token.

``notify.py`` nimmt Ergebnisse duck-typed entgegen (siehe Modul-Docstring dort),
deshalb bauen die Tests hier absichtlich einfache Objekte statt
``scout.ScoutResult`` zu importieren -- das ist der eigentliche Beweis, dass
kein Zirkelbezug zu ``scout.py`` besteht.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import notify  # noqa: E402
from config import AppConfig, Secrets, Settings, load_settings  # noqa: E402


def make_result(**overrides: Any) -> SimpleNamespace:
    defaults = dict(
        is_recommended=True,
        target_title="BRITA Maxtra Plus Filterkartuschen 3er Pack",
        source_marketplace="amazon",
        source_price_eur=39.99,
        target_marketplace="ebay",
        target_price_eur=34.90,
        net_profit_eur=12.28,
        roi_percent=73.6,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def make_settings(**secrets_kwargs: Any) -> Settings:
    """Settings mit frei waehlbaren Secrets, ohne .env von der Platte zu lesen."""
    return Settings(
        config=AppConfig(),
        secrets=Secrets(_env_file=None, **secrets_kwargs),  # type: ignore[call-arg]
        config_path=Path("config.yaml"),
    )


# --- Secrets.has_telegram / require_telegram --------------------------------
def test_has_telegram_braucht_beide_werte() -> None:
    assert make_settings().secrets.has_telegram is False
    assert make_settings(telegram_bot_token="123:abc").secrets.has_telegram is False
    assert make_settings(telegram_chat_id="42").secrets.has_telegram is False
    assert (
        make_settings(telegram_bot_token="123:abc", telegram_chat_id="42").secrets.has_telegram
        is True
    )


def test_require_telegram_wirft_ohne_werte() -> None:
    from config import ConfigError

    with pytest.raises(ConfigError):
        make_settings().secrets.require_telegram()


def test_require_telegram_liefert_klartext() -> None:
    settings = make_settings(telegram_bot_token="123:abc", telegram_chat_id="42")
    token, chat_id = settings.secrets.require_telegram()
    assert token == "123:abc"
    assert chat_id == "42"


# --- format_run_summary -------------------------------------------------------
def test_leere_ergebnisse_werden_als_keine_empfehlung_formatiert() -> None:
    text = notify.format_run_summary([], {"seen": 0}, run_id="abc123", offline=False)
    assert "Keine Kaufempfehlung" in text
    assert "abc123" in text


def test_empfehlung_erscheint_mit_kernzahlen() -> None:
    text = notify.format_run_summary(
        [make_result()], {"seen": 4, "matched": 1, "errors": 0}, run_id="r1", offline=False
    )
    assert "BRITA Maxtra Plus" in text
    assert "39.99" in text
    assert "34.90" in text
    assert "+12.28" in text
    assert "73.6" in text


def test_nicht_empfohlene_ergebnisse_werden_ausgefiltert() -> None:
    results = [make_result(is_recommended=False), make_result(is_recommended=True)]
    text = notify.format_run_summary(results, {}, run_id="r1", offline=False)
    assert "1 Empfehlung" in text


def test_offline_modus_wird_deutlich_markiert() -> None:
    text = notify.format_run_summary([], {}, run_id="r1", offline=True)
    assert "HEURISTIK" in text


def test_fehler_im_lauf_werden_hervorgehoben() -> None:
    text = notify.format_run_summary([], {"errors": 2}, run_id="r1", offline=False)
    assert "Fehler im Lauf aufgetreten" in text


def test_titel_wird_html_escaped() -> None:
    r = make_result(target_title="<script>alert(1)</script> & Co")
    text = notify.format_run_summary([r], {}, run_id="r1", offline=False)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "&amp;" in text


def test_max_items_begrenzt_die_liste() -> None:
    results = [make_result(target_title=f"Artikel {i}") for i in range(15)]
    text = notify.format_run_summary(results, {}, run_id="r1", offline=False, max_items=3)
    assert text.count("Artikel ") == 3
    assert "12 weitere" in text


def test_nachricht_wird_bei_telegram_limit_gekuerzt() -> None:
    results = [make_result(target_title=f"Sehr langer Artikelname Nummer {i}" * 3) for i in range(50)]
    text = notify.format_run_summary(results, {}, run_id="r1", offline=False, max_items=50)
    assert len(text) <= notify.MAX_MESSAGE_LENGTH


# --- send_message -------------------------------------------------------------
class _FakeResponse:
    def __init__(self, status_code: int = 200, ok: bool = True, text: str = "") -> None:
        self.status_code = status_code
        self._ok = ok
        self.text = text or '{"ok": true}'

    def json(self) -> dict[str, Any]:
        return {"ok": self._ok}


def test_send_message_ohne_secrets_gibt_false(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(notify.requests, "post", lambda *a, **kw: calls.append(1))
    assert notify.send_message(make_settings(), "hallo") is False
    assert calls == []  # kein API-Aufruf ohne Zugangsdaten


def test_send_message_erfolgreich(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_post(url: str, json: dict, timeout: float) -> _FakeResponse:
        captured["url"] = url
        captured["payload"] = json
        return _FakeResponse()

    monkeypatch.setattr(notify.requests, "post", fake_post)
    settings = make_settings(telegram_bot_token="123:abc", telegram_chat_id="42")
    assert notify.send_message(settings, "Testnachricht") is True
    assert "123:abc" in captured["url"]
    assert captured["payload"]["chat_id"] == "42"
    assert captured["payload"]["text"] == "Testnachricht"


def test_send_message_verwendet_expliziten_chat_id(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}
    monkeypatch.setattr(
        notify.requests, "post", lambda url, json, timeout: captured.update(json) or _FakeResponse()
    )
    settings = make_settings(telegram_bot_token="123:abc", telegram_chat_id="42")
    notify.send_message(settings, "hi", chat_id="999")
    assert captured["chat_id"] == "999"


def test_send_message_bei_api_fehler_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        notify.requests, "post", lambda *a, **kw: _FakeResponse(status_code=400, ok=False)
    )
    settings = make_settings(telegram_bot_token="123:abc", telegram_chat_id="42")
    assert notify.send_message(settings, "hi") is False


def test_send_message_bei_timeout_false(monkeypatch: pytest.MonkeyPatch) -> None:
    import requests as requests_module

    def raise_timeout(*a: Any, **kw: Any) -> Any:
        raise requests_module.exceptions.Timeout("timed out")

    monkeypatch.setattr(notify.requests, "post", raise_timeout)
    settings = make_settings(telegram_bot_token="123:abc", telegram_chat_id="42")
    assert notify.send_message(settings, "hi") is False


# --- send_run_summary ----------------------------------------------------------
def test_send_run_summary_ueberspringt_bei_leer_und_notify_on_empty_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    monkeypatch.setattr(notify, "send_message", lambda *a, **kw: calls.append(1) or True)
    settings = make_settings(telegram_bot_token="123:abc", telegram_chat_id="42")
    settings.config.telegram.notify_on_empty = False
    sent = notify.send_run_summary(settings, [], {}, run_id="r1", offline=False)
    assert sent is False
    assert calls == []


def test_send_run_summary_sendet_bei_leer_mit_notify_on_empty_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notify, "send_message", lambda *a, **kw: True)
    settings = make_settings(telegram_bot_token="123:abc", telegram_chat_id="42")
    assert settings.config.telegram.notify_on_empty is True
    assert notify.send_run_summary(settings, [], {}, run_id="r1", offline=False) is True


def test_send_run_summary_konsumiert_generator_nur_einmal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regressionstest: results als Generator darf nicht zweimal durchlaufen
    werden, sonst waere die zweite Iteration leer und Empfehlungen gingen
    verloren."""
    captured_text = {}
    monkeypatch.setattr(
        notify, "send_message", lambda settings, text, **kw: captured_text.setdefault("t", text) or True
    )
    settings = make_settings(telegram_bot_token="123:abc", telegram_chat_id="42")

    def gen():
        yield make_result()

    notify.send_run_summary(settings, gen(), {}, run_id="r1", offline=False)
    assert "BRITA" in captured_text["t"]


# --- Links und Gebinde -----------------------------------------------------------
def test_links_erscheinen_als_html_anker() -> None:
    r = make_result(source_url="https://www.amazon.de/dp/B0X", target_url="https://www.ebay.de/itm/1")
    text = notify.format_run_summary([r], {}, run_id="r1", offline=False)
    assert '<a href="https://www.amazon.de/dp/B0X">Einkauf</a>' in text
    assert '<a href="https://www.ebay.de/itm/1">Verkauf</a>' in text


def test_link_wird_attributsicher_escaped() -> None:
    r = make_result(source_url='https://x.de/?a=1&b="2"')
    text = notify.format_run_summary([r], {}, run_id="r1", offline=False)
    assert 'href="https://x.de/?a=1&amp;b=&quot;2&quot;"' in text


def test_ohne_links_keine_linkzeile() -> None:
    text = notify.format_run_summary([make_result()], {}, run_id="r1", offline=False)
    assert "<a href" not in text


def test_abweichendes_gebinde_wird_erklaert() -> None:
    r = make_result(package_quantity_source=12, package_quantity_target=3)
    text = notify.format_run_summary([r], {}, run_id="r1", offline=False)
    assert "Gebinde 12 → 3" in text
    assert "4 Verkäufe" in text


def test_gleiches_gebinde_ohne_hinweis() -> None:
    r = make_result(package_quantity_source=1, package_quantity_target=1)
    assert "Gebinde" not in notify.format_run_summary([r], {}, run_id="r1", offline=False)
