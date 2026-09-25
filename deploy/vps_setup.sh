#!/usr/bin/env bash
#
# vps_setup.sh -- bereitet einen Linux-VPS fuer zwei unabhaengige Python-Dienste vor:
#
#   <BASE_DIR>/arbitrage-scout/      Amazon/AliExpress vs. eBay Arbitrage-Scout
#   <BASE_DIR>/premarket-screener/   Wertpapiere Pre-Market Screener
#
# Jeder Dienst bekommt ein eigenes virtuelles Environment. Das ist keine
# Bequemlichkeit, sondern Absicht: Der Screener bringt typischerweise pandas
# und numpy mit, der Scout ein Google-SDK. Ein gemeinsames venv fuehrt frueher
# oder spaeter zu einem Versionskonflikt, der beide Dienste gleichzeitig kippt.
#
# Das Skript ist idempotent -- mehrfaches Ausfuehren ist unschaedlich und der
# uebliche Weg, nach einem Code-Update die Abhaengigkeiten nachzuziehen.
#
# Aufruf (als root fuer den Systemteil, sonst mit --base-dir ins HOME):
#
#   sudo bash vps_setup.sh --with-cron
#   bash vps_setup.sh --base-dir "$HOME/trading" --no-system-packages
#   bash vps_setup.sh --dry-run          # nur anzeigen, nichts veraendern
#
set -Eeuo pipefail

# ---------------------------------------------------------------------------
# Vorgaben -- alle per Flag oder Umgebungsvariable ueberschreibbar
# ---------------------------------------------------------------------------
BASE_DIR="${BASE_DIR:-/opt/trading}"
SERVICE_USER="${SERVICE_USER:-trader}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
MIN_PY_MINOR=10                       # Projekte nutzen `X | None`-Syntax

SCOUT_NAME="arbitrage-scout"
SCREENER_NAME="premarket-screener"

# Beide Repositories sind oeffentlich; ohne Angabe werden sie geklont.
SCOUT_REPO="${SCOUT_REPO:-https://github.com/sigmagammalabs/Arbitrage-Scout.git}"
SCREENER_REPO="${SCREENER_REPO:-https://github.com/sigmagammalabs/Stock-Pre-Market-Screener.git}"

# --- Arbitrage Scout ---
SCOUT_CRON_TZ="${SCOUT_CRON_TZ:-Europe/Berlin}"
SCOUT_CRON_TIME="${SCOUT_CRON_TIME:-15 6 * * *}"          # taeglich 06:15
SCOUT_ENTRY="${SCOUT_ENTRY:-scout.py}"                    # Existenzpruefung
SCOUT_CRON_CMD="${SCOUT_CRON_CMD:-.venv/bin/python scout.py}"
SCOUT_LOG="${SCOUT_LOG:-logs/cron.log}"

# --- Pre-Market Screener ---
# Der Screener bringt seinen eigenen Cron-Wrapper mit (deploy/run_scan.sh). Der
# ruft `main.py scan` mit den passenden Argumenten auf -- ein selbst gebautes
# `python main.py` waere ohne Unterbefehl schlicht falsch. Deshalb wird hier
# delegiert statt nachgebaut.
#
# Zeitzone Europe/Berlin und 08:15: Das Projekt screent die EURO STOXX 50, nicht
# US-Werte. Xetra oeffnet um 09:00 Berliner Zeit -- eine Umrechnung nach New York
# waere hier schlicht das falsche Handelsgebiet.
SCREENER_CRON_TZ="${SCREENER_CRON_TZ:-Europe/Berlin}"
SCREENER_CRON_TIME="${SCREENER_CRON_TIME:-15 8 * * 1-5}"  # Mo-Fr 08:15, 45 Min vor Xetra
SCREENER_ENTRY="${SCREENER_ENTRY:-main.py}"               # Existenzpruefung
SCREENER_CRON_CMD="${SCREENER_CRON_CMD:-deploy/run_scan.sh}"
SCREENER_LOG="${SCREENER_LOG:-watchlist/cron.log}"
# Telegram-Listener als systemd-Dienst (Dauerbetrieb, nicht Cron).
SCREENER_SERVICE_NAME="${SCREENER_SERVICE_NAME:-watchlist-listener}"
SCREENER_SERVICE_TEMPLATE="deploy/watchlist-listener.service.template"

WITH_CRON=0
WITH_LISTENER=0
WITH_LOGROTATE=1
INSTALL_SYSTEM_PACKAGES=1
DRY_RUN=0
SETUP_SCOUT=1
SETUP_SCREENER=1

# ---------------------------------------------------------------------------
# Ausgabe
# ---------------------------------------------------------------------------
if [[ -t 1 ]]; then
    C_RESET=$'\033[0m'; C_INFO=$'\033[1;34m'; C_OK=$'\033[1;32m'
    C_WARN=$'\033[1;33m'; C_ERR=$'\033[1;31m'; C_DIM=$'\033[2m'
else
    C_RESET=''; C_INFO=''; C_OK=''; C_WARN=''; C_ERR=''; C_DIM=''
fi

WARNINGS=()

log()  { printf '%s==>%s %s\n' "$C_INFO" "$C_RESET" "$*"; }
step() { printf '%s   ->%s %s\n' "$C_INFO" "$C_RESET" "$*"; }
ok()   { printf '%s  ok%s %s\n' "$C_OK" "$C_RESET" "$*"; }
skip() { printf '%s  --%s %s\n' "$C_DIM" "$C_RESET" "$*"; }
die()  { printf '%sFEHLER:%s %s\n' "$C_ERR" "$C_RESET" "$*" >&2; exit 1; }

warn() {
    # Fuehrende Leerzeichen entfernen: die Meldung wird sowohl eingerueckt im
    # Verlauf als auch flach in der Zusammenfassung ausgegeben.
    local msg="${*#"${*%%[![:space:]]*}"}"
    printf '%s  !!%s %s\n' "$C_WARN" "$C_RESET" "$msg" >&2
    WARNINGS+=("$msg")
}

on_error() {
    local line=$1
    printf '%sAbbruch in Zeile %s.%s Das Skript ist idempotent - nach dem\n' \
        "$C_ERR" "$line" "$C_RESET" >&2
    printf 'Beheben der Ursache kann es einfach erneut laufen.\n' >&2
}
trap 'on_error $LINENO' ERR

# Fuehrt ein Kommando aus -- oder zeigt es nur an (--dry-run).
run() {
    if (( DRY_RUN )); then
        printf '%s  would run:%s %s\n' "$C_DIM" "$C_RESET" "$*"
    else
        "$@"
    fi
}

# Fuehrt ein Kommando als Service-Benutzer aus.
#
# Noetig vor allem fuer git: Ein `git pull` als root auf einem Arbeitsbaum, der
# einem anderen Benutzer gehoert, bricht mit "detected dubious ownership" ab --
# und das trifft ausgerechnet den Update-Weg, fuer den dieses Skript gedacht ist.
# runuser kommt aus util-linux und ist praktisch immer da; sudo fehlt auf
# minimalen Images oefter.
run_as_service_user() {
    if (( ! IS_ROOT )) || [[ "$SERVICE_USER" == "root" ]]; then
        run "$@"
        return
    fi
    if command -v runuser >/dev/null 2>&1; then
        run runuser -u "$SERVICE_USER" -- "$@"
    elif command -v sudo >/dev/null 2>&1; then
        run sudo -u "$SERVICE_USER" "$@"
    else
        warn "Weder runuser noch sudo vorhanden - laeuft als root: $*"
        run "$@"
    fi
}

# Schreibt eine Datei aus stdin. Im Dry-Run nur der Hinweis.
write_file() {
    local path=$1 mode=${2:-644}
    if (( DRY_RUN )); then
        cat >/dev/null
        printf '%s  would write:%s %s (mode %s)\n' "$C_DIM" "$C_RESET" "$path" "$mode"
        return
    fi
    install -m "$mode" /dev/null "$path"
    cat >"$path"
}

usage() {
    # Kopfkommentar ab Zeile 3 bis zur ersten Nicht-Kommentarzeile ausgeben --
    # robuster als feste Zeilennummern, die beim Editieren veralten.
    awk 'NR<3 {next} /^#/ {sub(/^# ?/, ""); print; next} {exit}' "$0"
    cat <<'USAGE'

Optionen:
  --base-dir PFAD        Zielverzeichnis      (Standard: /opt/trading)
  --user NAME            Service-Benutzer     (Standard: trader)
  --python PFAD          Python-Interpreter   (Standard: python3)
  --scout-repo URL       Arbitrage-Scout per git clone holen
  --screener-repo URL    Screener per git clone holen
  --no-scout             Arbitrage-Scout ueberspringen
  --no-screener          Screener ueberspringen (z. B. wenn er bereits
                          anderswo laeuft und eigene deploy/*.sh nutzt)
  --with-cron            Cron-Eintraege unter /etc/cron.d anlegen
  --with-listener        Telegram-Listener des Screeners als systemd-Dienst
  --all                  Wie --with-cron --with-listener
  --no-logrotate         Keine logrotate-Regel schreiben
  --no-system-packages   apt-Installation ueberspringen
  --dry-run              Nur anzeigen, was passieren wuerde
  -h, --help             Diese Hilfe
USAGE
}

# ---------------------------------------------------------------------------
# Argumente
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --base-dir)      BASE_DIR=${2:?Pfad fehlt}; shift 2 ;;
        --user)          SERVICE_USER=${2:?Name fehlt}; shift 2 ;;
        --python)        PYTHON_BIN=${2:?Pfad fehlt}; shift 2 ;;
        --scout-repo)    SCOUT_REPO=${2:?URL fehlt}; shift 2 ;;
        --screener-repo) SCREENER_REPO=${2:?URL fehlt}; shift 2 ;;
        --no-scout)      SETUP_SCOUT=0; shift ;;
        --no-screener)   SETUP_SCREENER=0; shift ;;
        --with-cron)     WITH_CRON=1; shift ;;
        --with-listener) WITH_LISTENER=1; shift ;;
        --all)           WITH_CRON=1; WITH_LISTENER=1; shift ;;
        --no-logrotate)  WITH_LOGROTATE=0; shift ;;
        --no-system-packages) INSTALL_SYSTEM_PACKAGES=0; shift ;;
        --dry-run)       DRY_RUN=1; shift ;;
        -h|--help)       usage; exit 0 ;;
        *)               die "Unbekannte Option: $1 (--help fuer die Uebersicht)" ;;
    esac
done

BASE_DIR="${BASE_DIR%/}"
IS_ROOT=0
[[ ${EUID:-$(id -u)} -eq 0 ]] && IS_ROOT=1

# ---------------------------------------------------------------------------
# Vorpruefungen
# ---------------------------------------------------------------------------
preflight() {
    log "Vorpruefungen"

    [[ "$(uname -s)" == "Linux" ]] || warn "Nicht-Linux erkannt ($(uname -s)) - ungetestet."

    command -v "$PYTHON_BIN" >/dev/null 2>&1 \
        || die "Python nicht gefunden: $PYTHON_BIN (--python setzen)"

    local version major minor
    version=$("$PYTHON_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
    major=${version%%.*}; minor=${version##*.}
    if (( major < 3 || (major == 3 && minor < MIN_PY_MINOR) )); then
        die "Python $version ist zu alt, benoetigt wird >= 3.$MIN_PY_MINOR"
    fi
    ok "Python $version ($(command -v "$PYTHON_BIN"))"

    if ! "$PYTHON_BIN" -c 'import venv' >/dev/null 2>&1; then
        if (( IS_ROOT && INSTALL_SYSTEM_PACKAGES )); then
            warn "Modul venv fehlt - wird ueber apt nachinstalliert."
        else
            die "Modul venv fehlt. Auf Debian/Ubuntu: apt-get install python3-venv"
        fi
    fi

    if (( ! IS_ROOT )); then
        warn "Kein root - Systempakete, Service-Benutzer, Cron und logrotate werden uebersprungen."
        INSTALL_SYSTEM_PACKAGES=0
        WITH_LOGROTATE=0
        if (( WITH_CRON )); then
            warn "--with-cron braucht root. Cron-Zeilen werden am Ende nur ausgegeben."
        fi
        if [[ "$BASE_DIR" == /opt/* ]] && [[ ! -w "$(dirname "$BASE_DIR")" ]]; then
            die "Kein Schreibrecht auf $BASE_DIR. Entweder mit sudo starten oder:
       bash $0 --base-dir \"\$HOME/trading\""
        fi
        SERVICE_USER="$(id -un)"
    fi

    # Freier Platz: zwei venvs mit pandas/numpy brauchen realistisch ~500 MB.
    local avail_mb
    avail_mb=$(df -Pm "$(dirname "$BASE_DIR")" 2>/dev/null | awk 'NR==2 {print $4}') || avail_mb=""
    if [[ -n "$avail_mb" ]] && (( avail_mb < 1024 )); then
        warn "Nur ${avail_mb} MB frei unter $(dirname "$BASE_DIR") - das kann fuer zwei venvs knapp werden."
    fi
}

# ---------------------------------------------------------------------------
# Systempakete
# ---------------------------------------------------------------------------
install_system_packages() {
    (( INSTALL_SYSTEM_PACKAGES )) || { skip "Systempakete uebersprungen"; return; }

    if ! command -v apt-get >/dev/null 2>&1; then
        warn "Kein apt-get gefunden. Bitte manuell sicherstellen: python3-venv, python3-pip, git, cron, tzdata."
        return
    fi

    log "Systempakete"
    local packages=(python3-venv python3-pip git cron tzdata ca-certificates curl logrotate sudo)
    local missing=()
    for pkg in "${packages[@]}"; do
        dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "ok installed" || missing+=("$pkg")
    done

    if (( ${#missing[@]} == 0 )); then
        ok "Alle benoetigten Pakete vorhanden"
        return
    fi

    log "Installiere: ${missing[*]}"
    run env DEBIAN_FRONTEND=noninteractive apt-get update -qq
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "${missing[@]}"
    ok "Pakete installiert"

    # Auf minimalen Images laeuft cron nach der Installation nicht automatisch.
    if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files 2>/dev/null | grep -q '^cron\.service'; then
        run systemctl enable --now cron
        ok "cron aktiviert"
    fi
}

# ---------------------------------------------------------------------------
# Service-Benutzer
# ---------------------------------------------------------------------------
create_service_user() {
    (( IS_ROOT )) || { skip "Service-Benutzer uebersprungen (kein root), nutze $SERVICE_USER"; return; }

    log "Service-Benutzer '$SERVICE_USER'"
    if id -u "$SERVICE_USER" >/dev/null 2>&1; then
        ok "existiert bereits"
        return
    fi

    # Systemkonto ohne Login-Shell: Cron braucht keine, ein kompromittierter
    # Dienst bekommt damit keine interaktive Sitzung.
    run useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    ok "angelegt (nologin; Wartung per: sudo -u $SERVICE_USER bash)"
}

# ---------------------------------------------------------------------------
# Ein Projekt einrichten
# ---------------------------------------------------------------------------
# setup_project <verzeichnisname> <klartextname> <repo-url-oder-leer> <einstiegsskript>
setup_project() {
    local name=$1 label=$2 repo=$3 entry=$4
    local dir="$BASE_DIR/$name"
    local venv="$dir/.venv"
    local py="$venv/bin/python"

    log "$label  ->  $dir"

    run mkdir -p "$dir"/{data,logs}

    # --- Code ---
    if [[ -n "$repo" ]]; then
        if [[ -d "$dir/.git" ]]; then
            step "Repository aktualisieren"
            run_as_service_user git -C "$dir" pull --ff-only
        else
            step "Repository klonen"
            # In ein bestehendes, nicht leeres Verzeichnis klonen: Umweg ueber
            # ein temporaeres Ziel, damit data/ und logs/ erhalten bleiben.
            local tmp
            tmp=$(mktemp -d)
            run git clone --depth 1 "$repo" "$tmp/code"
            run sh -c "cp -a '$tmp/code/.' '$dir/'"
            rm -rf "$tmp"   # nicht ueber run(): auch im Dry-Run wurde es real angelegt
        fi
        ok "  Code aus $repo"
    elif [[ -f "$dir/$entry" ]]; then
        ok "  Code vorhanden ($entry)"
    else
        warn "[$label] $entry fehlt in $dir - Code per scp/rsync nachlegen, dann dieses Skript erneut ausfuehren."
    fi

    # --- virtuelles Environment ---
    if [[ -x "$py" ]]; then
        ok "  venv vorhanden"
    else
        step "venv anlegen"
        run "$PYTHON_BIN" -m venv "$venv"
        ok "  venv angelegt"
    fi

    if (( ! DRY_RUN )); then
        step "pip/setuptools/wheel aktualisieren"
        "$py" -m pip install --quiet --upgrade pip setuptools wheel
    else
        printf '%s  would run:%s %s -m pip install --upgrade pip setuptools wheel\n' \
            "$C_DIM" "$C_RESET" "$py"
    fi

    # --- Abhaengigkeiten ---
    if [[ -f "$dir/requirements.txt" ]]; then
        step "Abhaengigkeiten aus requirements.txt"
        if (( DRY_RUN )); then
            printf '%s  would run:%s %s -m pip install -r %s\n' \
                "$C_DIM" "$C_RESET" "$py" "$dir/requirements.txt"
        else
            "$py" -m pip install --quiet --upgrade -r "$dir/requirements.txt"
            ok "  installiert"
        fi
    else
        # Bewusst nicht raten: eine falsch geratene Abhaengigkeitsliste ist
        # schlimmer als gar keine, weil sie spaeter still das Falsche zieht.
        write_file "$dir/requirements.txt" 644 <<'REQ'
# Abhaengigkeiten dieses Dienstes -- bitte ausfuellen.
#
# Danach vps_setup.sh erneut ausfuehren; die Installation laeuft dann
# automatisch in das venv dieses Verzeichnisses.
#
# Typisch fuer einen Pre-Market-Screener (auskommentiert, Versionen pruefen):
# pandas>=2.2
# numpy>=1.26
# requests>=2.32
# python-dotenv>=1.0
# pytz>=2024.1
REQ
        warn "[$label] Keine requirements.txt - Vorlage angelegt, es wurde nichts installiert."
    fi

    # --- mitgelieferte Skripte ausfuehrbar machen ---
    # Nach einem Clone kann das Exec-Bit fehlen; der Cron-Wrapper des Screeners
    # laeuft sonst nicht.
    if [[ -d "$dir/deploy" ]] && (( ! DRY_RUN )); then
        chmod +x "$dir"/deploy/*.sh 2>/dev/null || true
    fi

    # --- .env ---
    if [[ -f "$dir/.env" ]]; then
        ok "  .env vorhanden"
    elif [[ -f "$dir/.env.example" ]]; then
        run cp "$dir/.env.example" "$dir/.env"
        ok "  .env aus .env.example erzeugt - Werte noch eintragen"
    else
        write_file "$dir/.env" 600 <<'ENVTPL'
# Zugangsdaten dieses Dienstes. Diese Datei gehoert NICHT ins Repository.
# Beispiele -- nur eintragen, was tatsaechlich gebraucht wird:
# API_KEY=
# API_SECRET=
ENVTPL
        warn "[$label] Keine .env.example - leere .env angelegt, Werte eintragen."
    fi
    run chmod 600 "$dir/.env"

    # --- Rechte ---
    if (( IS_ROOT )); then
        run chown -R "$SERVICE_USER:$SERVICE_USER" "$dir"
        run chmod 750 "$dir"
        run chmod 700 "$dir/data" "$dir/logs"
    fi

    ok "$label eingerichtet"
}

# ---------------------------------------------------------------------------
# Cron
# ---------------------------------------------------------------------------
cron_line() {
    local schedule=$1 dir=$2 command=$3 logfile=$4
    printf '%s %s cd %s && %s >> %s 2>&1\n' \
        "$schedule" "$SERVICE_USER" "$dir" "$command" "$logfile"
}

install_cron() {
    if (( ! WITH_CRON )); then
        skip "Cron uebersprungen (--with-cron aktiviert es)"
        return
    fi
    if (( ! IS_ROOT )); then
        return  # Hinweis kommt in der Zusammenfassung
    fi

    log "Cron-Eintraege unter /etc/cron.d"

    # Ein PATH ist noetig: cron startet mit einer sehr sparsamen Umgebung.
    if (( SETUP_SCOUT )); then
        write_file /etc/cron.d/arbitrage-scout 644 <<CRONSCOUT
# Arbitrage Selling Scout -- von vps_setup.sh erzeugt
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
MAILTO=""
CRON_TZ=$SCOUT_CRON_TZ
$(cron_line "$SCOUT_CRON_TIME" "$BASE_DIR/$SCOUT_NAME" "$SCOUT_CRON_CMD" "$SCOUT_LOG")
CRONSCOUT
    else
        skip "Cron-Datei fuer Arbitrage Scout uebersprungen (--no-scout)"
    fi

    if (( SETUP_SCREENER )); then
        write_file /etc/cron.d/premarket-screener 644 <<CRONSCREENER
# Pre-Market Screener -- von vps_setup.sh erzeugt
# Aufgerufen wird der Wrapper des Projekts (deploy/run_scan.sh), nicht direkt
# main.py: der Wrapper setzt Universe, Export, Telegram-Versand und KI-Briefing.
# Damit bleibt die Aufrufsyntax im Screener-Repo und veraltet hier nicht.
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
MAILTO=""
CRON_TZ=$SCREENER_CRON_TZ
$(cron_line "$SCREENER_CRON_TIME" "$BASE_DIR/$SCREENER_NAME" "$SCREENER_CRON_CMD" "$SCREENER_LOG")
CRONSCREENER
    else
        skip "Cron-Datei fuer Pre-Market Screener uebersprungen (--no-screener)"
    fi

    ok "Cron-Dateien geschrieben"
}


# ---------------------------------------------------------------------------
# systemd: Telegram-Listener des Screeners
# ---------------------------------------------------------------------------
install_listener_service() {
    if (( ! SETUP_SCREENER )); then
        skip "Telegram-Listener uebersprungen (--no-screener)"
        return
    fi
    if (( ! WITH_LISTENER )); then
        skip "Telegram-Listener uebersprungen (--with-listener aktiviert ihn)"
        return
    fi
    if (( ! IS_ROOT )); then
        warn "--with-listener braucht root - Dienst wurde nicht eingerichtet."
        return
    fi
    if ! command -v systemctl >/dev/null 2>&1; then
        warn "Kein systemd gefunden - Telegram-Listener wurde nicht eingerichtet."
        return
    fi

    local dir="$BASE_DIR/$SCREENER_NAME"
    local template="$dir/$SCREENER_SERVICE_TEMPLATE"
    local unit="/etc/systemd/system/${SCREENER_SERVICE_NAME}.service"

    if [[ ! -f "$template" ]]; then
        warn "Vorlage $template fehlt - Telegram-Listener uebersprungen."
        return
    fi

    log "systemd-Dienst $SCREENER_SERVICE_NAME"

    # Die Vorlage bleibt die einzige Quelle der Wahrheit; hier werden nur die
    # beiden Platzhalter ersetzt. Der Unterschied zum projekteigenen Installer:
    # der setzt User=$(whoami), wir tragen den Service-Benutzer ein.
    if (( DRY_RUN )); then
        printf '%s  would write:%s %s (User=%s, WorkingDirectory=%s)\n' \
            "$C_DIM" "$C_RESET" "$unit" "$SERVICE_USER" "$dir"
    else
        sed -e "s#__VPS_USER__#${SERVICE_USER}#g" \
            -e "s#__PROJECT_DIR__#${dir}#g" \
            "$template" > "$unit"
        chmod 644 "$unit"
    fi

    run systemctl daemon-reload
    run systemctl enable "${SCREENER_SERVICE_NAME}.service"

    # Ohne befuellte .env startet der Listener sofort wieder durch. Lieber
    # aktiviert lassen und den Start dem Benutzer ueberlassen.
    if (( ! DRY_RUN )) && grep -qE '^TELEGRAM_BOT_TOKEN=.+' "$dir/.env" 2>/dev/null; then
        run systemctl restart "${SCREENER_SERVICE_NAME}.service"
        ok "Dienst laeuft (Status: systemctl status $SCREENER_SERVICE_NAME)"
    else
        warn "TELEGRAM_BOT_TOKEN in $dir/.env noch leer - Dienst ist aktiviert, "\
"aber nicht gestartet. Nach dem Eintragen: systemctl start $SCREENER_SERVICE_NAME"
    fi
}

# ---------------------------------------------------------------------------
# logrotate
# ---------------------------------------------------------------------------
install_logrotate() {
    (( WITH_LOGROTATE && IS_ROOT )) || { skip "logrotate uebersprungen"; return; }

    log "logrotate"
    # Bewusst nur cron.log: Der Scout rotiert seine scout.log per
    # RotatingFileHandler selbst und vergibt dabei ebenfalls die Endungen
    # .1 bis .5 -- logrotate parallel darauf loszulassen, brächte beide
    # Nummerierungen durcheinander. Hier geht es allein um das, was die
    # Dienste nach stdout/stderr schreiben und cron in die Datei umlenkt.
    # Schreibt der Screener eigene Logdateien ohne eigene Rotation, gehoeren
    # deren Namen hier ergaenzt.
    write_file /etc/logrotate.d/trading 644 <<ROTATE
$BASE_DIR/*/logs/cron.log $BASE_DIR/*/watchlist/cron.log {
    weekly
    rotate 8
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    su $SERVICE_USER $SERVICE_USER
}
ROTATE
    ok "/etc/logrotate.d/trading"
}

# ---------------------------------------------------------------------------
# Abschlusspruefung
# ---------------------------------------------------------------------------
verify() {
    log "Pruefung"
    (( DRY_RUN )) && { skip "im Dry-Run nichts zu pruefen"; return; }

    local name dir py checked=()
    (( SETUP_SCOUT ))    && checked+=("$SCOUT_NAME")
    (( SETUP_SCREENER )) && checked+=("$SCREENER_NAME")
    for name in "${checked[@]}"; do
        dir="$BASE_DIR/$name"
        py="$dir/.venv/bin/python"
        if [[ -x "$py" ]]; then
            ok "$name: $("$py" --version 2>&1), $( "$py" -m pip list --format=freeze 2>/dev/null | wc -l ) Pakete"
        else
            warn "$name: kein venv unter $py"
        fi
    done

    # Der Scout bringt eine eigene Konfigurationspruefung mit.
    local scout_dir="$BASE_DIR/$SCOUT_NAME"
    if (( SETUP_SCOUT )) && [[ -f "$scout_dir/scout.py" && -x "$scout_dir/.venv/bin/python" ]]; then
        log "scout.py --check-config"
        local runner=(env -C "$scout_dir" "$scout_dir/.venv/bin/python" scout.py --check-config)
        (( IS_ROOT )) && runner=(sudo -u "$SERVICE_USER" "${runner[@]}")
        if "${runner[@]}"; then
            ok "Scout-Konfiguration gueltig"
        else
            warn "scout.py --check-config meldet ein Problem (haeufig: GEMINI_API_KEY fehlt noch in .env)"
        fi
    fi
}

summary() {
    printf '\n%s============================================================%s\n' "$C_INFO" "$C_RESET"
    printf '  Einrichtung abgeschlossen%s\n' "$( (( DRY_RUN )) && printf ' (DRY-RUN, nichts veraendert)')"
    printf '%s============================================================%s\n\n' "$C_INFO" "$C_RESET"

    local services=""
    (( SETUP_SCOUT ))    && services="$SCOUT_NAME"
    (( SETUP_SCREENER )) && services="${services:+$services, }$SCREENER_NAME"

    printf '  Basis:     %s\n' "$BASE_DIR"
    printf '  Benutzer:  %s\n' "$SERVICE_USER"
    printf '  Dienste:   %s\n\n' "${services:-keine (--no-scout und --no-screener gesetzt)}"

    if (( ${#WARNINGS[@]} )); then
        printf '  %sOffene Punkte:%s\n' "$C_WARN" "$C_RESET"
        local w
        for w in "${WARNINGS[@]}"; do printf '    - %s\n' "$w"; done
        printf '\n'
    fi

    printf '  Naechste Schritte:\n\n'
    printf '    1. Secrets eintragen\n'
    if (( SETUP_SCOUT )); then
        printf '         sudoedit %s/%s/.env\n' "$BASE_DIR" "$SCOUT_NAME"
        printf '           GEMINI_API_KEY oder GROQ_API_KEY, je nach llm.provider\n'
    fi
    if (( SETUP_SCREENER )); then
        printf '         sudoedit %s/%s/.env\n' "$BASE_DIR" "$SCREENER_NAME"
        printf '           TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GROQ_API_KEY\n'
    fi

    printf '\n    2. Testlauf ohne Nebenwirkungen\n'
    if (( SETUP_SCOUT )); then
        printf '         sudo -u %s env -C %s/%s .venv/bin/python scout.py --dry-run\n' \
            "$SERVICE_USER" "$BASE_DIR" "$SCOUT_NAME"
    fi
    if (( SETUP_SCREENER )); then
        printf '         sudo -u %s env -C %s/%s .venv/bin/python main.py scan \\\n' \
            "$SERVICE_USER" "$BASE_DIR" "$SCREENER_NAME"
        printf '              --universe custom --tickers SAP.DE,SIE.DE --min-gap-pct 0.1\n'
    fi

    printf '\n  Betrieb:\n\n'
    (( SETUP_SCOUT ))    && printf '    Logs Scout     tail -f %s/%s/logs/*.log\n' "$BASE_DIR" "$SCOUT_NAME"
    (( SETUP_SCREENER )) && printf '    Logs Screener  tail -f %s/%s/watchlist/*.log\n' "$BASE_DIR" "$SCREENER_NAME"
    (( SETUP_SCREENER && WITH_LISTENER )) && printf '    Listener       systemctl status %s\n                   journalctl -u %s -f\n' \
        "$SCREENER_SERVICE_NAME" "$SCREENER_SERVICE_NAME"
    (( WITH_CRON )) && printf '    Cron-Status    systemctl status cron\n'
    printf '    Update         cd %s/<dienst> && sudo -u %s git pull\n' "$BASE_DIR" "$SERVICE_USER"
    printf '                   dann dieses Skript erneut ausfuehren\n'
    (( SETUP_SCREENER && WITH_LISTENER )) && printf '                   (Listener danach: systemctl restart %s)\n' "$SCREENER_SERVICE_NAME"

    if (( WITH_CRON && ! IS_ROOT )); then
        printf '\n  Cron braucht root. Diese Zeilen als root in /etc/cron.d ablegen:\n\n'
        if (( SETUP_SCOUT )); then
            printf '    # /etc/cron.d/arbitrage-scout\n    CRON_TZ=%s\n    %s\n\n' \
                "$SCOUT_CRON_TZ" "$(cron_line "$SCOUT_CRON_TIME" "$BASE_DIR/$SCOUT_NAME" "$SCOUT_CRON_CMD" "$SCOUT_LOG")"
        fi
        if (( SETUP_SCREENER )); then
            printf '    # /etc/cron.d/premarket-screener\n    CRON_TZ=%s\n    %s\n' \
                "$SCREENER_CRON_TZ" "$(cron_line "$SCREENER_CRON_TIME" "$BASE_DIR/$SCREENER_NAME" "$SCREENER_CRON_CMD" "$SCREENER_LOG")"
        fi
    elif (( WITH_CRON )); then
        printf '\n  Cron aktiv:\n'
        (( SETUP_SCOUT ))    && printf '    %-22s %s (%s)\n' "$SCOUT_NAME" "$SCOUT_CRON_TIME" "$SCOUT_CRON_TZ"
        (( SETUP_SCREENER )) && printf '    %-22s %s (%s)\n' "$SCREENER_NAME" "$SCREENER_CRON_TIME" "$SCREENER_CRON_TZ"
    fi
    printf '\n'
}

# ---------------------------------------------------------------------------
main() {
    printf '%sVPS-Einrichtung: Arbitrage Scout + Pre-Market Screener%s\n' "$C_INFO" "$C_RESET"
    (( DRY_RUN )) && printf '%sDRY-RUN: es wird nichts veraendert.%s\n' "$C_WARN" "$C_RESET"
    printf '\n'

    preflight
    install_system_packages
    create_service_user

    run mkdir -p "$BASE_DIR"
    (( IS_ROOT )) && run chown "$SERVICE_USER:$SERVICE_USER" "$BASE_DIR"

    if (( SETUP_SCOUT )); then
        setup_project "$SCOUT_NAME" "Arbitrage Selling Scout" "$SCOUT_REPO" "$SCOUT_ENTRY"
    else
        skip "Arbitrage Scout uebersprungen (--no-scout)"
    fi

    if (( SETUP_SCREENER )); then
        setup_project "$SCREENER_NAME" "Pre-Market Screener" "$SCREENER_REPO" "$SCREENER_ENTRY"
    else
        skip "Pre-Market Screener uebersprungen (--no-screener)"
    fi

    install_cron
    install_listener_service
    install_logrotate
    verify
    summary
}

main "$@"
