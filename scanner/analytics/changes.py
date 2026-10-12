"""Shared rules for % change, highlighting, gainers/losers and the JSON envelope.

Every dataset (previous_close, premarket, live) uses these, so the rules are
identical everywhere:

  change_percent = (price - base) / base * 100, rounded to 2 decimals
  highlight      = "UP" if change_percent > +T, "DOWN" if < -T, else None
                   (evaluated on the rounded value so it matches what is shown)
  gainers        = change_percent > 0, sorted descending
  losers         = change_percent < 0, sorted ascending (biggest loser first)
  unchanged      = exactly 0, or not computable (change_percent null), by symbol
  summary.missing counts records whose change_percent is null
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any

from .. import SCHEMA_VERSION
from ..market_clock import iso_ist, now_ist


def r2(x: float | None) -> float | None:
    return None if x is None else round(float(x) + 0.0, 2)


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def pct_change(price: float | None, base: float | None) -> tuple[float | None, float | None]:
    """Return (change, change_percent), both rounded; (None, None) if not computable."""
    if not (_is_num(price) and _is_num(base)) or base <= 0 or price <= 0:
        return None, None
    change = price - base
    pct = change / base * 100.0
    return r2(change), r2(pct)


def highlight(change_percent: float | None, threshold: float) -> str | None:
    if change_percent is None:
        return None
    if change_percent > threshold:
        return "UP"
    if change_percent < -threshold:
        return "DOWN"
    return None


def split_and_sort(records: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    gainers, losers, unchanged = [], [], []
    for rec in records:
        pct = rec.get("change_percent")
        if pct is None or pct == 0:
            unchanged.append(rec)
        elif pct > 0:
            gainers.append(rec)
        else:
            losers.append(rec)
    gainers.sort(key=lambda r: (-r["change_percent"], r["symbol"]))
    losers.sort(key=lambda r: (r["change_percent"], r["symbol"]))
    unchanged.sort(key=lambda r: r["symbol"])
    return gainers, losers, unchanged


def summarize(gainers: list[dict], losers: list[dict], unchanged: list[dict]) -> dict:
    allrecs = gainers + losers + unchanged
    return {
        "total": len(allrecs),
        "gainers": len(gainers),
        "losers": len(losers),
        "unchanged": sum(1 for r in unchanged if r.get("change_percent") is not None),
        "highlighted_up": sum(1 for r in allrecs if r.get("highlight") == "UP"),
        "highlighted_down": sum(1 for r in allrecs if r.get("highlight") == "DOWN"),
        "missing": sum(1 for r in allrecs if r.get("change_percent") is None),
    }


def dataset_status(total: int, missing: int) -> str:
    if total == 0 or missing >= total:
        return "missing"
    if missing > 0:
        return "partial"
    return "ok"


def envelope(dataset: str, trade_date: date, provider: str, method: str, status: str,
             status_reason: str | None, threshold: float, summary: dict) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset": dataset,
        "trade_date": trade_date.isoformat(),
        "generated_at": iso_ist(now_ist()),
        "source": {"provider": provider, "method": method},
        "status": status,
        "status_reason": status_reason,
        "thresholds": {"up": float(threshold), "down": -float(threshold)},
        "summary": summary,
    }
