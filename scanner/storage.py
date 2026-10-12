"""Atomic JSON storage for datasets, snapshots and job status."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from .market_clock import iso_ist, now_ist

DATASETS = ("universe", "previous_close", "premarket", "live", "sectors", "status")


def write_json_atomic(path: Path, data: Any) -> None:
    """Write to a temp file in the same directory, fsync, then rename.
    Readers never see a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


class Storage:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.latest_dir = data_dir / "latest"
        self.snapshots_dir = data_dir / "snapshots"
        self.state_dir = data_dir / "state"

    def latest_path(self, dataset: str) -> Path:
        if dataset not in DATASETS:
            raise ValueError(f"Unknown dataset: {dataset}")
        return self.latest_dir / f"{dataset}.json"

    def snapshot_path(self, trade_date: date | str, dataset: str) -> Path:
        return self.snapshots_dir / str(trade_date) / f"{dataset}.json"

    def write_dataset(self, dataset: str, data: dict, snapshot: bool = True) -> Path:
        path = self.latest_path(dataset)
        write_json_atomic(path, data)
        if snapshot and data.get("trade_date"):
            write_json_atomic(self.snapshot_path(data["trade_date"], dataset), data)
        return path

    def read_dataset(self, dataset: str) -> dict | None:
        return read_json(self.latest_path(dataset))

    # ---- status.json ----------------------------------------------------
    def record_job(self, job: str, result: str, message: str = "", alerts: list[str] | None = None) -> None:
        status = read_json(self.latest_path("status"), default={}) or {}
        status.setdefault("jobs", {})[job] = {
            "last_run": iso_ist(now_ist()),
            "result": result,
            "message": message,
        }
        all_alerts = [a for a in status.get("alerts", []) if a.get("job") != job]
        for a in alerts or []:
            all_alerts.append({"job": job, "message": a, "at": iso_ist(now_ist())})
        status["alerts"] = all_alerts
        status["updated_at"] = iso_ist(now_ist())
        write_json_atomic(self.latest_path("status"), status)

    def record_token(self, info: dict) -> None:
        """Token expiry metadata for the dashboard — never the token itself."""
        status = read_json(self.latest_path("status"), default={}) or {}
        status["token"] = {k: v for k, v in info.items() if k != "access_token"}
        status["updated_at"] = iso_ist(now_ist())
        write_json_atomic(self.latest_path("status"), status)
