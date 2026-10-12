"""Build the NSE F&O stock universe from Dhan's detailed instrument master.

Rule:
  1. F&O stocks = distinct UNDERLYING_SYMBOL of rows with
     EXCH_ID=NSE, SEGMENT=D, INSTRUMENT=FUTSTK, excluding Dhan's dummy
     test underlyings (symbols ending in NSETEST).
  2. Map each to its NSE cash-equity row (EXCH_ID=NSE, SEGMENT=E,
     INSTRUMENT=EQUITY, SERIES=EQ) — primarily by UNDERLYING_SECURITY_ID,
     falling back to symbol. That row's SECURITY_ID is the NSE_EQ id used
     for quotes and the live feed.
  3. Attach the sector from config/sectors.json.

Nothing is hard-coded: additions/removals to F&O appear on the next build.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterable, TextIO

from . import SCHEMA_VERSION
from .market_clock import iso_ist, now_ist
from .sectors import UNMAPPED, SectorMap

log = logging.getLogger(__name__)

REQUIRED_COLUMNS = {
    "EXCH_ID", "SEGMENT", "SECURITY_ID", "INSTRUMENT",
    "UNDERLYING_SECURITY_ID", "UNDERLYING_SYMBOL", "SERIES",
}


class UniverseError(RuntimeError):
    pass


@dataclass
class UniverseStock:
    symbol: str
    security_id: str
    sector: str
    lot_size: int | None = None
    name: str | None = None
    mapped_by: str = "underlying_id"   # or "symbol"

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "security_id": self.security_id,
            "sector": self.sector,
            "lot_size": self.lot_size,
            "name": self.name,
        }


@dataclass
class UniverseBuild:
    stocks: list[UniverseStock]
    fno_underlyings: int
    missing_equity: list[str] = field(default_factory=list)   # FUTSTK symbol with no NSE_EQ row
    unmapped_sector: list[str] = field(default_factory=list)  # no sector in sectors.json
    mapped_by_symbol: list[str] = field(default_factory=list)


def _norm(v: str | None) -> str:
    return (v or "").strip()


def _to_int(v: str | None) -> int | None:
    try:
        return int(float(_norm(v)))
    except ValueError:
        return None


def _id(v: str | None) -> str:
    """Security IDs may arrive as '1333' or '1333.0'."""
    s = _norm(v)
    if s.endswith(".0"):
        s = s[:-2]
    return s


def _is_test_symbol(sym: str) -> bool:
    """Dhan lists dummy F&O underlyings (011NSETEST, 021NSETEST, ...) that have no price data."""
    return sym.endswith("NSETEST")


def parse_instrument_master(fh: TextIO, sector_map: SectorMap) -> UniverseBuild:
    reader = csv.DictReader(fh)
    header = {h.strip() for h in (reader.fieldnames or [])}
    missing_cols = REQUIRED_COLUMNS - header
    if missing_cols:
        raise UniverseError(
            "Instrument master is missing columns "
            f"{sorted(missing_cols)}. Use the DETAILED file (api-scrip-master-detailed.csv).")

    equity_by_id: dict[str, dict] = {}
    equity_by_symbol: dict[str, dict] = {}
    futstk: dict[str, dict] = {}   # underlying symbol -> {underlying_id, lot_size}

    for row in reader:
        row = {k.strip(): v for k, v in row.items() if k}
        if _norm(row.get("EXCH_ID")).upper() != "NSE":
            continue
        seg = _norm(row.get("SEGMENT")).upper()
        inst = _norm(row.get("INSTRUMENT")).upper()

        if seg == "E" and inst == "EQUITY" and _norm(row.get("SERIES")).upper() == "EQ":
            sid = _id(row.get("SECURITY_ID"))
            if sid:
                equity_by_id[sid] = row
                sym = _norm(row.get("UNDERLYING_SYMBOL")).upper()
                if sym:
                    equity_by_symbol[sym] = row
        elif seg == "D" and inst == "FUTSTK":
            sym = _norm(row.get("UNDERLYING_SYMBOL")).upper()
            if not sym or _is_test_symbol(sym):
                continue
            entry = futstk.setdefault(sym, {"underlying_id": _id(row.get("UNDERLYING_SECURITY_ID")),
                                            "lot_size": _to_int(row.get("LOT_SIZE"))})
            if not entry["underlying_id"]:
                entry["underlying_id"] = _id(row.get("UNDERLYING_SECURITY_ID"))

    build = UniverseBuild(stocks=[], fno_underlyings=len(futstk))
    for sym in sorted(futstk):
        info = futstk[sym]
        eq = equity_by_id.get(info["underlying_id"]) if info["underlying_id"] else None
        mapped_by = "underlying_id"
        if eq is None:
            eq = equity_by_symbol.get(sym)
            mapped_by = "symbol"
        if eq is None:
            build.missing_equity.append(sym)
            continue
        if mapped_by == "symbol":
            build.mapped_by_symbol.append(sym)
        sector = sector_map.sector_of(sym)
        if sector == UNMAPPED:
            build.unmapped_sector.append(sym)
        build.stocks.append(UniverseStock(
            symbol=sym,
            security_id=_id(eq.get("SECURITY_ID")),
            sector=sector,
            lot_size=info["lot_size"],
            name=_norm(eq.get("SYMBOL_NAME")) or _norm(eq.get("DISPLAY_NAME")) or None,
            mapped_by=mapped_by,
        ))
    return build


def validate_build(build: UniverseBuild, min_count: int, max_count: int, max_unmapped_ratio: float) -> list[str]:
    """Return a list of problems; empty means the build can be trusted."""
    problems = []
    n = len(build.stocks)
    if not (min_count <= n <= max_count):
        problems.append(f"F&O stock count {n} outside expected range {min_count}-{max_count}")
    if build.fno_underlyings:
        ratio = len(build.missing_equity) / build.fno_underlyings
        if ratio > max_unmapped_ratio:
            problems.append(
                f"{len(build.missing_equity)}/{build.fno_underlyings} F&O underlyings have no NSE_EQ match "
                f"({ratio:.0%} > {max_unmapped_ratio:.0%})")
    ids = [s.security_id for s in build.stocks]
    if len(ids) != len(set(ids)):
        problems.append("Duplicate NSE_EQ security IDs in universe")
    return problems


def universe_document(build: UniverseBuild, trade_date: date, source_file: str,
                      status: str = "ok", status_reason: str | None = None) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset": "universe",
        "trade_date": trade_date.isoformat(),
        "generated_at": iso_ist(now_ist()),
        "source": {"provider": "DHAN", "method": "instrument_master_detailed", "file": source_file},
        "status": status,
        "status_reason": status_reason,
        "count": len(build.stocks),
        "stocks": [s.to_dict() for s in build.stocks],
        "unmapped_sector": build.unmapped_sector,
        "missing_equity": build.missing_equity,
        "mapped_by_symbol": build.mapped_by_symbol,
    }


def build_from_file(path: Path, sector_map: SectorMap) -> UniverseBuild:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return parse_instrument_master(fh, sector_map)


def build_from_text(text: str, sector_map: SectorMap) -> UniverseBuild:
    return parse_instrument_master(io.StringIO(text), sector_map)


def stocks_from_document(doc: dict) -> list[UniverseStock]:
    return [UniverseStock(symbol=s["symbol"], security_id=str(s["security_id"]), sector=s["sector"],
                          lot_size=s.get("lot_size"), name=s.get("name"))
            for s in doc.get("stocks", [])]


def resector(stocks: Iterable[UniverseStock], sector_map: SectorMap) -> list[UniverseStock]:
    """Re-apply the current sectors.json (e.g. when falling back to yesterday's universe)."""
    out = []
    for s in stocks:
        out.append(UniverseStock(s.symbol, s.security_id, sector_map.sector_of(s.symbol),
                                 s.lot_size, s.name, s.mapped_by))
    return out
