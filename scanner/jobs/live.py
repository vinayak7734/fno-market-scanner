"""Job: keep data/latest/live.json current from the Dhan WebSocket feed.

For each F&O stock:
  ltp             last traded price from the feed (ticker packets)
  previous_close  base for % change
                  primary  : previous_close.json (Dhan, built at 08:45) for today
                  fallback : the feed's own prev-close packet
  change_percent  (ltp - previous_close) / previous_close * 100
  prev_close_check
                  "match" / "mismatch" previous_close.json vs feed prev close
                  "unverified" only one of them available
  record_status   "ok", "stale" (no tick for live.stale_after_seconds; value kept,
                  flagged), "no_data" (no tick yet) or "no_base" (no previous close)

live.json is rewritten atomically every live.write_interval_seconds. The whole
file is marked "stale" when the feed is down or silent for stale_after_seconds.
The final write at the end of the session also goes to data/snapshots/<date>/.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from typing import Callable

from ..analytics.changes import dataset_status, envelope, highlight, pct_change, r2, split_and_sort, summarize
from ..live_feed import FeedAuthError, FeedError, FeedEvent
from ..market_clock import MarketCalendar, iso_ist, now_ist
from ..storage import Storage
from ..universe import UniverseStock
from .sectors import update_sectors

log = logging.getLogger(__name__)
JOB = "live"
METHOD = "websocket ticker"


def previous_close_bases(prev_doc: dict | None, trade_date) -> dict[str, float]:
    """security_id -> previous_close from previous_close.json, only if it is for this session."""
    if not prev_doc or prev_doc.get("trade_date") != trade_date.isoformat():
        return {}
    out = {}
    for key in ("gainers", "losers", "unchanged"):
        for rec in prev_doc.get(key) or []:
            if rec.get("previous_close"):
                out[str(rec["security_id"])] = float(rec["previous_close"])
    return out


class LiveBook:
    """Thread-safe latest-price book fed by LiveFeed callbacks."""

    def __init__(self, stocks: list[UniverseStock], bases: dict[str, float], threshold: float,
                 tolerance_pct: float, stale_after: float, clock: Callable[[], datetime] = now_ist):
        self.stocks = stocks
        self.by_id = {s.security_id: s for s in stocks}
        self.bases = bases
        self.threshold = threshold
        self.tolerance = tolerance_pct
        self.stale_after = timedelta(seconds=stale_after)
        self.clock = clock
        self._lock = threading.Lock()
        self.ltp: dict[str, float] = {}
        self.tick_at: dict[str, datetime] = {}
        self.feed_prev: dict[str, float] = {}
        self.ticks = 0
        self.last_tick_at: datetime | None = None
        self.state = "starting"
        self.state_detail: str | None = None
        self.connected_since: datetime | None = None
        self.reconnects = 0

    def on_event(self, ev: FeedEvent) -> None:
        if ev.security_id not in self.by_id:
            return
        now = self.clock()
        with self._lock:
            if ev.kind == "ltp" and ev.ltp is not None:
                self.ltp[ev.security_id] = ev.ltp
                self.tick_at[ev.security_id] = now
                self.ticks += 1
                self.last_tick_at = now
            elif ev.kind == "prev_close" and ev.prev_close is not None:
                self.feed_prev[ev.security_id] = ev.prev_close

    def on_state(self, state: str, detail: str | None) -> None:
        with self._lock:
            if state == "reconnecting":
                self.reconnects += 1
            self.state = state
            self.state_detail = detail
            self.connected_since = self.clock() if state == "connected" else None
            log.info("feed %s%s", state, f" ({detail})" if detail else "")

    def _record(self, s: UniverseStock, now: datetime) -> dict:
        sid = s.security_id
        file_base = self.bases.get(sid)
        feed_base = self.feed_prev.get(sid)
        base = file_base if file_base is not None else feed_base
        base_source = "previous_close" if file_base is not None else ("feed" if feed_base is not None else None)
        if file_base is not None and feed_base is not None:
            diff = abs(file_base - feed_base) / file_base * 100
            check = "match" if diff <= self.tolerance else "mismatch"
        else:
            check = "unverified"

        ltp = self.ltp.get(sid)
        tick_at = self.tick_at.get(sid)
        change, pct = pct_change(ltp, base)
        if base is None:
            rec_status = "no_base"
        elif ltp is None:
            rec_status = "no_data"
        elif tick_at is not None and now - tick_at > self.stale_after:
            rec_status = "stale"
        else:
            rec_status = "ok"
        return {
            "symbol": s.symbol,
            "security_id": sid,
            "sector": s.sector,
            "previous_close": r2(base),
            "ltp": r2(ltp),
            "change": change,
            "change_percent": pct,
            "highlight": highlight(pct, self.threshold),
            "last_tick_at": iso_ist(tick_at) if tick_at else None,
            "feed_prev_close": r2(feed_base),
            "prev_close_check": check,
            "base_source": base_source,
            "record_status": rec_status,
        }

    def document(self, trade_date, previous_close_date: str | None = None, final: bool = False) -> dict:
        """final=True is the closing snapshot after a normal stop: the feed being
        stopped is expected then, so it does not make the file stale."""
        now = self.clock()
        with self._lock:
            records = [self._record(s, now) for s in self.stocks]
            feed = {
                "state": self.state,
                "detail": self.state_detail,
                "connected_since": iso_ist(self.connected_since) if self.connected_since else None,
                "last_tick_at": iso_ist(self.last_tick_at) if self.last_tick_at else None,
                "reconnects": self.reconnects,
                "ticks": self.ticks,
            }
            silent = self.last_tick_at is None or now - self.last_tick_at > self.stale_after
            connected = self.state == "connected"

        gainers, losers, unchanged = split_and_sort(records)
        summary = summarize(gainers, losers, unchanged)
        summary["stale"] = sum(1 for r in records if r["record_status"] == "stale")
        status = dataset_status(summary["total"], summary["missing"])
        reason = None
        if status != "missing" and not final and (not connected or silent):
            status = "stale"
            reason = f"Feed {feed['state']}; last tick {feed['last_tick_at'] or 'never'}"
        elif status != "ok":
            reason = f"{summary['missing']} of {summary['total']} stocks have no computable % change"

        doc = envelope("live", trade_date, "DHAN", METHOD, status, reason, self.threshold, summary)
        doc["as_of"] = iso_ist(now)
        doc["previous_close_date"] = previous_close_date
        doc["feed"] = feed
        doc["checks"] = dict(Counter(r["prev_close_check"] for r in records))
        doc["base_sources"] = dict(Counter(r["base_source"] or "none" for r in records))
        doc["gainers"], doc["losers"], doc["unchanged"] = gainers, losers, unchanged
        return doc


def run_live(
    settings,
    storage: Storage,
    calendar: MarketCalendar,
    stocks: list[UniverseStock],
    prev_doc: dict | None,
    feed_factory: Callable[[Callable, Callable], object],
    until: datetime,
    now_fn: Callable[[], datetime] = now_ist,
    stop: threading.Event | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Run the feed until `until` (or `stop`), writing live.json every interval."""
    if not stocks:
        storage.record_job(JOB, "failed", "Universe is empty")
        raise ValueError("Universe is empty — run build-universe first")

    stop = stop or threading.Event()
    trade_date = calendar.session_date_for(now_fn())
    threshold = settings.threshold("live")
    interval = float(settings.get("live.write_interval_seconds", 5))
    stale_after = float(settings.get("live.stale_after_seconds", 60))
    tolerance = float(settings.get("previous_close.prev_close_tolerance_pct", 0.05))
    alerts: list[str] = []

    bases = previous_close_bases(prev_doc, trade_date)
    prev_close_date = prev_doc.get("close_date") if bases and prev_doc else None
    if not bases:
        alerts.append(f"previous_close.json is not for {trade_date}; using the feed's previous close as base")

    book = LiveBook(stocks, bases, threshold, tolerance, stale_after, clock=now_fn)
    feed = feed_factory(book.on_event, book.on_state)
    feed_stop = threading.Event()
    failure: list[BaseException] = []

    def worker():
        try:
            feed.run([s.security_id for s in stocks], feed_stop)
        except BaseException as exc:  # noqa: BLE001
            failure.append(exc)

    thread = threading.Thread(target=worker, name="dhan-feed", daemon=True)
    thread.start()
    storage.record_job(JOB, "running", f"feed started for {len(stocks)} stocks, until {iso_ist(until)}")
    heartbeat_every = timedelta(seconds=float(settings.get("live.heartbeat_seconds", 60)))
    next_heartbeat = now_fn() + heartbeat_every
    ticks_at_heartbeat = 0
    last_status = None
    try:
        while not stop.is_set() and now_fn() < until and not failure:
            waited = 0.0
            while waited < interval and not stop.is_set() and not failure and now_fn() < until:
                step = min(0.5, interval - waited)
                sleep(step)
                waited += step
            doc = book.document(trade_date, prev_close_date)
            storage.write_dataset("live", doc, snapshot=False)
            update_sectors(settings, storage, "live", doc, snapshot=False)
            if doc["status"] != last_status:
                log.info("live.json status %s -> %s%s", last_status, doc["status"],
                         f" ({doc['status_reason']})" if doc["status_reason"] else "")
                last_status = doc["status"]
            if now_fn() >= next_heartbeat:
                ticks = doc["feed"]["ticks"]
                log.info("live heartbeat: %d ticks since last heartbeat, feed %s, last tick %s, stale stocks %d",
                         ticks - ticks_at_heartbeat, doc["feed"]["state"], doc["feed"]["last_tick_at"],
                         doc["summary"]["stale"])
                ticks_at_heartbeat = ticks
                next_heartbeat = now_fn() + heartbeat_every
    finally:
        feed_stop.set()
        thread.join(timeout=10)

    doc = book.document(trade_date, prev_close_date, final=not failure)
    if failure:
        exc = failure[0]
        alerts.append(f"Live feed failed: {exc}")
        doc["status"] = "missing" if doc["summary"]["missing"] >= doc["summary"]["total"] else "stale"
        doc["status_reason"] = f"Live feed failed: {exc}"
    storage.write_dataset("live", doc, snapshot=True)
    update_sectors(settings, storage, "live", doc, snapshot=True)
    s = doc["summary"]
    storage.record_job(JOB, "failed" if failure else doc["status"],
                       doc["status_reason"] or f"{s['total']} stocks, {doc['feed']['ticks']} ticks", alerts)
    log.info("live: %s — %d gainers, %d losers, %d missing, %d ticks",
             doc["status"], s["gainers"], s["losers"], s["missing"], doc["feed"]["ticks"])
    if failure and isinstance(failure[0], (FeedAuthError, FeedError)):
        raise failure[0]
    return doc
