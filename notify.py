"""Telegram-Benachrichtigung fuer abgeschlossene Scout-Laeufe.

Reiner Versand- und Formatierungscode, bewusst ohne Import aus ``scout.py``:
Der Aufrufer (``scout.py --notify-telegram`` wie auch ``listener.py``)
importiert dieses Modul, nicht umgekehrt -- ein Import in die Gegenrichtung
wuerde einen Zirkelbezug erzeugen. Ergebnisse werden deshalb duck-typed
entgegengenommen (jedes Objekt mit den unten verwendeten Attributen reicht,
z. B. ``scout.ScoutResult``); das haelt dieses Modul unabhaengig testbar.

Nutzt bewusst kein Telegram-SDK, nur ``requests`` -- ein einzelner POST-Aufruf
rechtfertigt keine zusaetzliche Abhaengigkeit.
"""

from __future__ import annotations

from html import escape
from typing import Any, Iterable, Mapping

import requests

from config import Settings
from logging_utils import get_logger

logger = get_logger(__name__)

API_URL = "https://api.telegram.org/bot{token}/sendMessage"
MAX_MESSAGE_LENGTH = 4096
REQUEST_TIMEOUT_SECONDS = 10


def send_message(settings: Settings, text: str, *, chat_id: str | None = None) -> bool:
    """Eine Nachricht senden. Gibt ``False`` zurueck statt zu werfen -- ein
    fehlgeschlagener Telegram-Versand darf einen Scout-Lauf nicht zum Absturz
    bringen, dafuer ist die Benachrichtigung zu unwichtig."""
    try:
        token, default_chat_id = settings.secrets.require_telegram()
    except Exception as exc:
        logger.warning("Telegram-Versand uebersprungen: %s", exc)
        return False

    target_chat_id = chat_id or default_chat_id
    url = API_URL.format(token=token)
    payload = {
        "chat_id": target_chat_id,
        "text": text[:MAX_MESSAGE_LENGTH],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.exceptions.Timeout:
        logger.error("Telegram-Versand: Timeout beim Verbindungsaufbau.")
        return False
    except requests.exceptions.RequestException as exc:
        logger.error("Telegram-Versand: Netzwerkfehler: %s", exc)
        return False

    if resp.status_code == 200 and resp.json().get("ok"):
        logger.info("Telegram-Nachricht gesendet an chat_id=%s (%d Zeichen)", target_chat_id, len(text))
        return True

    logger.error("Telegram API Fehler (%s): %s", resp.status_code, resp.text[:500])
    return False


def _format_recommendation(r: Any) -> str:
    """Eine Zeile pro Empfehlung. ``r`` ist ein ``scout.ScoutResult`` (oder
    strukturell aequivalent) -- siehe Modul-Docstring zum Duck-Typing."""
    title = escape(r.target_title[:70])
    line = (
        f"• <b>{title}</b>\n"
        f"  {r.source_marketplace} {r.source_price_eur:.2f} € → "
        f"{r.target_marketplace} {r.target_price_eur:.2f} €  "
        f"| Gewinn {r.net_profit_eur:+.2f} €  ROI {r.roi_percent:.1f} %"
    )
    # Ohne Gebindehinweis wirkt "Einkauf 39,99 -> Verkauf 34,90" wie ein
    # Verlustgeschaeft, obwohl aus einem 12er-Pack vier 3er-Packs werden.
    qty_source = getattr(r, "package_quantity_source", 1) or 1
    qty_target = getattr(r, "package_quantity_target", 1) or 1
    if qty_source != qty_target:
        line += f"\n  Gebinde {qty_source} → {qty_target} Stk. (Einkauf deckt {qty_source / qty_target:g} Verkäufe)"

    # getattr statt Attributzugriff: Links sind optional (CSV ohne URL-Spalte).
    links = []
    for label, url in (("Einkauf", getattr(r, "source_url", None)),
                       ("Verkauf", getattr(r, "target_url", None))):
        if url:
            links.append(f'<a href="{escape(str(url), quote=True)}">{label}</a>')
    if links:
        line += "\n  " + " · ".join(links)
    return line


def format_run_summary(
    results: Iterable[Any],
    stats: Mapping[str, int],
    *,
    run_id: str,
    offline: bool,
    max_items: int = 10,
) -> str:
    """HTML-formatierte Zusammenfassung eines Laufs fuer Telegram.

    ``results`` und ``stats`` entsprechen ``scout.Scout.run()`` bzw.
    ``scout.Scout.stats``. ``offline`` markiert den Heuristik-Modus deutlich,
    damit niemand eine ungeprueft geratene Empfehlung fuer ein LLM-Urteil haelt.
    """
    results = list(results)
    recommended = [r for r in results if r.is_recommended]

    mode_tag = " ⚠️ HEURISTIK, KEIN LLM" if offline else ""
    lines = [f"🛒 <b>Arbitrage Scout</b> — Lauf {escape(run_id)}{mode_tag}", ""]

    if not recommended:
        lines.append("Keine Kaufempfehlung in diesem Lauf.")
    else:
        lines.append(f"<b>{len(recommended)} Empfehlung(en):</b>")
        for r in recommended[:max_items]:
            lines.append(_format_recommendation(r))
        if len(recommended) > max_items:
            lines.append(f"… und {len(recommended) - max_items} weitere.")

    lines.append("")
    lines.append(
        f"Geprueft: {stats.get('seen', 0)} | Vorgefiltert: {stats.get('prefiltered', 0)} | "
        f"Match: {stats.get('matched', 0)} | Fehler: {stats.get('errors', 0)}"
    )
    if stats.get("errors", 0) > 0:
        lines.append("⚠️ Fehler im Lauf aufgetreten -- Log pruefen.")

    message = "\n".join(lines)
    if len(message) > MAX_MESSAGE_LENGTH:
        logger.warning(
            "Nachricht ueberschreitet %d Zeichen (%d) - wird gekuerzt.",
            MAX_MESSAGE_LENGTH,
            len(message),
        )
        message = message[: MAX_MESSAGE_LENGTH - 20].rstrip() + "\n…"
    return message


def send_run_summary(
    settings: Settings,
    results: Iterable[Any],
    stats: Mapping[str, int],
    *,
    run_id: str,
    offline: bool,
) -> bool:
    """Zusammenfassung eines Laufs formatieren und senden -- die uebliche
    Einstiegsstelle fuer ``scout.py --notify-telegram``.

    ``results`` wird hier einmalig materialisiert: als ``Iterable`` typisiert,
    damit auch Generatoren angenommen werden, aber sowohl fuer die
    Empfehlungspruefung als auch fuer die Formatierung gebraucht -- ein
    Generator waere nach dem ersten Durchlauf leer.
    """
    results = list(results)
    recommended = any(r.is_recommended for r in results)

    if not recommended and not settings.config.telegram.notify_on_empty:
        logger.debug("Keine Empfehlungen und notify_on_empty=false - Versand uebersprungen.")
        return False

    text = format_run_summary(
        results,
        stats,
        run_id=run_id,
        offline=offline,
        max_items=settings.config.telegram.max_recommendations_in_message,
    )
    return send_message(settings, text)


def _link(label: str, url: Any) -> str:
    return f'<a href="{escape(str(url), quote=True)}">{label}</a>'


def format_discovery_summary(
    items: Iterable[Any],
    deals: Iterable[Any],
    stats: Mapping[str, int],
    *,
    run_id: str,
    max_items: int = 10,
) -> str:
    """Fundliste der Produktfindung (``scout.py --discover``) fuer Telegram.

    ``items`` sind ``discovery.HotItem`` (duck-typed wie oben), ``deals`` die
    Ergebnisse einer Amazon-Gegenpruefung, falls es eine gab.
    """
    items = list(items)
    confirmed = [d for d in deals if d.is_recommended]
    lines = [f"🔎 <b>Produktfindung</b> — Lauf {escape(run_id)}", ""]

    if not items:
        lines.append("Keine gut laufenden Artikel, die die Kriterien erfuellen.")
    else:
        lines.append(f"<b>{len(items)} gut laufende Artikel</b> (eBay.de):")
        for idx, item in enumerate(items[:max_items], start=1):
            pace = (
                f"{item.sold_quantity} verkauft, ~{item.sold_per_day:.1f}/Tag"
                if item.sold_per_day is not None
                else f"{item.sold_quantity} verkauft"
            )
            lines.append(f"{idx}. <b>{escape(item.title[:64])}</b>")
            lines.append(
                f"   {item.price_eur:.2f} € · {pace} · "
                f"max. Einkauf <b>{item.max_purchase_price_eur:.2f} €</b>"
            )
            if item.competing_listings > 1 and item.lowest_competitor_price_eur is not None:
                note = (
                    f"   {item.competing_listings} Anbieter, guenstigster "
                    f"{item.lowest_competitor_price_eur:.2f} €"
                )
                at_lowest = getattr(item, "max_purchase_at_lowest_price_eur", None)
                if at_lowest is not None:
                    note += (
                        f" → dann max. Einkauf {at_lowest:.2f} €" if at_lowest > 0
                        else " → zu dem Preis nicht rentabel"
                    )
                lines.append(note)
            links = [_link("eBay", item.url)] if item.url else []
            links += [_link(label, url) for label, url in list(item.research_links.items())[:3]]
            if links:
                lines.append("   " + " · ".join(links))
        if len(items) > max_items:
            lines.append(f"… und {len(items) - max_items} weitere im Export.")

    if confirmed:
        lines.append("")
        lines.append(f"✅ <b>{len(confirmed)} davon mit Amazon-Einkaufsquelle bestaetigt:</b>")
        for deal in confirmed[:max_items]:
            lines.append(_format_recommendation(deal))

    lines.append("")
    lines.append(
        f"{stats.get('detail_lookups', 0)} Angebote geprueft · "
        f"{stats.get('api_calls', 0)} eBay-Aufrufe"
    )
    lines.append("Stueckzahlen sind eBays Schaetzung je Angebot, Preise sind Angebotspreise.")

    message = "\n".join(lines)
    if len(message) > MAX_MESSAGE_LENGTH:
        message = message[: MAX_MESSAGE_LENGTH - 20].rstrip() + "\n…"
    return message


def send_discovery_summary(
    settings: Settings,
    items: Iterable[Any],
    deals: Iterable[Any],
    stats: Mapping[str, int],
    *,
    run_id: str,
) -> bool:
    items = list(items)
    if not items and not settings.config.telegram.notify_on_empty:
        return False
    text = format_discovery_summary(
        items, list(deals), stats,
        run_id=run_id,
        max_items=settings.config.telegram.max_recommendations_in_message,
    )
    return send_message(settings, text)
