"""Job: keep data/latest/sectors.json in step with the stock datasets.

sectors.json holds one block per dataset — previous_close, premarket, live —
each computed by analytics/sectors.py from that dataset's records. A block is
only included when its dataset is for the same trade_date; a new trade_date
starts a fresh file.

update_sectors() is called by the previous-close and premarket jobs after they
write, and by the live job on every write. It never raises: a sector problem
(e.g. a broken sectors.json mid-day) is logged and recorded in status.json,
and the stock datasets carry on.
"""

from __future__ import annotations

import logging

from .. import SCHEMA_VERSION
from ..analytics.sectors import sector_block
from ..market_clock import iso_ist, now_ist
from ..sectors import SectorMap, load_sectors
from ..storage import Storage

log = logging.getLogger(__name__)
JOB = "sectors"
BLOCKS = ("previous_close", "premarket", "live")


def _config(settings) -> tuple[SectorMap, dict[str, float], float]:
    sector_map = load_sectors(settings.path("paths.sectors_file"))
    thresholds = {ds: float(settings.require(f"sector_thresholds.{ds}")) for ds in BLOCKS}
    min_coverage = float(settings.get("sectors.min_coverage_pct", 50)) / 100.0
    return sector_map, thresholds, min_coverage


def _document(settings, trade_date: str, blocks: dict[str, dict | None]) -> dict:
    _, thresholds, min_coverage = _config(settings)
    present = [b for b in blocks.values() if b]
    if not present:
        status = "missing"
    elif all(b["status"] == "ok" for b in present):
        status = "ok"
    elif all(b["status"] == "missing" for b in present):
        status = "missing"
    else:
        status = "partial"
    doc = {
        "schema_version": SCHEMA_VERSION,
        "dataset": "sectors",
        "trade_date": trade_date,
        "generated_at": iso_ist(now_ist()),
        "method": settings.get("sectors.method", "equal_weight"),
        "min_coverage_pct": r_pct(min_coverage),
        "thresholds": {ds: {"up": t, "down": -t} for ds, t in thresholds.items()},
        "status": status,
    }
    for ds in BLOCKS:
        doc[ds] = blocks.get(ds)
    return doc


def r_pct(fraction: float) -> float:
    return round(fraction * 100, 2)


def compute_block(settings, dataset: str, doc: dict) -> dict:
    sector_map, thresholds, min_coverage = _config(settings)
    return sector_block(doc, sector_map, thresholds[dataset], min_coverage)


def update_sectors(settings, storage: Storage, dataset: str, doc: dict, snapshot: bool = True) -> dict | None:
    """Recompute the `dataset` block from `doc` and rewrite sectors.json."""
    try:
        trade_date = doc["trade_date"]
        current = storage.read_dataset("sectors") or {}
        blocks = {ds: current.get(ds) for ds in BLOCKS} if current.get("trade_date") == trade_date else {}
        blocks[dataset] = compute_block(settings, dataset, doc)
        out = _document(settings, trade_date, blocks)
        storage.write_dataset("sectors", out, snapshot=snapshot)
        if snapshot:      # not on every 5-second live write
            storage.record_job(JOB, out["status"], f"{dataset} block updated")
        return out
    except Exception as exc:  # noqa: BLE001 — sectors must never break the stock jobs
        log.error("Sector update for %s failed: %s", dataset, exc)
        try:
            storage.record_job(JOB, "failed", f"{dataset}: {exc}", [f"Sector update for {dataset} failed: {exc}"])
        except Exception:  # noqa: BLE001
            pass
        return None


def rebuild_sectors(settings, storage: Storage, trade_date) -> dict:
    """Recompute every block from the current data/latest files (no network)."""
    td = trade_date.isoformat()
    blocks: dict[str, dict | None] = {}
    skipped = []
    for ds in BLOCKS:
        doc = storage.read_dataset(ds)
        if doc and doc.get("trade_date") == td:
            blocks[ds] = compute_block(settings, ds, doc)
        else:
            blocks[ds] = None
            skipped.append(ds)
    out = _document(settings, td, blocks)
    storage.write_dataset("sectors", out)
    storage.record_job(JOB, out["status"],
                       f"rebuilt for {td}" + (f"; no {', '.join(skipped)} for today" if skipped else ""))
    return out
