"""OS helpers for unattended runs: keep-awake, single-instance lock, pre-flight checks.

keep_awake()   asks Windows not to sleep while the block runs
               (SetThreadExecutionState). It changes no power settings and ends
               when the block exits or the process dies. No-op elsewhere.
InstanceLock   data/state/<name>.lock holding the owner's PID; a lock whose
               process is gone is treated as stale and replaced.
preflight()    alerts for next year's holiday file (from Dec 1) and low disk.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import sys
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


@contextlib.contextmanager
def keep_awake(enabled: bool = True, kernel32=None):
    k32 = kernel32
    if enabled and k32 is None and sys.platform == "win32":
        import ctypes

        k32 = ctypes.windll.kernel32
    if not enabled or k32 is None:
        yield False
        return
    ok = bool(k32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED))
    if ok:
        log.info("Keep-awake on: Windows will not sleep while this run is active")
    else:
        log.warning("Keep-awake request was refused by Windows")
    try:
        yield ok
    finally:
        if ok:
            k32.SetThreadExecutionState(ES_CONTINUOUS)
            log.info("Keep-awake off")


# ---------------------------------------------------------------------------
def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) would TERMINATE the process on Windows, so ask the kernel instead.
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, path: Path, label: str, alive=pid_alive):
        self.path = path
        self.label = label
        self.alive = alive
        self.held = False

    def acquire(self) -> "InstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                owner = self._owner()
                if owner and self.alive(owner.get("pid", -1)):
                    raise AlreadyRunning(
                        f"Another {owner.get('label', 'scanner')} run is active (PID {owner['pid']}, "
                        f"started {owner.get('started', '?')}). Lock: {self.path}") from None
                log.warning("Removing stale lock %s (PID %s is not running)", self.path,
                            owner.get("pid") if owner else "?")
                with contextlib.suppress(FileNotFoundError):
                    self.path.unlink()
                continue
            from .market_clock import iso_ist, now_ist

            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"pid": os.getpid(), "label": self.label, "started": iso_ist(now_ist())}, fh)
            self.held = True
            return self
        raise AlreadyRunning(f"Could not take lock {self.path}")

    def _owner(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def release(self) -> None:
        if self.held:
            owner = self._owner()
            if owner and owner.get("pid") == os.getpid():
                with contextlib.suppress(FileNotFoundError):
                    self.path.unlink()
            self.held = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()


# ---------------------------------------------------------------------------
def preflight(settings, today: date, disk_usage=shutil.disk_usage) -> list[str]:
    alerts = []
    config_dir = settings.root / "config"
    check_from = str(settings.get("preflight.holiday_check_from", "12-01"))
    month, day = (int(x) for x in check_from.split("-"))
    if (today.month, today.day) >= (month, day):
        nxt = config_dir / f"nse_holidays_{today.year + 1}.json"
        if not nxt.exists():
            alerts.append(f"{nxt.name} is missing: add next year's NSE trading holidays before January "
                          f"(copy the format of nse_holidays_{today.year}.json)")
    if not (config_dir / f"nse_holidays_{today.year}.json").exists():
        alerts.append(f"nse_holidays_{today.year}.json is missing: holidays will not be skipped")
    min_mb = float(settings.get("preflight.min_free_disk_mb", 1024))
    try:
        free_mb = disk_usage(settings.data_dir if settings.data_dir.exists() else settings.root).free / 2**20
        if free_mb < min_mb:
            alerts.append(f"Low disk space: {free_mb:.0f} MB free (alert below {min_mb:.0f} MB)")
    except OSError as exc:
        alerts.append(f"Could not check disk space: {exc}")
    return alerts
