"""Telegram-Listener: nimmt Befehle entgegen, startet/stoppt Scout-Laeufe.

Long-Polling gegen die Telegram Bot API (``getUpdates``) -- kein Webhook, kein
offener Port noetig, passt zum headless VPS-Betrieb. Laeuft als eigener,
dauerhafter Prozess (systemd), getrennt vom Cronjob.

Befehle:
    /start, /help   Kurzuebersicht (Telegram sendet /start automatisch, sobald
                     jemand den Chat oeffnet -- deshalb loest es bewusst KEINEN
                     Scan aus, sonst kostet schon das Oeffnen des Chats Geld).
    /scan, /run      Sofortigen Lauf anstossen (--notify-telegram, --quiet).
    /stop            Aktiven Lauf abbrechen -- egal ob ueber den Listener oder
                     per Cron gestartet (ueber das PID-Lockfile erkannt).
    /status          Zeigt, ob gerade ein Lauf aktiv ist.

Sicherheit: Nur Nachrichten von der in .env konfigurierten TELEGRAM_CHAT_ID
werden verarbeitet. Jede andere chat_id wird geloggt und ohne Antwort
verworfen -- sonst koennte jeder, der die Bot-ID errataet, Laeufe mit
Google/Groq-API-Kosten auf dem Server auslösen.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from config import ConfigError, Settings, load_settings
from logging_utils import get_logger, setup_logging
from notify import send_message

logger = get_logger(__name__)

GET_UPDATES_URL = "https://api.telegram.org/bot{token}/getUpdates"
PROJECT_DIR = Path(__file__).resolve().parent
SCOUT_SCRIPT = PROJECT_DIR / "scout.py"

# Wie lange nach einem SIGTERM gewartet wird, bevor hart nachgelegt wird
# (SIGKILL). Grosszuegig bemessen: ein einzelnes Angebotspaar kann durch
# Retries mit Backoff (siehe gemini.max_backoff_seconds / groq.*) durchaus eine
# ganze Weile brauchen, und der Lauf beendet sich erst zwischen zwei Paaren.
TERMINATE_GRACE_SECONDS = 60.0

HELP_TEXT = (
    "🤖 <b>Arbitrage Scout Bot</b>\n\n"
    "/scan – Lauf jetzt starten (nutzt config.yaml-Schwellen, sendet Ergebnis hierher)\n"
    "/stop – Aktiven Lauf abbrechen (Cron oder /scan)\n"
    "/status – Laeuft gerade etwas?\n"
    "/help – Diese Uebersicht"
)


def _is_process_alive(pid: int) -> bool:
    """Signal 0 prueft nur die Existenz, sendet nichts -- gleiches Muster wie
    in scout.RunLock._is_stale."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except AttributeError:  # pragma: no cover - Plattformen ohne os.kill
        return False
    return True


def _read_lock_pid(lock_path: Path) -> int | None:
    """PID aus einem laufenden Scout-Lock lesen, ``None`` wenn keiner aktiv ist.

    Deckt auch per Cron gestartete Laeufe ab, nicht nur ueber diesen Listener
    angestossene -- der Lockfile-Pfad ist derselbe (``logs/scout.lock``).
    """
    try:
        pid = int(lock_path.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if _is_process_alive(pid) else None


class ListenerState:
    """Haelt den vom Listener selbst gestarteten Subprozess, falls einer laeuft.

    ``spawn`` ist injizierbar, damit Tests keine echten Prozesse starten
    muessen -- Produktionscode uebergibt eine ``subprocess.Popen``-Fabrik,
    Tests einen Fake mit derselben Schnittstelle (poll/terminate/kill/pid).
    """

    def __init__(self, lock_path: Path, spawn: Callable[[], Any]) -> None:
        self.lock_path = lock_path
        self._spawn = spawn
        self.process: Any = None
        self.started_at: datetime | None = None
        self.terminate_requested_at: datetime | None = None

    def _own_process_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> str:
        if self._own_process_alive():
            started = self.started_at.strftime("%H:%M:%S") if self.started_at else "?"
            return f"⏳ Ein Lauf ist bereits aktiv (gestartet um {started}). /stop zum Abbrechen."

        external_pid = _read_lock_pid(self.lock_path)
        if external_pid is not None:
            return f"⏳ Ein Lauf ist bereits aktiv (PID {external_pid}, z. B. per Cron). /stop zum Abbrechen."

        self.process = self._spawn()
        self.started_at = datetime.now(timezone.utc)
        self.terminate_requested_at = None
        return f"▶️ Lauf gestartet (PID {self.process.pid}). Ich melde mich, wenn er fertig ist."

    def stop(self) -> str:
        if self._own_process_alive():
            self.process.terminate()
            self.terminate_requested_at = datetime.now(timezone.utc)
            return "⏹️ Abbruch angefordert - der Lauf beendet sich nach dem aktuellen Paar."

        external_pid = _read_lock_pid(self.lock_path)
        if external_pid is not None:
            try:
                os.kill(external_pid, signal.SIGTERM)
            except OSError as exc:
                return f"⚠️ Konnte PID {external_pid} nicht signalisieren: {exc}"
            return f"⏹️ Abbruch an PID {external_pid} gesendet (z. B. Cron-Lauf)."

        return "⚪ Kein Lauf aktiv."

    def status(self) -> str:
        if self._own_process_alive():
            started = self.started_at.strftime("%H:%M:%S") if self.started_at else "?"
            return f"🟢 Lauf aktiv seit {started} (PID {self.process.pid}, ueber diesen Bot gestartet)."
        external_pid = _read_lock_pid(self.lock_path)
        if external_pid is not None:
            return f"🟢 Lauf aktiv (PID {external_pid}, extern gestartet, z. B. Cron)."
        return "⚪ Kein Lauf aktiv."

    def poll_finished(self) -> str | None:
        """Periodisch aufrufen. Meldet einmalig, wenn der eigene Prozess fertig
        ist, oder eskaliert auf SIGKILL, wenn er nach dem Grace-Zeitraum immer
        noch auf das SIGTERM nicht reagiert hat."""
        if self.process is None:
            return None

        returncode = self.process.poll()
        if returncode is not None:
            pid = self.process.pid
            self.process = None
            self.started_at = None
            self.terminate_requested_at = None
            if returncode == 0:
                return f"✅ Lauf beendet (PID {pid})."
            if returncode == 130:
                return f"⏹️ Lauf abgebrochen (PID {pid})."
            return f"❌ Lauf fehlgeschlagen (PID {pid}, Exit {returncode}) - Log pruefen."

        if self.terminate_requested_at is not None:
            waited = (datetime.now(timezone.utc) - self.terminate_requested_at).total_seconds()
            if waited > TERMINATE_GRACE_SECONDS:
                logger.warning(
                    "Prozess PID %s reagiert %.0fs nach SIGTERM nicht - sende SIGKILL.",
                    self.process.pid,
                    waited,
                )
                self.process.kill()
                self.terminate_requested_at = None  # nur einmal eskalieren
        return None


def _spawn_scan(python_bin: str = sys.executable) -> subprocess.Popen:
    """Scout als eigenstaendigen Prozess starten.

    ``--notify-telegram`` laesst scout.py die Ergebnis-Nachricht selbst senden
    (derselbe Code wie beim Cron-Lauf) -- der Listener muss die Ergebnisse
    dafuer nicht selbst einsammeln. ``--quiet`` unterdrueckt nur den
    Klartext-Report auf stdout, der hier niemand liest.
    """
    return subprocess.Popen(
        [python_bin, str(SCOUT_SCRIPT), "--notify-telegram", "--quiet"],
        cwd=str(PROJECT_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def dispatch(text: str, state: ListenerState) -> str | None:
    """Befehlstext einer Aktion zuordnen. ``None`` = keine Antwort noetig
    (aktuell ungenutzt, aber die Signatur laesst spaeter stille Befehle zu)."""
    command = text.strip().split()[0].lower() if text.strip() else ""

    if command in ("/scan", "/run"):
        return state.start()
    if command == "/stop":
        return state.stop()
    if command == "/status":
        return state.status()
    return HELP_TEXT  # /start, /help, alles Unbekannte


def run_listener(settings: Settings) -> None:
    token, chat_id = settings.secrets.require_telegram()
    lock_path = settings.resolve("logs/scout.lock")
    state = ListenerState(lock_path, spawn=_spawn_scan)

    poll_seconds = settings.config.telegram.long_poll_seconds
    url = GET_UPDATES_URL.format(token=token)
    offset: int | None = None

    logger.info(
        "Telegram-Listener gestartet (Long-Poll %.0fs, autorisierte chat_id=%s). Strg+C zum Beenden.",
        poll_seconds,
        chat_id,
    )

    while True:
        try:
            finished_note = state.poll_finished()
            if finished_note:
                send_message(settings, finished_note, chat_id=chat_id)

            params: dict[str, Any] = {"timeout": poll_seconds}
            if offset is not None:
                params["offset"] = offset
            resp = requests.get(url, params=params, timeout=poll_seconds + 5)
            resp.raise_for_status()
            data = resp.json()

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message") or update.get("edited_message")
                if not message or "text" not in message:
                    continue

                sender_chat_id = str(message["chat"]["id"])
                text = message["text"]

                if sender_chat_id != chat_id:
                    logger.warning(
                        "Nachricht von nicht autorisierter chat_id=%s ignoriert: %r",
                        sender_chat_id,
                        text[:100],
                    )
                    continue

                logger.info("Befehl von chat_id=%s: %s", sender_chat_id, text)
                reply = dispatch(text, state)
                if reply:
                    send_message(settings, reply, chat_id=sender_chat_id)

        except requests.exceptions.Timeout:
            continue
        except requests.exceptions.RequestException as exc:
            logger.error("Telegram getUpdates fehlgeschlagen: %s", exc)
            time.sleep(5.0)
        except KeyboardInterrupt:
            logger.info("Listener durch Nutzer beendet.")
            break
        except Exception:
            logger.exception("Unerwarteter Fehler im Listener-Loop, wird fortgesetzt.")
            time.sleep(5.0)


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        return 2

    setup_logging(settings, force=True)

    if not settings.secrets.has_telegram:
        logger.error(
            "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID fehlen - Listener kann nicht starten."
        )
        return 2

    try:
        run_listener(settings)
    except Exception:
        logger.exception("Listener abgebrochen.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
