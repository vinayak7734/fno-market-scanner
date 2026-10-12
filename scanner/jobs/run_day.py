"""Job: run one trading day end to end on the schedule in settings.yaml.

  token_refresh   08:15   pre-flight checks (holiday file, disk space), then make
                          sure the Dhan token lasts until eod_archive
  universe_build  08:30   build-universe
  previous_close  08:45   previous-close
  preopen         09:10   premarket (retries until 09:14:30)
  ws_connect      09:14   live feed until live_end (15:30)
  eod_archive     15:35   copy the day's final files into data/snapshots/<date>/
  publish         15:40   commit that snapshot folder and push it (jobs/publish.py)

Weekends and NSE holidays: nothing runs. Started late, it waits for nothing
that has passed: steps whose output already exists for today are skipped, the
rest run immediately in order (the live step is skipped after live_end).
A failing step is recorded in status.json and the day carries on — later
steps decide for themselves whether they can still produce data.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Callable

from ..market_clock import MarketCalendar, at, iso_ist, now_ist
from ..storage import Storage, read_json, write_json_atomic
from .token import check_token  # noqa: F401 — re-exported for callers of run_day

log = logging.getLogger(__name__)
JOB = "run_day"

# (step name, schedule key, dataset whose presence for today means "already done")
PLAN = [
    ("preflight", "token_refresh", None),
    ("token_check", "token_refresh", None),
    ("build_universe", "universe_build", "universe"),
    ("previous_close", "previous_close", "previous_close"),
    ("premarket", "preopen_first_attempt", "premarket"),
    ("live", "ws_connect", None),
    ("eod_archive", "eod_archive", None),
]
DEFAULT_TIMES = {"token_refresh": "08:15", "universe_build": "08:30", "previous_close": "08:45",
                 "preopen_first_attempt": "09:10:00", "ws_connect": "09:14", "live_end": "15:30",
                 "eod_archive": "15:35", "publish": "15:40"}


def archive_day(storage: Storage, trade_date) -> str:
    """Make sure the day's final datasets and status are in data/snapshots/<date>/."""
    copied = []
    for ds in ("universe", "previous_close", "premarket", "live", "sectors"):
        doc = storage.read_dataset(ds)
        if doc and doc.get("trade_date") == trade_date.isoformat():
            write_json_atomic(storage.snapshot_path(trade_date, ds), doc)
            copied.append(ds)
    status = read_json(storage.latest_path("status"))
    if status:
        write_json_atomic(storage.snapshot_path(trade_date, "status"), status)
        copied.append("status")
    return "archived " + ", ".join(copied) if copied else "nothing to archive"


class DayRunner:
    def __init__(self, settings, storage: Storage, calendar: MarketCalendar,
                 actions: dict[str, Callable[..., object]],
                 now_fn: Callable[[], datetime] = now_ist, sleep: Callable[[float], None] = time.sleep,
                 stop: threading.Event | None = None):
        self.settings = settings
        self.storage = storage
        self.calendar = calendar
        self.actions = actions
        self.now_fn = now_fn
        self.sleep = sleep
        self.stop = stop or threading.Event()

    def _time(self, key: str) -> datetime:
        return at(self.trade_date, self.settings.get(f"schedule.{key}", DEFAULT_TIMES[key]))

    def _wait_until(self, target: datetime) -> None:
        while not self.stop.is_set():
            remaining = (target - self.now_fn()).total_seconds()
            if remaining <= 0:
                return
            self.sleep(min(remaining, 30))

    def _done_today(self, dataset: str | None) -> bool:
        if not dataset:
            return False
        doc = self.storage.read_dataset(dataset)
        return bool(doc) and doc.get("trade_date") == self.trade_date.isoformat() and doc.get("status") != "missing"

    def run(self) -> dict[str, str]:
        now = self.now_fn()
        self.trade_date = now.date()
        if not self.calendar.is_trading_day(self.trade_date):
            msg = f"{self.trade_date} is not a trading day; nothing to do"
            log.info(msg)
            self.storage.record_job(JOB, "skipped", msg)
            return {}

        live_end = self._time("live_end")
        results: dict[str, str] = {}
        self.storage.record_job(JOB, "running", f"started {iso_ist(now)}")
        for name, key, dataset in PLAN:
            if name not in self.actions:      # optional step not configured
                continue
            self._wait_until(self._time(key))
            if self.stop.is_set():
                results[name] = "stopped"
                break
            if self._done_today(dataset):
                results[name] = "already done"
            elif name == "live" and self.now_fn() >= live_end:
                results[name] = "skipped (after live_end)"
            else:
                log.info("run-day: %s", name)
                try:
                    out = self.actions[name](live_end) if name == "live" else self.actions[name]()
                    results[name] = out if isinstance(out, str) else "ok"
                except Exception as exc:  # noqa: BLE001 — one failed step must not end the day
                    log.error("run-day: %s failed: %s", name, exc)
                    results[name] = f"failed: {exc}"
            log.info("run-day: %s -> %s", name, results[name])

        failed = [n for n, r in results.items() if r.startswith("failed")]
        self.storage.record_job(JOB, "partial" if failed else "ok",
                                "; ".join(f"{n}: {r}" for n, r in results.items()),
                                [f"{n} {results[n]}" for n in failed])
        if "eod_archive" in results and not results["eod_archive"].startswith("failed"):
            archive_day(self.storage, self.trade_date)   # include the final status.json

        # Publish last, after the final archive, so the committed snapshot is complete.
        if "publish" in self.actions and not self.stop.is_set() and "eod_archive" in results:
            self._wait_until(self._time("publish"))
            if not self.stop.is_set():
                log.info("run-day: publish")
                try:
                    out = self.actions["publish"]()
                    results["publish"] = out if isinstance(out, str) else "ok"
                except Exception as exc:  # noqa: BLE001
                    log.error("run-day: publish failed: %s", exc)
                    results["publish"] = f"failed: {exc}"
                log.info("run-day: publish -> %s", results["publish"])
        return results
