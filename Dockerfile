# syntax=docker/dockerfile:1
#
# Schlankes Image fuer den headless Betrieb auf einem Linux-VPS.
#
#   docker build -t arbitrage-scout .
#   docker run --rm --env-file .env -v "$PWD/data:/app/data" -v "$PWD/logs:/app/logs" \
#              arbitrage-scout --dry-run
#
# Cron laeuft auf dem Host, nicht im Container -- ein Container pro Lauf ist
# einfacher zu ueberwachen als ein Daemon mit eigenem crond:
#   15 6 * * * docker run --rm --env-file /opt/scout/.env \
#              -v /opt/scout/data:/app/data -v /opt/scout/logs:/app/logs \
#              arbitrage-scout >/dev/null 2>&1

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/Berlin

WORKDIR /app

# Abhaengigkeiten zuerst -- bleibt bei Codeaenderungen im Build-Cache.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py calculator.py llm_providers.py matcher.py logging_utils.py models.py sources.py notify.py listener.py scout.py ./
COPY config.yaml ./

# Unprivilegierter Nutzer; Datenverzeichnisse gehoeren ihm.
RUN useradd --create-home --uid 10001 scout \
    && mkdir -p /app/logs /app/data \
    && chown -R scout:scout /app
USER scout

# Frueher Fehlschlag beim Build, falls ein Modul kaputt ist.
RUN python -c "import config, calculator, llm_providers, matcher, models, sources, notify, listener, scout"

# Secrets kommen zur Laufzeit ueber --env-file, nie ins Image.
ENTRYPOINT ["python", "scout.py"]
CMD ["--help"]
