"""Logging-Setup fuer den unbeaufsichtigten Betrieb auf dem VPS.

* Rotierende Datei-Logs (Groesse begrenzt, damit die Platte nicht volllaeuft)
* Konsole nur, wenn gewuenscht -- im Cronjob meist unnoetiges Rauschen
* Optional JSON-Zeilen fuer Loki/ELK
* Ein Filter, der API-Keys und Token aus Logzeilen entfernt
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # nur fuer Typpruefung, vermeidet Import-Zyklus
    from config import Settings

# Bibliotheken, deren DEBUG-Ausgabe den eigenen Log unlesbar macht.
_NOISY_LOGGERS = ("httpx", "httpcore", "urllib3", "google", "google_genai", "asyncio")

# Muster fuer Werte, die niemals im Klartext im Log stehen duerfen.
_SECRET_PATTERNS = (
    re.compile(r"(AIza[0-9A-Za-z\-_]{10,})"),                       # Google API Keys
    re.compile(r"((?:api[_-]?key|token|secret|password)\"?\s*[:=]\s*\"?)([^\s\"',]{6,})", re.I),
    re.compile(r"(Bearer\s+)([A-Za-z0-9\-._~+/]{10,})", re.I),
)

_configured = False
RUN_ID = uuid.uuid4().hex[:8]


class SecretRedactingFilter(logging.Filter):
    """Ersetzt erkannte Secrets durch ``***``.

    Zweite Verteidigungslinie -- Secrets liegen ohnehin als ``SecretStr`` vor,
    aber Tracebacks von Drittbibliotheken halten sich nicht daran.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # kaputte %-Formatierung nicht zum Absturz fuehren lassen
            return True
        redacted = self._redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True

    @staticmethod
    def _redact(text: str) -> str:
        for pattern in _SECRET_PATTERNS:
            if pattern.groups >= 2:
                text = pattern.sub(lambda m: f"{m.group(1)}***", text)
            else:
                text = pattern.sub("***", text)
        return text


class RunIdFilter(logging.Filter):
    """Haengt jedem Datensatz die Lauf-ID an -- macht Cron-Laeufe unterscheidbar."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = RUN_ID
        return True


class JsonFormatter(logging.Formatter):
    """Eine JSON-Zeile pro Log-Eintrag."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "run_id": getattr(record, "run_id", RUN_ID),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(settings: Settings | None = None, *, force: bool = False) -> logging.Logger:
    """Root-Logger konfigurieren und den Anwendungs-Logger zurueckgeben.

    Idempotent: mehrfache Aufrufe fuegen keine doppelten Handler hinzu.
    """
    global _configured
    root = logging.getLogger()

    if _configured and not force:
        return logging.getLogger("scout")

    if settings is None:
        from config import get_settings  # spaeter Import bricht den Zyklus

        settings = get_settings()

    cfg = settings.config.logging
    level = getattr(logging, cfg.level, logging.INFO)

    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    root.setLevel(level)

    plain = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(run_id)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    formatter: logging.Formatter = JsonFormatter() if cfg.json_format else plain

    redactor = SecretRedactingFilter()
    run_id_filter = RunIdFilter()

    log_path: Path = settings.log_file
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=cfg.max_bytes,
            backupCount=cfg.backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redactor)
        file_handler.addFilter(run_id_filter)
        root.addHandler(file_handler)
        file_error: OSError | None = None
    except OSError as exc:
        # Kein Schreibrecht auf das Logverzeichnis darf den Lauf nicht stoppen.
        file_error = exc

    if cfg.console or not root.handlers:
        console = logging.StreamHandler(stream=sys.stderr)
        console.setFormatter(formatter)
        console.addFilter(redactor)
        console.addFilter(run_id_filter)
        root.addHandler(console)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(max(level, logging.WARNING))

    logging.captureWarnings(True)
    _configured = True

    logger = logging.getLogger("scout")
    if file_error is not None:
        logger.warning("Datei-Logging deaktiviert (%s): %s", log_path, file_error)
    else:
        logger.debug("Logging aktiv -> %s (Level %s)", log_path, cfg.level)
    return logger


def get_logger(name: str) -> logging.Logger:
    """Untergeordneten Logger holen, z. B. ``get_logger(__name__)``."""
    return logging.getLogger(name if name.startswith("scout") else f"scout.{name}")
