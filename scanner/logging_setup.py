"""Logging: console + one file per command per day, with secrets scrubbed.

  logs/<command>-YYYY-MM-DD.log     e.g. logs/run-day-2026-10-08.log
                                    (a long-running `serve` rolls over at midnight)

Every handler uses ScrubbingFormatter, so secrets are masked in messages AND
tracebacks: JWT-shaped tokens, `token=` URL parameters, access-token headers,
Bearer values, and the literal values of DHAN_ACCESS_TOKEN, DHAN_PIN,
DHAN_TOTP_SECRET, DASHBOARD_TOKEN, GITHUB_TOKEN and DHAN_CLIENT_ID.
Files older than logging.retention_days are deleted when logging starts.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from .market_clock import IST

MASK = "***"
PATTERNS = [
    re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]*"),          # JWT
    re.compile(r"(?i)(token=)[^&\s'\"]+"),                                              # URL param
    re.compile(r"(?i)(['\"]?access[-_]?token['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+"),       # header / json
    re.compile(r"(?i)(bearer\s+)[^\s'\"]+"),
    re.compile(r"(?i)(\b['\"]?(?:pin|totp|totp_secret|password)['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}&]+"),
]


class Scrubber:
    def __init__(self, literals: Iterable[str] = ()):
        # longest first so a secret containing another is masked whole
        self.literals = sorted({s for s in literals if s and len(s) >= 4}, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for lit in self.literals:
            text = text.replace(lit, MASK)
        for pat in PATTERNS:
            text = pat.sub(lambda m: (m.group(1) if m.groups() else "") + MASK, text)
        return text


class ScrubbingFormatter(logging.Formatter):
    def __init__(self, scrubber: Scrubber, fmt: str):
        super().__init__(fmt)
        self.scrubber = scrubber

    def format(self, record: logging.LogRecord) -> str:
        return self.scrubber(super().format(record))


class DailyFileHandler(logging.Handler):
    """Appends to <dir>/<prefix>-YYYY-MM-DD.log (IST date), switching file at midnight."""

    def __init__(self, directory: Path, prefix: str, clock=lambda: datetime.now(IST)):
        super().__init__()
        self.directory = directory
        self.prefix = prefix
        self.clock = clock
        self._day: date | None = None
        self._stream = None

    def path_for(self, day: date) -> Path:
        return self.directory / f"{self.prefix}-{day.isoformat()}.log"

    def emit(self, record: logging.LogRecord) -> None:
        try:
            day = self.clock().date()
            if day != self._day or self._stream is None:
                self._open(day)
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
        except Exception:  # noqa: BLE001
            self.handleError(record)

    def _open(self, day: date) -> None:
        if self._stream:
            self._stream.close()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._stream = open(self.path_for(day), "a", encoding="utf-8")
        self._day = day

    def close(self) -> None:
        try:
            if self._stream:
                self._stream.close()
                self._stream = None
        finally:
            super().close()


def prune_logs(directory: Path, retention_days: int, now: float | None = None) -> list[Path]:
    """Delete *.log files older than retention_days (by modification time)."""
    if not directory.is_dir():
        return []
    cutoff = (now or time.time()) - retention_days * 86400
    removed = []
    for p in directory.glob("*.log"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed.append(p)
        except OSError:
            pass
    return removed


def secret_literals(settings) -> list[str]:
    s = settings.secrets
    return [s.dhan_access_token, s.dhan_pin, s.dhan_totp_secret, s.dashboard_token, s.github_token,
            s.dhan_client_id]


def setup_logging(command: str, settings=None, verbose: bool = False) -> Scrubber:
    """Configure the root logger. Without settings (config broken) only the console is used."""
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    scrubber = Scrubber(secret_literals(settings) if settings else ())
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    if sys.stderr is not None:          # pythonw.exe (scheduled tasks) has no console
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(ScrubbingFormatter(scrubber, fmt))
        root.addHandler(console)

    if settings is not None:
        log_dir = settings.path("logging.dir") if settings.get("logging.dir") else settings.root / "logs"
        prune_logs(log_dir, int(settings.get("logging.retention_days", 30)))
        fh = DailyFileHandler(log_dir, command)
        fh.setFormatter(ScrubbingFormatter(scrubber, fmt))
        root.addHandler(fh)
    # third-party chatter
    for name in ("urllib3", "websocket", "httpx", "multipart"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return scrubber


def close_file_logs() -> None:
    """Flush and close the daily log files (Windows keeps open files locked)."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, DailyFileHandler):
            root.removeHandler(h)
            h.close()
