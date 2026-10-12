"""IST time, NSE trading days and holidays."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

PRE_OPEN_START = time(9, 0)
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


def now_ist() -> datetime:
    return datetime.now(IST)


def iso_ist(dt: datetime) -> str:
    """ISO 8601 with +05:30 offset, second precision."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST).replace(microsecond=0).isoformat()


def epoch_to_ist(epoch: int | float) -> datetime:
    return datetime.fromtimestamp(float(epoch), tz=IST)


def parse_hhmm(value: str) -> time:
    """'09:10' or '09:14:30' -> time."""
    parts = [int(p) for p in str(value).strip().split(":")]
    if not 2 <= len(parts) <= 3:
        raise ValueError(f"Bad time of day: {value!r}")
    return time(*parts)


def at(d: date, t: time | str) -> datetime:
    """IST datetime for a session date and a time of day."""
    if isinstance(t, str):
        t = parse_hhmm(t)
    return datetime.combine(d, t, tzinfo=IST)


class MarketCalendar:
    def __init__(self, holidays: set[date] | None = None):
        self.holidays = holidays or set()

    @classmethod
    def from_files(cls, *files: Path) -> "MarketCalendar":
        holidays: set[date] = set()
        for f in files:
            if not f.exists():
                continue
            data = json.loads(f.read_text(encoding="utf-8"))
            for h in data.get("holidays", []):
                holidays.add(date.fromisoformat(h["date"]))
        return cls(holidays)

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def previous_trading_day(self, d: date) -> date:
        cur = d - timedelta(days=1)
        while not self.is_trading_day(cur):
            cur -= timedelta(days=1)
        return cur

    def next_trading_day(self, d: date) -> date:
        cur = d + timedelta(days=1)
        while not self.is_trading_day(cur):
            cur += timedelta(days=1)
        return cur

    def session_date_for(self, now: datetime) -> date:
        """The session a pre-market job prepares for: today if it is a trading
        day, otherwise the next trading day."""
        today = now.astimezone(IST).date()
        return today if self.is_trading_day(today) else self.next_trading_day(today)

    def phase(self, now: datetime) -> str:
        now = now.astimezone(IST)
        if not self.is_trading_day(now.date()):
            return "CLOSED"
        t = now.time()
        if PRE_OPEN_START <= t < MARKET_OPEN:
            return "PRE_OPEN"
        if MARKET_OPEN <= t < MARKET_CLOSE:
            return "LIVE"
        return "CLOSED"
