"""Minimal NSE website client for the pre-open market data (no credentials).

NSE's JSON endpoints need the session cookies its web pages set, so the client
first loads the home page and the pre-open page, then calls the API. A 401/403
re-primes the cookies once. Network errors and 5xx are retried with backoff.

  GET /api/market-data-pre-open?key=FO    pre-open book for F&O stocks
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import requests

from .market_clock import IST

log = logging.getLogger(__name__)

BASE_URL = "https://www.nseindia.com"
PREOPEN_PAGE = "/market-data/pre-open-market-cm-and-emerge-market"
PREOPEN_API = "/api/market-data-pre-open"

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


class NseError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreopenQuote:
    symbol: str
    iep: float | None              # final / indicative equilibrium price
    prev_close: float | None       # NSE's previous close
    final_quantity: int | None
    last_update: datetime | None


def parse_nse_time(value: Any) -> datetime | None:
    """NSE timestamps look like '07-Oct-2026 09:08:00'."""
    if not value:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%m-%Y %H:%M:%S"):
        try:
            return datetime.strptime(str(value).strip(), fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def _pos(x: Any) -> float | None:
    try:
        v = float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return round(v, 2) if v > 0 else None


def _int(x: Any) -> int | None:
    try:
        return int(float(str(x).replace(",", "")))
    except (TypeError, ValueError):
        return None


def parse_preopen(body: Any) -> tuple[datetime | None, dict[str, PreopenQuote]]:
    """Return (snapshot timestamp, {symbol: PreopenQuote}) from the API body."""
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise NseError("Unexpected NSE pre-open response (no 'data' list)")
    out: dict[str, PreopenQuote] = {}
    for item in body["data"]:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata") or {}
        pom = (item.get("detail") or {}).get("preOpenMarket") or {}
        sym = str(meta.get("symbol") or "").strip().upper()
        if not sym:
            continue
        iep = _pos(pom.get("IEP")) or _pos(pom.get("finalPrice")) or _pos(meta.get("iep"))
        out[sym] = PreopenQuote(
            symbol=sym,
            iep=iep,
            prev_close=_pos(meta.get("previousClose")) or _pos(pom.get("prevClose")),
            final_quantity=_int(pom.get("finalQuantity") or meta.get("finalQuantity")),
            last_update=parse_nse_time(pom.get("lastUpdateTime")),
        )
    return parse_nse_time(body.get("timestamp")), out


class NseClient:
    def __init__(self, session: Any | None = None, base_url: str = BASE_URL, timeout: float = 10,
                 max_retries: int = 2, sleep: Callable[[float], None] = time.sleep):
        self.session = session or requests.Session()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.sleep = sleep
        self._primed = False

    @classmethod
    def from_settings(cls, settings, session: Any | None = None) -> "NseClient":
        g = settings.get
        return cls(session=session, base_url=g("nse.base_url", BASE_URL),
                   timeout=g("nse.request_timeout_seconds", 10), max_retries=g("nse.max_retries", 2))

    def _get(self, path: str, params: dict | None = None, referer: str | None = None) -> Any:
        headers = dict(BROWSER_HEADERS)
        if referer:
            headers["Referer"] = referer
        return self.session.request("GET", f"{self.base_url}{path}", params=params, headers=headers,
                                    timeout=self.timeout)

    def _prime(self) -> None:
        for path in ("/", PREOPEN_PAGE):
            self._get(path)
        self._primed = True

    def preopen(self, key: str = "FO") -> dict:
        reprimed = False
        attempt = 0
        while True:
            attempt += 1
            try:
                if not self._primed:
                    self._prime()
                resp = self._get(PREOPEN_API, {"key": key}, referer=f"{self.base_url}{PREOPEN_PAGE}")
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt > self.max_retries:
                    raise NseError(f"NSE network error after {attempt} attempts: {exc}") from exc
                self._backoff(attempt, f"network error: {exc}")
                continue

            status = resp.status_code
            if status == 200:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise NseError("NSE returned a non-JSON response (possibly blocked)") from exc
            if status in (401, 403) and not reprimed:
                reprimed = True
                self._primed = False
                log.warning("NSE returned %s; refreshing cookies", status)
                continue
            if status == 429 or status >= 500:
                if attempt > self.max_retries:
                    raise NseError(f"NSE HTTP {status} after {attempt} attempts")
                self._backoff(attempt, f"HTTP {status}")
                continue
            raise NseError(f"NSE HTTP {status}: {(getattr(resp, 'text', '') or '')[:200]}")

    def _backoff(self, attempt: int, reason: str) -> None:
        delay = min(2 ** (attempt - 1), 8)
        log.warning("NSE retry %d in %ss (%s)", attempt, delay, reason)
        self.sleep(delay)
