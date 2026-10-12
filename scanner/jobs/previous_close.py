"""Job: build data/latest/previous_close.json (Dhan data only).

For each F&O stock:
  previous_close  last completed session's close
                  primary  : Dhan quote  -> last_price - net_change
                  fallback : Dhan daily candle for that session
  prior_close     close of the session before it (Dhan daily candles)
  change_percent  (previous_close - prior_close) / prior_close * 100
  prev_close_check
                  "match"      quote and candle agree within tolerance
                  "mismatch"   they disagree (value kept from quote, flagged)
                  "unverified" only one source available

The session dates are taken from the candles themselves (the most common
latest date), and compared with the holiday calendar; a disagreement raises
an alert instead of silently trusting either.

Auth / data-plan errors abort the job without writing a file: no data is
better than wrong data. Per-stock failures leave that stock's values null.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime, timedelta

from ..analytics.changes import (dataset_status, envelope, highlight, pct_change, r2, split_and_sort,
                                 summarize)
from ..dhan_client import Candle, DhanAuthError, DhanClient, DhanError
from ..market_clock import MARKET_OPEN, MarketCalendar, at, now_ist
from ..storage import Storage
from ..universe import UniverseStock
from .sectors import update_sectors

log = logging.getLogger(__name__)
JOB = "previous_close"
METHOD = "marketfeed/quote + charts/historical"


class PreviousCloseExists(Exception):
    """A valid pre-open build for this session exists and the market has opened."""


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def quote_previous_close(q: dict | None) -> float | None:
    """Previous close implied by a Dhan quote: last_price - net_change."""
    if not q:
        return None
    lp = _num(q.get("last_price"))
    nc = q.get("net_change")
    try:
        nc = float(nc)
    except (TypeError, ValueError):
        return None
    if lp is None:
        return None
    return _num(round(lp - nc, 2))   # prices are quoted in paise


def _close_on(candles: list[Candle], day: date) -> float | None:
    for c in candles:
        if c.day == day:
            return _num(c.close)
    return None


def _close_before(candles: list[Candle], day: date) -> tuple[date | None, float | None]:
    prior = [c for c in candles if c.day < day]
    if not prior:
        return None, None
    return prior[-1].day, _num(prior[-1].close)


def run_previous_close(
    settings,
    client: DhanClient,
    storage: Storage,
    calendar: MarketCalendar,
    stocks: list[UniverseStock],
    now: datetime | None = None,
    force: bool = False,
) -> dict:
    now = now or now_ist()
    trade_date = calendar.session_date_for(now)

    # Never let a late/manual re-run replace the good pre-open build: once this
    # session's market has opened, Dhan's quote no longer gives the previous close.
    existing = storage.read_dataset(JOB)
    if (not force and existing and existing.get("trade_date") == trade_date.isoformat()
            and existing.get("status") == "ok" and now >= at(trade_date, MARKET_OPEN)):
        raise PreviousCloseExists(
            f"previous_close.json for {trade_date} is already ok (generated {existing.get('generated_at')}) "
            "and the market has opened; kept it unchanged. Use --force to rebuild anyway.")

    expected_close_date = calendar.previous_trading_day(trade_date)
    threshold = settings.threshold("previous_close")
    tolerance = float(settings.get("previous_close.prev_close_tolerance_pct", 0.05))
    fetch_prior = bool(settings.get("previous_close.fetch_prior_close", True))
    lookback = int(settings.get("dhan.history_lookback_days", 12))
    alerts: list[str] = []

    if not stocks:
        storage.record_job(JOB, "failed", "Universe is empty")
        raise ValueError("Universe is empty — run build-universe first")

    try:
        client.ensure_data_plan()
        quotes = client.quote([s.security_id for s in stocks])
    except DhanAuthError as exc:
        storage.record_job(JOB, "failed", str(exc), [f"Previous close not built: {exc}"])
        raise
    except DhanError as exc:
        log.error("Quote request failed, continuing with historical data only: %s", exc)
        alerts.append(f"Dhan quote failed, used daily candles only: {exc}")
        quotes = {}

    # ---- daily candles -----------------------------------------------------
    history: dict[str, list[Candle]] = {}
    if fetch_prior:
        start = trade_date - timedelta(days=lookback)
        failures = 0
        for s in stocks:
            try:
                history[s.security_id] = [c for c in client.daily_history(s.security_id, start, trade_date)
                                          if c.day < trade_date]
            except DhanAuthError as exc:
                storage.record_job(JOB, "failed", str(exc), [f"Previous close not built: {exc}"])
                raise
            except DhanError as exc:
                failures += 1
                log.warning("History failed for %s: %s", s.symbol, exc)
        if failures:
            alerts.append(f"Daily candles unavailable for {failures} stocks")

    # Session date = most common latest candle date; cross-check with calendar.
    last_days = Counter(h[-1].day for h in history.values() if h)
    close_date = last_days.most_common(1)[0][0] if last_days else expected_close_date
    if last_days and close_date != expected_close_date:
        alerts.append(f"Holiday calendar expected last session {expected_close_date}, "
                      f"Dhan candles show {close_date}; using {close_date}. Check config/nse_holidays_*.json")

    # ---- per-stock records ---------------------------------------------------
    records: list[dict] = []
    prior_dates: Counter = Counter()
    mismatches: list[str] = []
    for s in stocks:
        candles = history.get(s.security_id, [])
        q_prev = quote_previous_close(quotes.get(s.security_id))
        h_prev = _close_on(candles, close_date)
        prior_day, prior_close = _close_before(candles, close_date)
        if prior_day:
            prior_dates[prior_day] += 1

        if q_prev is not None and h_prev is not None:
            diff_pct = abs(q_prev - h_prev) / h_prev * 100
            check = "match" if diff_pct <= tolerance else "mismatch"
            if check == "mismatch":
                mismatches.append(f"{s.symbol} (quote {q_prev:.2f} vs candle {h_prev:.2f})")
        else:
            check = "unverified"

        prev = q_prev if q_prev is not None else h_prev
        source = "quote" if q_prev is not None else ("historical" if h_prev is not None else None)
        change, pct = pct_change(prev, prior_close)
        if prev is None:
            rec_status = "missing"
        elif pct is None:
            rec_status = "no_prior_close"
        else:
            rec_status = "ok"

        records.append({
            "symbol": s.symbol,
            "security_id": s.security_id,
            "sector": s.sector,
            "previous_close": r2(prev),
            "prior_close": r2(prior_close),
            "change": change,
            "change_percent": pct,
            "highlight": highlight(pct, threshold),
            "prev_close_check": check,
            "prev_close_source": source,
            "record_status": rec_status,
        })

    if mismatches:
        alerts.append(f"{len(mismatches)} quote/candle previous-close mismatches: " + ", ".join(mismatches[:20]))

    gainers, losers, unchanged = split_and_sort(records)
    summary = summarize(gainers, losers, unchanged)
    status = dataset_status(summary["total"], summary["missing"])
    reason = None
    if status != "ok":
        reason = f"{summary['missing']} of {summary['total']} stocks have no computable % change"

    doc = envelope("previous_close", trade_date, "DHAN", METHOD, status, reason, threshold, summary)
    doc["close_date"] = close_date.isoformat()
    doc["prior_close_date"] = prior_dates.most_common(1)[0][0].isoformat() if prior_dates else None
    doc["checks"] = {
        "match": sum(1 for r in records if r["prev_close_check"] == "match"),
        "mismatch": len(mismatches),
        "unverified": sum(1 for r in records if r["prev_close_check"] == "unverified"),
    }
    doc["gainers"], doc["losers"], doc["unchanged"] = gainers, losers, unchanged

    storage.write_dataset("previous_close", doc)
    storage.record_job(JOB, status, reason or f"{summary['total']} stocks", alerts)
    update_sectors(settings, storage, "previous_close", doc)
    log.info("previous_close: %s — %d gainers, %d losers, %d missing",
             status, summary["gainers"], summary["losers"], summary["missing"])
    return doc
