"""Sector configuration (config/sectors.json): load, validate, look up."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

EXPECTED_SECTOR_COUNT = 19
UNMAPPED = "Unmapped"


class SectorConfigError(ValueError):
    pass


@dataclass(frozen=True)
class SectorMap:
    sectors: dict[str, tuple[str, ...]]   # sector -> symbols (config order)
    by_symbol: dict[str, str]             # symbol -> sector

    def sector_of(self, symbol: str) -> str:
        return self.by_symbol.get(symbol.upper(), UNMAPPED)

    @property
    def names(self) -> list[str]:
        return list(self.sectors)


def parse_sectors(data: dict, expected_count: int | None = EXPECTED_SECTOR_COUNT) -> SectorMap:
    raw = data.get("sectors")
    if not isinstance(raw, dict) or not raw:
        raise SectorConfigError("sectors.json must contain a non-empty 'sectors' object")
    if expected_count is not None and len(raw) != expected_count:
        raise SectorConfigError(f"sectors.json defines {len(raw)} sectors, expected {expected_count}")

    sectors: dict[str, tuple[str, ...]] = {}
    by_symbol: dict[str, str] = {}
    dupes: list[str] = []
    for name, symbols in raw.items():
        if not isinstance(symbols, list):
            raise SectorConfigError(f"Sector '{name}' must be a list of symbols")
        if name == UNMAPPED:
            raise SectorConfigError(f"'{UNMAPPED}' is reserved")
        clean = []
        for s in symbols:
            sym = str(s).strip().upper()
            if not sym:
                continue
            if sym in by_symbol:
                dupes.append(f"{sym} ({by_symbol[sym]} / {name})")
                continue
            by_symbol[sym] = name
            clean.append(sym)
        sectors[name] = tuple(clean)
    if dupes:
        raise SectorConfigError("Symbols in more than one sector: " + ", ".join(dupes))
    return SectorMap(sectors=sectors, by_symbol=by_symbol)


def load_sectors(path: Path, expected_count: int | None = EXPECTED_SECTOR_COUNT) -> SectorMap:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SectorConfigError(f"Sector file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SectorConfigError(f"Sector file is not valid JSON: {exc}") from exc
    return parse_sectors(data, expected_count)
