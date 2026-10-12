"""Job: build data/latest/universe.json from Dhan's instrument master.

On any problem (download fails, columns changed, count out of range, too many
unmatched underlyings) it falls back to the previous universe.json, re-applies
the current sectors.json, marks status "fallback" and raises an alert. If no
previous universe exists, the job fails rather than writing a bad list.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..dhan_client import DhanError, download_file
from ..market_clock import MarketCalendar, now_ist
from ..sectors import SectorMap
from ..storage import Storage
from ..universe import (UniverseBuild, UniverseError, build_from_file, resector,
                        stocks_from_document, universe_document, validate_build)

log = logging.getLogger(__name__)
JOB = "build_universe"


def run_build_universe(
    settings,
    storage: Storage,
    sector_map: SectorMap,
    calendar: MarketCalendar,
    now: datetime | None = None,
    csv_path: Path | None = None,
    downloader: Callable[[str, Path], Any] | None = None,
) -> dict:
    now = now or now_ist()
    trade_date = calendar.session_date_for(now)
    g = settings.get

    problems: list[str] = []
    build: UniverseBuild | None = None
    source_file = str(csv_path) if csv_path else g("dhan.instrument_master_url")
    try:
        if csv_path is None:
            dest = settings.data_dir / "cache" / "instrument_master_detailed.csv"
            (downloader or (lambda url, d: download_file(url, d)))(g("dhan.instrument_master_url"), dest)
            csv_path = dest
        build = build_from_file(csv_path, sector_map)
        problems = validate_build(build, int(g("universe.min_count", 150)), int(g("universe.max_count", 300)),
                                  float(g("universe.max_unmapped_ratio", 0.10)))
    except (DhanError, UniverseError, OSError) as exc:
        problems = [f"{type(exc).__name__}: {exc}"]

    if build is not None and not problems:
        doc = universe_document(build, trade_date, source_file)
        storage.write_dataset("universe", doc)
        alerts = []
        if build.unmapped_sector:
            alerts.append(f"{len(build.unmapped_sector)} F&O stocks have no sector in sectors.json: "
                          + ", ".join(build.unmapped_sector))
        if build.missing_equity:
            alerts.append(f"{len(build.missing_equity)} F&O underlyings have no NSE_EQ row: "
                          + ", ".join(build.missing_equity))
        not_fno = sorted(set(sector_map.by_symbol) - {s.symbol for s in build.stocks})
        if not_fno:
            alerts.append(f"{len(not_fno)} symbols in sectors.json are not in today's F&O universe "
                          "(ignored): " + ", ".join(not_fno))
        storage.record_job(JOB, "ok", f"{doc['count']} F&O stocks", alerts)
        log.info("Universe built: %d stocks (%d without sector)", doc["count"], len(build.unmapped_sector))
        return doc

    reason = "; ".join(problems)
    previous = storage.read_dataset("universe")
    if previous and previous.get("stocks"):
        stocks = resector(stocks_from_document(previous), sector_map)
        fb = UniverseBuild(stocks=stocks, fno_underlyings=len(stocks),
                           unmapped_sector=[s.symbol for s in stocks if s.sector == "Unmapped"])
        doc = universe_document(fb, trade_date, previous.get("source", {}).get("file", "previous universe.json"),
                                status="fallback",
                                status_reason=f"Rebuild failed, using universe from {previous.get('trade_date')}: {reason}")
        storage.write_dataset("universe", doc)
        storage.record_job(JOB, "fallback", doc["status_reason"], [doc["status_reason"]])
        log.error(doc["status_reason"])
        return doc

    storage.record_job(JOB, "failed", reason, [f"Universe build failed and no previous universe exists: {reason}"])
    raise UniverseError(f"Universe build failed and no previous universe exists: {reason}")
