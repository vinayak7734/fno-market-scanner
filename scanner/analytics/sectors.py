"""Equal-weight sector performance from per-stock records.

For one dataset (previous_close, premarket or live):

  avg_change_percent = mean of the sector's stocks' change_percent, rounded to 2
                       decimals — only when at least min_coverage of the
                       sector's stocks have a change_percent; otherwise null
                       (never estimated from fewer stocks)
  highlight          = "UP" / "DOWN" against the sector threshold for the
                       dataset (same > / < rule as stocks)
  status             = "ok" (all priced), "partial" (>= coverage), "missing"

Stocks are grouped by the current config/sectors.json (by symbol), so a config
edit applies at the next update. All configured sectors are listed, even with
no constituents today; stocks with no sector form a separate "unmapped" group
that is not ranked.
Sectors are sorted by avg_change_percent descending, null last, then by name.
"""

from __future__ import annotations

from ..sectors import UNMAPPED, SectorMap
from .changes import highlight, r2


def _stock_ref(rec: dict | None) -> dict | None:
    if not rec:
        return None
    return {"symbol": rec["symbol"], "change_percent": rec["change_percent"]}


def sector_row(name: str, records: list[dict], threshold: float, min_coverage: float) -> dict:
    priced = [r for r in records if r.get("change_percent") is not None]
    n, k = len(records), len(priced)
    coverage = k / n if n else 0.0
    if n and coverage >= min_coverage:
        avg = r2(sum(r["change_percent"] for r in priced) / k)
        status = "ok" if k == n else "partial"
    else:
        avg = None
        status = "missing"
    best = max(priced, key=lambda r: (r["change_percent"], r["symbol"]), default=None)
    worst = min(priced, key=lambda r: (r["change_percent"], r["symbol"]), default=None)
    return {
        "sector": name,
        "constituents": n,
        "priced": k,
        "coverage_pct": r2(coverage * 100),
        "avg_change_percent": avg,
        "highlight": highlight(avg, threshold),
        "advances": sum(1 for r in priced if r["change_percent"] > 0),
        "declines": sum(1 for r in priced if r["change_percent"] < 0),
        "unchanged": sum(1 for r in priced if r["change_percent"] == 0),
        "highlighted_up": sum(1 for r in records if r.get("highlight") == "UP"),
        "highlighted_down": sum(1 for r in records if r.get("highlight") == "DOWN"),
        "top_gainer": _stock_ref(best) if best and best["change_percent"] > 0 else None,
        "top_loser": _stock_ref(worst) if worst and worst["change_percent"] < 0 else None,
        "status": status,
        "symbols": sorted(r["symbol"] for r in records),
    }


def all_records(doc: dict) -> list[dict]:
    return (doc.get("gainers") or []) + (doc.get("losers") or []) + (doc.get("unchanged") or [])


def sector_block(doc: dict, sector_map: SectorMap, threshold: float, min_coverage: float) -> dict:
    """Sector performance for one dataset document, grouped by the current sectors.json."""
    groups: dict[str, list[dict]] = {name: [] for name in sector_map.names}
    unmapped: list[dict] = []
    for rec in all_records(doc):
        sec = sector_map.sector_of(rec["symbol"])
        if sec in groups:
            groups[sec].append(rec)
        else:
            unmapped.append(rec)

    rows = [sector_row(name, recs, threshold, min_coverage) for name, recs in groups.items()]
    rows.sort(key=lambda r: (r["avg_change_percent"] is None, -(r["avg_change_percent"] or 0), r["sector"]))
    for i, row in enumerate(rows, 1):
        row["rank"] = i if row["avg_change_percent"] is not None else None

    with_avg = [r for r in rows if r["avg_change_percent"] is not None]
    if doc.get("status") == "missing" or not with_avg:
        status = "missing"
    elif len(with_avg) < len(rows) or any(r["status"] != "ok" for r in rows) or doc.get("status") != "ok":
        status = "partial"
    else:
        status = "ok"

    return {
        "dataset": doc.get("dataset"),
        "source_generated_at": doc.get("as_of") or doc.get("generated_at"),
        "source_status": doc.get("status"),
        "status": status,
        "thresholds": {"up": float(threshold), "down": -float(threshold)},
        "summary": {
            "sectors": len(rows),
            "with_average": len(with_avg),
            "advancing": sum(1 for r in with_avg if r["avg_change_percent"] > 0),
            "declining": sum(1 for r in with_avg if r["avg_change_percent"] < 0),
            "highlighted_up": sum(1 for r in rows if r["highlight"] == "UP"),
            "highlighted_down": sum(1 for r in rows if r["highlight"] == "DOWN"),
            "missing": len(rows) - len(with_avg),
        },
        "sectors": rows,
        "unmapped": sector_row(UNMAPPED, unmapped, threshold, min_coverage) if unmapped else None,
    }
