#!/usr/bin/env python3
"""Arbitrage Selling Scout -- Orchestrator.

Ablauf je Angebotspaar:

1. **Vorfilter** (reine Arithmetik, kostenlos): Paare, die selbst im
   guenstigsten Fall die Schwellen reissen, werden verworfen, bevor ein
   LLM-Aufruf faellig wird. Das spart den Grossteil der API-Kosten.
2. **Semantischer Abgleich** (Gemini): Ist es dasselbe Produkt, dieselbe
   Variante, welches Gebinde?
3. **Endkalkulation** mit den vom Modell bestimmten Stueckzahlen.
4. **Report** auf Konsole/Log und optionaler Export nach CSV/JSON.

Aufruf::

    python scout.py --dry-run                 # ohne API-Aufrufe und ohne Export
    python scout.py --category kfz            # nur eine Kategorie
    python scout.py --limit 50 --min-roi 30   # Schwellen ad hoc anheben
    python scout.py --check-config            # nur Konfiguration pruefen

Cronjob (taeglich 06:15, Ausgabe landet im rotierenden Logfile)::

    15 6 * * * cd /opt/arbitrage-scout && /opt/arbitrage-scout/.venv/bin/python scout.py >/dev/null 2>&1
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from typing import Any, Iterable

from pydantic import BaseModel

from amazon_client import AmazonError
from calculator import MarginBreakdown, MarginCalculator, format_breakdown
from config import ConfigError, Settings, load_settings
from discovery import Discoverer, DiscoveryError, HotItem, amazon_pairs
from matcher import (
    MatchDecision,
    MatcherError,
    ProductMatcher,
    guess_package_quantity,
)
from logging_utils import RUN_ID, get_logger, setup_logging
from models import CandidatePair
from notify import send_discovery_summary, send_run_summary
from sources import (
    OfferSource,
    SourceError,
    build_amazon_client,
    build_ebay_client,
    build_source,
    write_example_csv,
    write_purchase_example,
)

logger = get_logger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_INTERRUPTED = 130

# Wird von SIGTERM/SIGINT gesetzt; die Hauptschleife bricht dann sauber ab,
# statt mitten in einem API-Aufruf zu sterben.
_shutdown_requested = False


def _request_shutdown(signum: int, _frame: FrameType | None) -> None:
    global _shutdown_requested
    _shutdown_requested = True
    logger.warning("Signal %s empfangen - beende nach dem laufenden Paar.", signum)


class ScoutResult(BaseModel):
    """Ein vollstaendig geprueftes Angebotspaar."""

    pair_id: str
    category: str
    keyword: str | None = None

    source_title: str
    source_marketplace: str
    source_price_eur: float
    source_url: str | None = None

    target_title: str
    target_marketplace: str
    target_price_eur: float
    target_url: str | None = None

    is_match: bool
    match_accepted: bool
    confidence: float
    mismatch_reason: str | None = None
    package_quantity_source: int
    package_quantity_target: int
    match_model: str

    net_profit_eur: float
    roi_percent: float
    is_recommended: bool
    rejection_reasons: list[str] = []
    breakdown: MarginBreakdown | None = None

    checked_at: datetime

    def flat_row(self) -> dict[str, Any]:
        """Flache Zeile fuer den CSV-Export (ohne verschachtelte Aufschluesselung)."""
        data = self.model_dump(exclude={"breakdown"})
        data["rejection_reasons"] = "; ".join(self.rejection_reasons)
        data["checked_at"] = self.checked_at.isoformat()
        return data


class Scout:
    """Fuehrt Quelle, Matcher und Rechner zusammen."""

    def __init__(
        self,
        settings: Settings,
        *,
        dry_run: bool = False,
        offline: bool = False,
        use_prefilter: bool = True,
        provider_name: str | None = None,
    ) -> None:
        self.settings = settings
        self.dry_run = dry_run
        self.calculator = MarginCalculator.from_settings(settings)
        self.source: OfferSource = build_source(settings)
        # Im Dry-Run wird grundsaetzlich nicht gegen die API gesprochen.
        self.matcher = ProductMatcher.from_settings(
            settings, offline=offline or dry_run, provider_name=provider_name
        )
        self.use_prefilter = use_prefilter
        self.stats = {
            "seen": 0,
            "prefiltered": 0,
            "matched": 0,
            "rejected_match": 0,
            "recommended": 0,
            "errors": 0,
        }

    # -- Vorfilter ------------------------------------------------------------
    def _passes_prefilter(self, pair: CandidatePair) -> bool:
        """Kostenlose Vorkalkulation, bevor ein LLM-Aufruf faellig wird.

        Zwei Vorsichtsmassnahmen, damit der Filter keine echten Chancen wegwirft:

        * Die Gebindegroessen werden aus den Titeln geschaetzt. Ohne das wuerde
          ein 6er-Pack gegen ein Einzelstueck 1:1 gerechnet -- und ausgerechnet
          der lukrativste Fall floege raus.
        * Verworfen wird erst deutlich unterhalb der echten Schwellen
          (``prefilter_margin_factor``), weil die Schaetzung eben eine ist.
        """
        if not self.use_prefilter:
            return True

        qty_source = pair.source.package_quantity or guess_package_quantity(pair.source.title)
        qty_target = pair.target.package_quantity or guess_package_quantity(pair.target.title)

        try:
            estimate = self.calculator.evaluate_pair(
                pair, quantity_source=qty_source, quantity_target=qty_target
            )
        except Exception as exc:
            # Im Zweifel durchlassen -- ein unnoetiger API-Aufruf ist billiger
            # als eine uebersehene Gelegenheit.
            logger.debug("Vorfilter uebersprungen fuer %s: %s", pair.pair_id, exc)
            return True

        factor = self.settings.config.search.prefilter_margin_factor
        margin = self.settings.config.margin
        if (
            estimate.net_profit_eur >= margin.min_profit_eur * factor
            and estimate.roi_percent >= margin.min_roi_percent * factor
        ):
            return True

        self.stats["prefiltered"] += 1
        logger.debug(
            "Vorfilter verwirft %s: Schaetzung %.2f EUR / ROI %.1f %% bei Gebinde %d:%d",
            pair.pair_id,
            estimate.net_profit_eur,
            estimate.roi_percent,
            qty_source,
            qty_target,
        )
        return False

    # -- Hauptschleife --------------------------------------------------------
    def run(self, category_name: str | None = None, limit: int | None = None) -> list[ScoutResult]:
        cfg = self.settings.config
        category = None
        if category_name:
            category = cfg.search.category(category_name)
            if category is None:
                known = ", ".join(c.name for c in cfg.search.categories) or "keine"
                raise ConfigError(
                    f"Kategorie '{category_name}' ist nicht konfiguriert. Bekannt: {known}"
                )

        max_items = limit or cfg.search.max_candidates_per_run
        results: list[ScoutResult] = []
        started = time.monotonic()

        logger.info(
            "Lauf %s gestartet | Kategorie: %s | Limit: %d | Modus: %s | Backend: %s",
            RUN_ID,
            category.name if category else "alle",
            max_items,
            "DRY-RUN" if self.dry_run else "live",
            self.matcher.model_label,
        )

        try:
            pairs: Iterable[CandidatePair] = self.source.fetch_pairs(category)
        except SourceError as exc:
            logger.error("Datenquelle nicht verfuegbar: %s", exc)
            raise

        iterator = iter(pairs)
        while True:
            # Abbruch und Limit VOR dem naechsten Abruf pruefen: bei der
            # API-Quelle kostet jedes weitere Paar einen eBay-Aufruf.
            if _shutdown_requested:
                logger.warning("Abbruch angefordert - Lauf wird beendet.")
                break
            if self.stats["seen"] >= max_items:
                logger.info("Limit von %d Kandidaten erreicht.", max_items)
                break

            # Quellen sind Generatoren: Fehler kommen erst beim Abruf, nicht
            # beim Anlegen. Faellt die Quelle mitten im Lauf aus (z. B. Token
            # entzogen), bleiben die bisherigen Ergebnisse erhalten.
            try:
                pair = next(iterator)
            except StopIteration:
                break
            except SourceError as exc:
                if self.stats["seen"] == 0:
                    logger.error("Datenquelle nicht verfuegbar: %s", exc)
                    raise
                self.stats["errors"] += 1
                logger.error("Datenquelle waehrend des Laufs ausgefallen, Abbruch: %s", exc)
                break

            self.stats["seen"] += 1
            try:
                result = self._process(pair)
            except MatcherError as exc:
                self.stats["errors"] += 1
                logger.error("Paar %s uebersprungen: %s", pair.pair_id, exc)
                continue
            except Exception:
                self.stats["errors"] += 1
                logger.exception("Unerwarteter Fehler bei %s", pair.pair_id)
                continue

            if result is not None:
                results.append(result)

        duration = time.monotonic() - started
        results.sort(key=lambda r: r.net_profit_eur, reverse=True)
        self._log_summary(duration)
        return results

    def _process(self, pair: CandidatePair) -> ScoutResult | None:
        """Ein Paar durch Vorfilter, Matcher und Endkalkulation schicken."""
        if not self._passes_prefilter(pair):
            return None

        decision: MatchDecision = self.matcher.match(pair)

        if not decision.accepted:
            self.stats["rejected_match"] += 1
            logger.info("Kein Match: %s | %s", pair.source.short(), decision.reason)
            return self._build_result(pair, decision, breakdown=None)

        self.stats["matched"] += 1
        breakdown = self.calculator.evaluate_pair(
            pair,
            quantity_source=decision.result.package_quantity_source,
            quantity_target=decision.result.package_quantity_target,
        )

        if breakdown.is_recommended:
            self.stats["recommended"] += 1
            logger.info("TREFFER %s", breakdown.summary())
            logger.info("  Einkauf: %s", pair.source.short())
            logger.info("  Verkauf: %s", pair.target.short())
            logger.debug("\n%s", format_breakdown(breakdown))
        else:
            logger.info(
                "Match, aber unrentabel: %s | %s",
                pair.source.title[:60],
                "; ".join(breakdown.rejection_reasons),
            )

        return self._build_result(pair, decision, breakdown)

    @staticmethod
    def _build_result(
        pair: CandidatePair, decision: MatchDecision, breakdown: MarginBreakdown | None
    ) -> ScoutResult:
        r = decision.result
        return ScoutResult(
            pair_id=pair.pair_id,
            category=pair.category,
            keyword=pair.keyword,
            source_title=pair.source.title,
            source_marketplace=pair.source.marketplace.value,
            source_price_eur=pair.source.landed_price_eur,
            source_url=str(pair.source.url) if pair.source.url else None,
            target_title=pair.target.title,
            target_marketplace=pair.target.marketplace.value,
            target_price_eur=pair.target.landed_price_eur,
            target_url=str(pair.target.url) if pair.target.url else None,
            is_match=r.is_match,
            match_accepted=decision.accepted,
            confidence=r.confidence,
            mismatch_reason=r.mismatch_reason,
            package_quantity_source=r.package_quantity_source,
            package_quantity_target=r.package_quantity_target,
            match_model=decision.model,
            net_profit_eur=breakdown.net_profit_eur if breakdown else 0.0,
            roi_percent=breakdown.roi_percent if breakdown else 0.0,
            is_recommended=breakdown.is_recommended if breakdown else False,
            rejection_reasons=breakdown.rejection_reasons if breakdown else [decision.reason],
            breakdown=breakdown,
            checked_at=datetime.now(timezone.utc),
        )

    def _log_summary(self, duration: float) -> None:
        logger.info(
            "Lauf beendet in %.1f s | geprueft: %d | vorgefiltert: %d | Match: %d | "
            "kein Match: %d | Empfehlungen: %d | Fehler: %d",
            duration,
            self.stats["seen"],
            self.stats["prefiltered"],
            self.stats["matched"],
            self.stats["rejected_match"],
            self.stats["recommended"],
            self.stats["errors"],
        )
        self.matcher.log_stats()


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def export_results(settings: Settings, results: list[ScoutResult]) -> list[Path]:
    """Ergebnisse nach CSV und/oder JSON schreiben."""
    cfg = settings.config.output
    if cfg.only_recommended:
        results = [r for r in results if r.is_recommended]
    if not results:
        logger.info("Nichts zu exportieren.")
        return []

    out_dir = settings.results_dir
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("Ausgabeverzeichnis %s nicht anlegbar: %s", out_dir, exc)
        return []

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    written: list[Path] = []

    if cfg.write_csv:
        path = out_dir / f"results-{stamp}.csv"
        try:
            rows = [r.flat_row() for r in results]
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)
            written.append(path)
        except OSError as exc:
            logger.error("CSV-Export fehlgeschlagen: %s", exc)

    if cfg.write_json:
        path = out_dir / f"results-{stamp}.json"
        try:
            payload = {
                "run_id": RUN_ID,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "count": len(results),
                "results": [json.loads(r.model_dump_json()) for r in results],
            }
            path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            written.append(path)
        except OSError as exc:
            logger.error("JSON-Export fehlgeschlagen: %s", exc)

    for path in written:
        logger.info("Geschrieben: %s", path)
    return written


def print_report(results: list[ScoutResult], *, verbose: bool = False) -> None:
    """Kurzreport auf stdout -- fuer den interaktiven Aufruf."""
    recommended = [r for r in results if r.is_recommended]
    print()
    print("=" * 78)
    print(f"  ARBITRAGE SCOUT | Lauf {RUN_ID} | {len(recommended)} von {len(results)} empfohlen")
    print("=" * 78)

    if not recommended:
        print("\n  Keine Kaufempfehlung in diesem Lauf.")
        if results:
            best = max(results, key=lambda r: r.net_profit_eur)
            print(
                f"  Bester Kandidat: {best.source_title[:50]} "
                f"({best.net_profit_eur:+.2f} EUR, ROI {best.roi_percent:.1f} %)"
            )
        print()
        return

    for idx, r in enumerate(recommended, start=1):
        print(f"\n  [{idx}] {r.target_title[:64]}")
        print(f"      Kategorie   {r.category}")
        print(f"      Einkauf     {r.source_price_eur:>8.2f} EUR  ({r.source_marketplace})")
        print(f"      Verkauf     {r.target_price_eur:>8.2f} EUR  ({r.target_marketplace})")
        print(f"      Gewinn      {r.net_profit_eur:>8.2f} EUR   ROI {r.roi_percent:.1f} %")
        print(
            f"      Gebinde     Quelle {r.package_quantity_source} / "
            f"Ziel {r.package_quantity_target}   Confidence {r.confidence:.2f}"
        )
        if r.source_url:
            print(f"      Einkauf-URL {r.source_url}")
        if r.target_url:
            print(f"      Verkauf-URL {r.target_url}")
        if verbose and r.breakdown:
            print(format_breakdown(r.breakdown))
    print()


# ---------------------------------------------------------------------------
# Produktfindung (--discover)
# ---------------------------------------------------------------------------
def export_discovery(settings: Settings, items: list[HotItem], deals: list[ScoutResult]) -> list[Path]:
    """Fundliste nach CSV und/oder JSON schreiben."""
    cfg = settings.config.output
    if not items:
        logger.info("Nichts zu exportieren.")
        return []
    out_dir = settings.results_dir
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("Ausgabeverzeichnis %s nicht anlegbar: %s", out_dir, exc)
        return []

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    written: list[Path] = []

    if cfg.write_csv:
        path = out_dir / f"discovery-{stamp}.csv"
        fields = [
            "rank", "title", "price_eur", "shipping_eur", "sold_quantity", "days_listed",
            "sold_per_day", "competing_listings", "lowest_competitor_price_eur",
            "max_purchase_price_eur", "max_purchase_at_lowest_price_eur",
            "gtin", "brand", "mpn", "category", "keyword", "origin",
            "url", "link_amazon", "link_idealo", "link_google_shopping", "link_aliexpress",
        ]
        try:
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for rank, item in enumerate(items, start=1):
                    row = item.model_dump(include=set(fields))
                    row.update(
                        rank=rank,
                        link_amazon=item.research_links.get("Amazon", ""),
                        link_idealo=item.research_links.get("idealo", ""),
                        link_google_shopping=item.research_links.get("Google Shopping", ""),
                        link_aliexpress=item.research_links.get("AliExpress", ""),
                    )
                    writer.writerow(row)
            written.append(path)
        except OSError as exc:
            logger.error("CSV-Export fehlgeschlagen: %s", exc)

    if cfg.write_json:
        path = out_dir / f"discovery-{stamp}.json"
        try:
            payload = {
                "run_id": RUN_ID,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "count": len(items),
                "items": [json.loads(i.model_dump_json()) for i in items],
                "amazon_deals": [json.loads(d.model_dump_json()) for d in deals],
            }
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            written.append(path)
        except OSError as exc:
            logger.error("JSON-Export fehlgeschlagen: %s", exc)

    for path in written:
        logger.info("Geschrieben: %s", path)
    return written


def print_discovery_report(items: list[HotItem], stats: dict[str, int]) -> None:
    """Fundliste auf stdout."""
    print()
    print("=" * 78)
    print(f"  PRODUKTFINDUNG | Lauf {RUN_ID} | {len(items)} gut laufende Artikel")
    print("=" * 78)
    print(
        f"  {stats.get('keywords', 0)} Suchbegriffe, {stats.get('detail_lookups', 0)} Angebote "
        f"im Detail, {stats.get('api_calls', 0)} eBay-Aufrufe | aussortiert: "
        f"{stats.get('dropped_low_sales', 0)} zu wenig Verkaeufe, "
        f"{stats.get('dropped_unprofitable', 0)} Preis traegt die Kosten nicht"
    )
    if not items:
        print("\n  Keine Artikel, die die Kriterien erfuellen.\n")
        return

    for idx, item in enumerate(items, start=1):
        tag = "  [eBay-Bestseller-Liste]" if item.origin == "bestseller" else ""
        print(f"\n  [{idx}] {item.title[:66]}{tag}")
        shipping = f" + {item.shipping_eur:.2f} Versand" if item.shipping_eur else ""
        print(f"      Verkauf      {item.price_eur:>8.2f} EUR{shipping}")
        if item.sold_per_day is not None and item.days_listed is not None:
            print(
                f"      Verkauft     {item.sold_quantity} Stk. in {item.days_listed:.0f} Tagen "
                f"(~{item.sold_per_day:.1f}/Tag, Schaetzwert)"
            )
        else:
            print(f"      Verkauft     {item.sold_quantity} Stk. (Angebotsalter unbekannt)")
        if item.competing_listings > 1 and item.lowest_competitor_price_eur is not None:
            print(
                f"      Konkurrenz   {item.competing_listings} Angebote, "
                f"guenstigstes {item.lowest_competitor_price_eur:.2f} EUR"
            )
        print(f"      Max. Einkauf {item.max_purchase_price_eur:>8.2f} EUR  (inkl. Versand zu dir, Gebinde 1:1)")
        if item.max_purchase_at_lowest_price_eur is not None:
            if item.max_purchase_at_lowest_price_eur > 0:
                print(
                    f"                   {item.max_purchase_at_lowest_price_eur:>8.2f} EUR  "
                    "wenn du mit dem guenstigsten Anbieter gleichziehst"
                )
            else:
                print("                   zum Preis des guenstigsten Anbieters nicht rentabel")
        ident = "  ".join(
            f"{label} {value}" for label, value in
            (("EAN", item.gtin), ("Marke", item.brand), ("MPN", item.mpn)) if value
        )
        if ident:
            print(f"      {ident}")
        if item.url:
            print(f"      eBay         {item.url}")
        for label, link in item.research_links.items():
            print(f"      {label + ':':<16} {link}")
    print()
    print("  Die Stueckzahl ist eBays Schaetzung fuer dieses eine Angebot seit seiner")
    print("  Erstellung -- ein Tempo-Hinweis, kein Marktvolumen. Preise sind Angebots-,")
    print("  keine erzielten Verkaufspreise.")
    print()


def _check_amazon(settings: Settings, args: argparse.Namespace, items: list[HotItem]) -> list[ScoutResult]:
    """Nur mit Creators-API-Zugang: zu jedem Fund Amazon-Angebote suchen und
    durch die normale Pruefung (Vorfilter, Matcher, Kalkulation) schicken."""
    try:
        amazon = build_amazon_client(settings)
        scout = Scout(
            settings,
            dry_run=args.dry_run,
            offline=args.offline,
            use_prefilter=not args.no_prefilter,
            provider_name=args.provider,
        )
    except (SourceError, MatcherError) as exc:
        logger.warning("Amazon-Gegenpruefung uebersprungen: %s", exc)
        return []

    limit = settings.config.discovery.amazon_candidates_per_item
    deals: list[ScoutResult] = []
    for item in items:
        if _shutdown_requested:
            break
        try:
            pairs = amazon_pairs(item, amazon, limit)
        except AmazonError as exc:
            if exc.fatal:
                logger.error("Amazon-Gegenpruefung abgebrochen: %s", exc)
                break
            logger.warning("Amazon-Suche zu '%s' uebersprungen: %s", item.title[:50], exc)
            continue
        for pair in pairs:
            try:
                result = scout._process(pair)
            except Exception as exc:
                logger.error("Paar %s uebersprungen: %s", pair.pair_id, exc)
                continue
            if result is not None:
                deals.append(result)
    scout.matcher.log_stats()
    deals.sort(key=lambda r: r.net_profit_eur, reverse=True)
    return deals


def _execute_discovery(settings: Settings, args: argparse.Namespace) -> int:
    cfg = settings.config
    categories = cfg.search.categories
    if args.category:
        category = cfg.search.category(args.category)
        if category is None:
            known = ", ".join(c.name for c in cfg.search.categories) or "keine"
            logger.error("Kategorie '%s' ist nicht konfiguriert. Bekannt: %s", args.category, known)
            return EXIT_CONFIG
        categories = [category]
    if not categories:
        logger.error(
            "Produktfindung braucht Suchbegriffe: search.categories in config.yaml ist leer."
        )
        return EXIT_CONFIG
    if args.limit:
        cfg.discovery.max_results = min(args.limit, 200)

    try:
        ebay = build_ebay_client(settings)
    except SourceError as exc:
        logger.error("Produktfindung braucht eBay-Zugangsdaten: %s", exc)
        return EXIT_CONFIG

    discoverer = Discoverer(ebay, cfg.discovery, MarginCalculator.from_settings(settings))
    logger.info(
        "Produktfindung %s gestartet | Kategorien: %s | Budget: %d eBay-Aufrufe",
        RUN_ID, ", ".join(c.name for c in categories) or "keine", cfg.discovery.max_api_calls,
    )
    try:
        items = discoverer.run(categories)
    except DiscoveryError as exc:
        logger.error("Produktfindung abgebrochen: %s", exc)
        return EXIT_ERROR

    stats = discoverer.stats
    logger.info(
        "Produktfindung beendet | Funde: %d | Details: %d | eBay-Aufrufe: %d | "
        "zu wenig Verkaeufe: %d | unrentabel: %d",
        len(items), stats["detail_lookups"], stats["api_calls"],
        stats["dropped_low_sales"], stats["dropped_unprofitable"],
    )

    deals: list[ScoutResult] = []
    if items and settings.secrets.has_amazon:
        deals = _check_amazon(settings, args, items)

    if not args.no_export and not args.dry_run:
        export_discovery(settings, items, deals)

    if args.notify_telegram:
        sent = send_discovery_summary(settings, items, deals, stats, run_id=RUN_ID)
        logger.info("Telegram-Benachrichtigung %s.", "gesendet" if sent else "nicht gesendet")

    if not args.quiet:
        print_discovery_report(items, stats)
        if deals:
            print("  Gegenpruefung auf Amazon:")
            print_report(deals, verbose=args.verbose)
        elif items and not settings.secrets.has_amazon:
            print("  Einkaufsquellen: ohne Amazon-Creators-Zugang erfolgt keine automatische")
            print("  Gegenpruefung. Angebot ueber die Links suchen, in data/purchases.csv")
            print("  eintragen -- der normale Lauf (sources.provider: api) prueft es dann.")
            print()

    return EXIT_INTERRUPTED if _shutdown_requested else EXIT_OK


# ---------------------------------------------------------------------------
# Lockfile -- verhindert ueberlappende Cron-Laeufe
# ---------------------------------------------------------------------------
class RunLock:
    """Einfacher PID-Lock. Ein zweiter Lauf beendet sich, statt doppelt zu kaufen."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def __enter__(self) -> RunLock:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self._fd, str(os.getpid()).encode())
        except FileExistsError:
            stale = self._is_stale()
            if stale:
                logger.warning("Verwaister Lock %s wird entfernt.", self.path)
                self.path.unlink(missing_ok=True)
                return self.__enter__()
            raise RuntimeError(
                f"Ein anderer Lauf ist aktiv (Lock: {self.path}). "
                "Bei Bedarf mit --no-lock ueberspringen."
            ) from None
        except OSError as exc:
            logger.warning("Lock nicht setzbar (%s): %s - Lauf ohne Lock.", self.path, exc)
        return self

    def _is_stale(self) -> bool:
        """Lock ohne laufenden Prozess dahinter gilt als verwaist."""
        try:
            pid = int(self.path.read_text().strip())
        except (OSError, ValueError):
            return True
        if pid == os.getpid():
            return True
        try:
            os.kill(pid, 0)  # Signal 0 = nur Existenzpruefung
        except OSError:
            return True
        except AttributeError:  # pragma: no cover - Plattformen ohne os.kill
            return False
        return False

    def __exit__(self, *_exc: object) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
        self.path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scout",
        description="Arbitrage-Scout: Amazon/AliExpress gegen eBay pruefen.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Beispiele:\n"
            "  python scout.py --dry-run\n"
            "  python scout.py --category kfz --limit 25 -v\n"
            "  python scout.py --min-roi 35 --min-profit 20\n"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Kein API-Aufruf, kein Export. Prueft die Pipeline heuristisch.",
    )
    parser.add_argument(
        "--category",
        metavar="NAME",
        help="Nur diese Kategorie aus config.yaml pruefen.",
    )
    parser.add_argument(
        "--limit", type=int, metavar="N", help="Maximale Anzahl Kandidaten in diesem Lauf."
    )
    parser.add_argument(
        "--config", metavar="PFAD", help="Alternative Konfigurationsdatei (Default: config.yaml)."
    )
    parser.add_argument(
        "--min-roi", type=float, metavar="PROZENT", help="Mindest-ROI ueberschreiben."
    )
    parser.add_argument(
        "--min-profit", type=float, metavar="EUR", help="Mindestgewinn ueberschreiben."
    )
    parser.add_argument(
        "--provider",
        choices=["gemini", "groq"],
        help="LLM-Backend fuer diesen Lauf (Standard: llm.provider aus config.yaml).",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Heuristisch matchen statt ein LLM zu befragen (Export bleibt aktiv).",
    )
    parser.add_argument(
        "--no-prefilter",
        action="store_true",
        help="Jedes Paar an Gemini schicken, auch offensichtlich unrentable.",
    )
    parser.add_argument(
        "--no-export", action="store_true", help="Ergebnisse nicht auf die Platte schreiben."
    )
    parser.add_argument(
        "--notify-telegram",
        action="store_true",
        help="Zusammenfassung nach Lauf-Ende an TELEGRAM_CHAT_ID senden (auch bei 0 Treffern).",
    )
    parser.add_argument("--no-lock", action="store_true", help="Ohne Lockfile laufen.")
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Loglevel fuer diesen Lauf ueberschreiben.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Vollstaendige Kostenaufschluesselung ausgeben."
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="Keinen Report auf stdout.")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Konfiguration laden, pruefen, ausgeben und beenden.",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Produktfindung: gut laufende eBay-Artikel suchen und den hoechsten "
             "noch lohnenden Einkaufspreis ausgeben (braucht EBAY_APP_ID/EBAY_CERT_ID).",
    )
    parser.add_argument(
        "--write-example-data",
        action="store_true",
        help="Beispiel-CSV nach data/offers.example.csv schreiben und beenden.",
    )
    return parser


def _apply_overrides(settings: Settings, args: argparse.Namespace) -> None:
    """CLI-Flags haben Vorrang vor der YAML-Konfiguration."""
    if args.min_roi is not None:
        settings.config.margin.min_roi_percent = args.min_roi
    if args.min_profit is not None:
        settings.config.margin.min_profit_eur = args.min_profit
    if args.provider:
        settings.config.llm.provider = args.provider  # type: ignore[assignment]
    if args.log_level:
        settings.config.logging.level = args.log_level  # type: ignore[assignment]
    if args.dry_run:
        settings.config.output.write_csv = False
        settings.config.output.write_json = False


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    _apply_overrides(settings, args)
    setup_logging(settings, force=True)

    if args.check_config:
        cfg = settings.config
        print(f"Konfiguration:  {settings.config_path}")
        print(f"Logdatei:       {settings.log_file}")
        print(f"Ergebnisse:     {settings.results_dir}")
        print(f"Datenquelle:    {cfg.sources.provider}")
        if cfg.sources.provider == "api":
            api_cfg = cfg.sources.api
            print(f"  Einkauf:      {api_cfg.purchase_source}"
                  + (f" ({settings.resolve(api_cfg.purchase_csv_path)})"
                     if api_cfg.purchase_source == "csv" else f" ({api_cfg.amazon_country})"))
            print(f"  Verkauf:      eBay {api_cfg.ebay_marketplace}")
            print(f"  eBay-Keys:    {'gesetzt' if settings.secrets.has_ebay else 'FEHLEN (EBAY_APP_ID/EBAY_CERT_ID)'}")
            if api_cfg.purchase_source == "amazon":
                print(f"  Amazon-Keys:  {'gesetzt' if settings.secrets.has_amazon else 'FEHLEN (AMAZON_CREATORS_*, AMAZON_PARTNER_TAG)'}")
        provider = cfg.llm.provider
        model = cfg.groq.model if provider == "groq" else cfg.gemini.model
        print(f"LLM-Provider:   {provider} ({model})")
        print(f"GEMINI_API_KEY: {'gesetzt' if settings.secrets.has_gemini_key else 'fehlt'}")
        print(f"GROQ_API_KEY:   {'gesetzt' if settings.secrets.has_groq_key else 'fehlt'}")
        if not settings.secrets.has_key(provider):
            print(f"  ! Kein Key fuer '{provider}' - Laeufe fallen auf die Heuristik zurueck.")
        telegram_state = "gesetzt" if settings.secrets.has_telegram else "fehlt (optional)"
        print(f"Telegram:       {telegram_state} (fuer --notify-telegram und listener.py)")
        print(f"Schwellen:      ROI >= {cfg.margin.min_roi_percent} %, "
              f"Gewinn >= {cfg.margin.min_profit_eur} EUR")
        print(f"Kategorien:     {', '.join(c.name for c in cfg.search.categories) or 'keine'}")
        print("Konfiguration ist gueltig.")
        return EXIT_OK

    if args.write_example_data:
        path = write_example_csv(settings.resolve("data/offers.example.csv"))
        print(f"Beispieldaten geschrieben: {path}")
        path = write_purchase_example(settings.resolve("data/purchases.example.csv"))
        print(f"Vorlage Einkaufsliste:     {path}")
        return EXIT_OK

    signal.signal(signal.SIGINT, _request_shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _request_shutdown)

    lock_path = settings.resolve("logs/scout.lock")
    try:
        runner = _execute_discovery if args.discover else _execute
        if args.no_lock:
            return runner(settings, args)
        with RunLock(lock_path):
            return runner(settings, args)
    except RuntimeError as exc:  # Lock belegt
        logger.error("%s", exc)
        return EXIT_ERROR
    except KeyboardInterrupt:
        logger.warning("Durch Benutzer abgebrochen.")
        return EXIT_INTERRUPTED


def _execute(settings: Settings, args: argparse.Namespace) -> int:
    try:
        scout = Scout(
            settings,
            dry_run=args.dry_run,
            offline=args.offline,
            use_prefilter=not args.no_prefilter,
            provider_name=args.provider,
        )
    except MatcherError as exc:
        logger.error("Matcher nicht initialisierbar: %s", exc)
        return EXIT_CONFIG
    except SourceError as exc:
        # Fehlende Marktplatz-Keys o. ae. -- ein Konfigurations-, kein Laufzeitfehler.
        logger.error("Datenquelle nicht initialisierbar: %s", exc)
        return EXIT_CONFIG

    try:
        results = scout.run(category_name=args.category, limit=args.limit)
    except ConfigError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG
    except SourceError as exc:
        logger.error("%s", exc)
        return EXIT_ERROR
    except Exception:
        logger.exception("Lauf abgebrochen.")
        return EXIT_ERROR

    if not args.no_export and not args.dry_run:
        export_results(settings, results)
    elif args.dry_run:
        logger.info("Dry-Run: Export uebersprungen.")

    if args.notify_telegram:
        sent = send_run_summary(
            settings, results, scout.stats, run_id=RUN_ID, offline=scout.matcher.offline
        )
        logger.info("Telegram-Benachrichtigung %s.", "gesendet" if sent else "nicht gesendet")

    if not args.quiet:
        print_report(results, verbose=args.verbose)

    if _shutdown_requested:
        return EXIT_INTERRUPTED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
