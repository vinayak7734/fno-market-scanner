"""Job: build data/latest/premarket.json from NSE's pre-open session.

For each F&O stock:
  preopen_price   NSE pre-open equilibrium price (IEP / final price, set ~09:08)
  previous_close  base for % change
                  primary  : previous_close.json (Dhan) for today
                  fallback : NSE's own previous close (base_source "nse")
  change_percent  (preopen_price - previous_close) / previous_close * 100
  prev_close_check
                  "match" / "mismatch" Dhan vs NSE previous close
                  "unverified" only one of them available

Timing: first attempt at schedule.preopen_first_attempt, then every
preopen_retry_seconds until preopen_last_attempt. A snapshot only counts if
NSE's timestamp is today and at or after premarket.min_snapshot_time (end of
price discovery). If no attempt succeeds the file is written with status
"missing" and every price null — pre-open prices are never estimated.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from datetime import datetime
from typing import Callable

from ..analytics.changes import dataset_status, envelope, highlight, pct_change, r2, split_and_sort, summarize
from ..market_clock import MarketCalendar, at, iso_ist, now_ist, parse_hhmm
from ..nse_client import NseClient, NseError, PreopenQuote, parse_preopen
from ..storage import Storage
from ..universe import UniverseStock
from .live import previous_close_bases
from .sectors import update_sectors

log = logging.getLogger(__name__)
JOB = "premarket"
METHOD = "market-data-pre-open?key=FO"


def _sleep_until(target: datetime, now_fn: Callable[[], datetime], sleep: Callable[[float], None]) -> None:
    while True:
        remaining = (target - now_fn()).total_seconds()
        if remaining <= 0:
            return
        sleep(min(remaining, 30))


def fetch_snapshot(nse: NseClient, trade_date, min_time) -> tuple[datetime | None, dict[str, PreopenQuote]]:
    """One attempt. Raises NseError if the data is unavailable or not today's final snapshot."""
    ts, quotes = parse_preopen(nse.preopen("FO"))
    if not quotes:
        raise NseError("NSE pre-open data is empty")
    if ts is None:
        raise NseError("NSE pre-open response has no timestamp")
    if ts.date() != trade_date:
        raise NseError(f"NSE pre-open data is from {ts.date()}, not {trade_date}")
    if ts.time() < min_time:
        raise NseError(f"NSE pre-open snapshot at {ts.time()} is before price discovery ends ({min_time})")
    return ts, quotes


def run_premarket(
    settings,
    nse: NseClient,
    storage: Storage,
    calendar: MarketCalendar,
    stocks: list[UniverseStock],
    prev_doc: dict | None,
    now_fn: Callable[[], datetime] = now_ist,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    if not stocks:
        storage.record_job(JOB, "failed", "Universe is empty")
        raise ValueError("Universe is empty — run build-universe first")

    g = settings.get
    trade_date = calendar.session_date_for(now_fn())
    threshold = settings.threshold("premarket")
    tolerance = float(g("premarket.prev_close_tolerance_pct", 0.05))
    min_time = parse_hhmm(g("premarket.min_snapshot_time", "09:08"))
    first = at(trade_date, g("schedule.preopen_first_attempt", "09:10:00"))
    last = at(trade_date, g("schedule.preopen_last_attempt", "09:14:30"))
    retry = float(g("schedule.preopen_retry_seconds", 30))
    alerts: list[str] = []

    _sleep_until(first, now_fn, sleep)

    ts, quotes, attempts, problem = None, {}, 0, None
    while True:
        attempts += 1
        try:
            ts, quotes = fetch_snapshot(nse, trade_date, min_time)
            problem = None
            break
        except NseError as exc:
            problem = str(exc)
            log.warning("Pre-open attempt %d failed: %s", attempts, exc)
        if (now_fn() - last).total_seconds() + retry > 0:
            break
        sleep(retry)

    bases = previous_close_bases(prev_doc, trade_date)
    if not bases:
        alerts.append(f"previous_close.json is not for {trade_date}; using NSE previous close as base")

    records: list[dict] = []
    mismatches: list[str] = []
    for s in stocks:
        q = quotes.get(s.symbol.upper())
        dhan_base = bases.get(s.security_id)
        nse_base = q.prev_close if q else None
        base = dhan_base if dhan_base is not None else nse_base
        base_source = "previous_close" if dhan_base is not None else ("nse" if nse_base is not None else None)
        if dhan_base is not None and nse_base is not None:
            check = "match" if abs(dhan_base - nse_base) / dhan_base * 100 <= tolerance else "mismatch"
            if check == "mismatch":
                mismatches.append(f"{s.symbol} (Dhan {dhan_base:.2f} vs NSE {nse_base:.2f})")
        else:
            check = "unverified"
        price = q.iep if q else None
        change, pct = pct_change(price, base)
        if price is None:
            rec_status = "missing"
        elif base is None:
            rec_status = "no_base"
        else:
            rec_status = "ok"
        records.append({
            "symbol": s.symbol,
            "security_id": s.security_id,
            "sector": s.sector,
            "previous_close": r2(base),
            "preopen_price": r2(price),
            "change": change,
            "change_percent": pct,
            "highlight": highlight(pct, threshold),
            "nse_prev_close": r2(nse_base),
            "prev_close_check": check,
            "base_source": base_source,
            "final_quantity": q.final_quantity if q else None,
            "record_status": rec_status,
        })

    unmatched = sorted(s.symbol for s in stocks if s.symbol.upper() not in quotes) if quotes else []
    if unmatched:
        alerts.append(f"{len(unmatched)} F&O stocks not in NSE pre-open data: " + ", ".join(unmatched[:30]))
    if mismatches:
        alerts.append(f"{len(mismatches)} Dhan/NSE previous-close mismatches: " + ", ".join(mismatches[:20]))

    gainers, losers, unchanged = split_and_sort(records)
    summary = summarize(gainers, losers, unchanged)
    status = dataset_status(summary["total"], summary["missing"])
    if problem:
        reason = f"No valid NSE pre-open snapshot after {attempts} attempts: {problem}"
        alerts.append(reason)
    elif status != "ok":
        reason = f"{summary['missing']} of {summary['total']} stocks have no computable % change"
    else:
        reason = None

    doc = envelope("premarket", trade_date, "NSE", METHOD, status, reason, threshold, summary)
    doc["nse_timestamp"] = iso_ist(ts) if ts else None
    doc["attempts"] = attempts
    doc["previous_close_date"] = prev_doc.get("close_date") if bases and prev_doc else None
    doc["checks"] = dict(Counter(r["prev_close_check"] for r in records))
    doc["base_sources"] = dict(Counter(r["base_source"] or "none" for r in records))
    doc["unmatched"] = unmatched
    doc["gainers"], doc["losers"], doc["unchanged"] = gainers, losers, unchanged

    storage.write_dataset("premarket", doc)
    storage.record_job(JOB, status, reason or f"{summary['total']} stocks", alerts)
    update_sectors(settings, storage, "premarket", doc)
    log.info("premarket: %s — %d gainers, %d losers, %d missing (%d attempts)",
             status, summary["gainers"], summary["losers"], summary["missing"], attempts)
    return doc
