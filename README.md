# Arbitrage Selling Scout

Findet Preisunterschiede zwischen Einkauf (Amazon / AliExpress) und Verkauf (eBay)
und prüft per LLM — Google Gemini oder Groq — ob es sich überhaupt um dasselbe
Produkt handelt. Ausgelegt auf headless Betrieb per Cronjob auf einem Linux-VPS.

Der teure Fehler im Arbitragehandel ist nicht die falsche Marge, sondern das
falsche Produkt: ein 5er-Pack gegen ein Einzelstück, 128 GB gegen 256 GB,
Markenware gegen Nachbau. Genau dafür ist das LLM da — die Rechnung selbst
bleibt deterministisch in `calculator.py`.

## Aufbau

| Datei | Aufgabe |
|---|---|
| `config.yaml` | Fachliche Parameter: Schwellen, Pauschalen, Suchbegriffe. Gehört ins Repo. |
| `.env` | Nur Secrets (API-Keys). Gehört **nicht** ins Repo, Vorlage: `.env.example`. |
| `config.py` | Lädt und validiert beides über Pydantic Settings. |
| `models.py` | Gemeinsame Datenmodelle (`Offer`, `CandidatePair`). |
| `matcher.py` | Antwortschema, Prompt, Retry, Rate-Limit, Cache, Offline-Heuristik. |
| `llm_providers.py` | Austauschbare LLM-Backends: Gemini und Groq. |
| `calculator.py` | Netto-Margenrechner mit Decimal-Arithmetik. |
| `sources.py` | Woher die Angebote kommen (CSV / Mock; erweiterbar um echte APIs). |
| `scout.py` | Orchestrator mit CLI. |
| `logging_utils.py` | Rotierende Logs, Secret-Redaction, Lauf-ID. |

Konfigurations-Priorität, absteigend: CLI-Flag → Umgebungsvariable → `.env` → `config.yaml`.

## Schnellstart

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && chmod 600 .env   # GEMINI_API_KEY eintragen
python scout.py --check-config
python scout.py --write-example-data     # data/offers.example.csv
cp data/offers.example.csv data/offers.csv
python scout.py --dry-run
```

`--dry-run` ruft die API nicht auf und schreibt nichts auf die Platte. Ohne
API-Key fällt der Matcher automatisch auf eine Titel-Heuristik zurück — die
reicht, um die Pipeline zu prüfen, aber ausdrücklich **nicht** für echte
Kaufentscheidungen.

In `.env` genügt der Key des Backends, das du verwendest: `GEMINI_API_KEY`
oder `GROQ_API_KEY`.

## CLI

```
--dry-run              Kein API-Aufruf, kein Export
--category NAME        Nur eine Kategorie aus config.yaml
--limit N              Obergrenze der Kandidaten in diesem Lauf
--config PFAD          Alternative Konfigurationsdatei
--min-roi PROZENT      Mindest-ROI überschreiben
--min-profit EUR       Mindestgewinn überschreiben
--provider NAME        LLM-Backend für diesen Lauf (gemini | groq)
--offline              Heuristik statt LLM, Export bleibt aktiv
--no-prefilter         Jedes Paar ans LLM schicken (teurer, vollständiger)
--no-export            Nicht auf die Platte schreiben
--no-lock              Ohne Lockfile laufen
--log-level LEVEL      Loglevel für diesen Lauf
-v / -q                Volle Kostenaufschlüsselung / kein stdout-Report
--check-config         Konfiguration prüfen und beenden
--write-example-data   Beispiel-CSV erzeugen
```

Exit-Codes: `0` ok, `1` Laufzeitfehler, `2` Konfigurationsfehler, `130` abgebrochen.

## LLM-Backends

Umschaltbar über `llm.provider` in `config.yaml` oder `--provider` pro Lauf:

| | Gemini | Groq |
|---|---|---|
| Schema | echtes `response_schema`, serverseitig erzwungen | JSON-Modus; `json_schema` nur bei manchen Modellen |
| Tempo | solide | deutlich schneller |
| Rate-Limit | großzügig | im Free-Tier eng (Default 30/min) |
| Key | `GEMINI_API_KEY` | `GROQ_API_KEY` |

```bash
python scout.py --provider groq          # Backend für diesen Lauf
python scout.py --provider gemini --limit 20
```

Nötig ist nur der Key des Providers, den du tatsächlich nutzt. Fehlt er, fällt
der Lauf auf die Titel-Heuristik zurück, statt abzubrechen — im Report und im
Log als `heuristic` gekennzeichnet.

Setzt du bei Groq `json_schema_mode: true` und das Modell unterstützt es nicht,
schaltet der Provider nach der ersten Ablehnung selbsttätig auf den JSON-Modus
um und protokolliert das. Der Lauf geht dadurch nicht verloren. Groq wechselt
sein Modellangebot häufiger als Google — vor dem ersten Lauf lohnt ein Blick in
die [aktuelle Modellliste](https://console.groq.com/docs/models).

## Die Rechnung

```
Reingewinn = eBay-Brutto
           − eBay-Gebühren (12 % + 0,35 €)
           − Wareneinsatz (auf die verkaufte Stückzahl normiert)
           − Inlandsversand − Verpackung − Retourenpuffer
ROI        = Reingewinn / eingesetztes Kapital
```

Optional zuschaltbar in `config.yaml`, standardmäßig aus: Umsatzsteuer bei
Regelbesteuerung, Vorsteuerabzug, Einfuhrzoll, Wareneingangsversand,
separate Zahlungsgebühren.

**Gebindenormierung:** Gemini liefert `package_quantity_source` und
`package_quantity_target`. Wer ein 12er-Pack für 39,99 € kauft und ein 3er-Pack
verkauft, trägt pro Verkauf nur 10,00 € Wareneinsatz. Ohne diese Normierung
rechnet man sich systematisch arm — oder, in der Gegenrichtung, reich.

Jedes Ergebnis enthält zusätzlich `break_even_price_eur` und
`min_viable_sale_price_eur` — der Preis, ab dem der Artikel die konfigurierten
Schwellen erreicht.

## Kostenkontrolle

Vor jedem LLM-Aufruf läuft ein kostenloser Vorfilter: Gebindegrößen werden aus
den Titeln geschätzt und die Marge überschlagen. Verworfen wird erst deutlich
unterhalb der echten Schwellen (`search.prefilter_margin_factor`, Standard 0.5),
damit Grenzfälle nicht ungeprüft verloren gehen. Dazu kommen ein Ergebnis-Cache
pro Lauf, `search.max_candidates_per_run` als harte Obergrenze und
clientseitiges Rate-Limiting (`gemini.requests_per_minute`).

## Betrieb auf dem VPS

`deploy/vps_setup.sh` richtet den Server ein — Systempakete, Service-Benutzer,
getrennte venvs für diesen Scout und den Pre-Market Screener, Verzeichnisse,
Rechte, optional Cron und logrotate. Das Skript ist idempotent und der übliche
Weg, nach einem Update die Abhängigkeiten nachzuziehen.

### Installation aus diesem Repository

Das Repository ist oeffentlich, der VPS braucht also keine Zugangsdaten:

```bash
REPO=https://github.com/sigmagammalabs/Arbitrage-Scout.git

git clone "$REPO" /tmp/scout-bootstrap
sudo bash /tmp/scout-bootstrap/deploy/vps_setup.sh --scout-repo "$REPO" --dry-run
sudo bash /tmp/scout-bootstrap/deploy/vps_setup.sh --scout-repo "$REPO" --with-cron
```

Das Skript klont den Code nach `/opt/trading/arbitrage-scout/`, legt beide venvs
an und installiert die Abhängigkeiten. `bash vps_setup.sh --help` listet alle
Optionen.

Danach den API-Key eintragen — je nach `llm.provider` `GEMINI_API_KEY` oder
`GROQ_API_KEY`:

```bash
sudoedit /opt/trading/arbitrage-scout/.env
sudo -u trader env -C /opt/trading/arbitrage-scout .venv/bin/python scout.py --check-config
```

**Wenn du das Repository später auf privat stellst,** braucht der VPS Lesezugriff.
Sauberste Variante ist ein Deploy Key — nur für dieses eine Repository gültig,
read-only, jederzeit widerrufbar:

```bash
# auf dem VPS
ssh-keygen -t ed25519 -C "vps-deploy" -f ~/.ssh/id_scout -N ""
cat ~/.ssh/id_scout.pub
# Inhalt eintragen unter: Repo > Settings > Deploy keys > Add deploy key
# (Haken "Allow write access" NICHT setzen)

cat >> ~/.ssh/config <<'CFG'
Host github-scout
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_scout
CFG
```

Danach als Repo-URL `git@github-scout:sigmagammalabs/Arbitrage-Scout.git`
verwenden. Ein persönlicher Access Token im Klartext in der URL funktioniert
zwar auch, landet aber in `.git/config` und in der Shell-History — der Deploy
Key ist die bessere Wahl.

### Updates

```bash
cd /opt/trading/arbitrage-scout && sudo -u trader git pull
sudo bash /tmp/scout-bootstrap/deploy/vps_setup.sh   # Abhängigkeiten nachziehen
```

Der Cron-Eintrag, den es schreibt:

```bash
# Cron: täglich 06:15, Ausgabe landet im rotierenden Logfile
15 6 * * * cd /opt/arbitrage-scout && /opt/arbitrage-scout/.venv/bin/python scout.py >/dev/null 2>&1
```

Ein PID-Lockfile (`logs/scout.lock`) verhindert überlappende Läufe; verwaiste
Locks werden erkannt und entfernt. `SIGTERM`/`SIGINT` beenden den Lauf nach dem
laufenden Paar, statt mitten im API-Aufruf abzubrechen.

Logs rotieren bei 5 MB (5 Sicherungen). `logging.json_format: true` schaltet auf
JSON-Zeilen für Loki/ELK um. Ein Filter entfernt API-Keys und Bearer-Token aus
Logzeilen — auch aus Tracebacks fremder Bibliotheken.

### Docker

```bash
docker build -t arbitrage-scout .
docker run --rm --env-file .env \
  -v "$PWD/data:/app/data" -v "$PWD/logs:/app/logs" \
  arbitrage-scout --dry-run
```

Cron läuft auf dem Host, ein Container pro Lauf — einfacher zu überwachen als
ein Daemon mit eigenem `crond`. Secrets kommen zur Laufzeit über `--env-file`,
nie ins Image.

## Datenquellen

Mitgeliefert sind `csv` (Standard) und `mock` (eingebaute Beispiele,
`sources.provider: mock`). Ein echter Connector implementiert nur
`OfferSource.fetch_pairs` in `sources.py`; `scout.py` bleibt unangetastet.

Bewusst nicht enthalten ist Scraping gegen Amazon oder eBay — das verstößt gegen
deren Nutzungsbedingungen. Vorgesehener Weg sind die offiziellen APIs
(eBay Browse API, Amazon PA-API) oder ein lizenzierter Datenanbieter; die
Zugangsdaten dafür sind in `.env.example` bereits vorgesehen.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```

57 Tests, ohne Netzwerk. Der Online-Pfad wird mit eingeschleusten
Fake-Providern geprüft: Retry bei 429/5xx, Abbruch nach `max_retries`, kein
Retry bei fachlichen Fehlern, Weiterlaufen nach Einzelfehlern, und für Groq der
Rückfall von `json_schema` auf den JSON-Modus.

## Haftungsausschluss

Das Werkzeug liefert eine Entscheidungsgrundlage, keine Kaufentscheidung.
LLM-Urteile können falsch sein; `llm.min_confidence` und der Retourenpuffer
begrenzen das Risiko, beseitigen es aber nicht. Preise, Gebühren und
Verfügbarkeiten ändern sich zwischen Lauf und Kauf. Steuerliche Behandlung
(§ 19 UStG vs. Regelbesteuerung, Einfuhrumsatzsteuer bei Drittlandsware) ist
konfigurierbar, ersetzt aber keine Steuerberatung.
