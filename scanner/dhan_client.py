"""Thin, defensive client for the Dhan v2 REST data APIs used by the scanner.

Covered here (Phase 1):
  GET  /v2/profile                 token + data-plan check
  POST /v2/marketfeed/quote        batched quotes (<=1000 ids, 1 req/s)
  POST /v2/charts/historical       daily candles (toDate is NON-inclusive)
  GET  instrument master CSV       downloaded to a local file

The WebSocket live feed is in scanner/live_feed.py.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

from .auth import TokenError, TokenProvider
from .market_clock import epoch_to_ist

log = logging.getLogger(__name__)

AUTH_CODES = {"DH-901", "DH-902", "806", "807", "808", "809", "810"}
RATE_CODES = {"DH-904", "804", "805"}


class DhanError(RuntimeError):
    def __init__(self, message: str, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class DhanAuthError(DhanError):
    """Token invalid/expired or account not permitted."""


class DhanDataPlanError(DhanAuthError):
    """Data API subscription is not active on the account."""


class DhanRateLimitError(DhanError):
    pass


class DhanRequestError(DhanError):
    """Bad request / unexpected response that retrying will not fix."""


@dataclass(frozen=True)
class Candle:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: int


class RateLimiter:
    """Minimum spacing between calls; thread-safe; clock/sleep injectable for tests."""

    def __init__(self, per_second: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.interval = 1.0 / per_second if per_second > 0 else 0.0
        self.clock = clock
        self.sleep = sleep
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self.clock()
            if now < self._next:
                self.sleep(self._next - now)
                now = self._next
            self._next = now + self.interval


def _chunks(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _error_code(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    for key in ("errorCode", "error_code"):
        if body.get(key):
            return str(body[key])
    for container in ("remarks", "data"):
        inner = body.get(container)
        if isinstance(inner, dict):
            for key in ("error_code", "errorCode"):
                if inner.get(key):
                    return str(inner[key])
            # data API errors are sometimes {"data": {"806": "message"}}
            for k in inner:
                if str(k).isdigit() and 800 <= int(k) <= 899:
                    return str(k)
    return None


def _error_message(body: Any, fallback: str) -> str:
    if isinstance(body, dict):
        for key in ("errorMessage", "error_message", "message"):
            if body.get(key):
                return str(body[key])
        for container in ("remarks", "data"):
            inner = body.get(container)
            if isinstance(inner, dict):
                for v in inner.values():
                    if isinstance(v, str):
                        return v
    return fallback


class DhanClient:
    def __init__(
        self,
        client_id: str,
        token_provider: TokenProvider,
        base_url: str = "https://api.dhan.co/v2",
        session: Any | None = None,
        timeout: float = 15,
        max_retries: int = 4,
        quote_batch_size: int = 1000,
        quote_rps: float = 1,
        historical_rps: float = 4,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not client_id:
            raise DhanAuthError("DHAN_CLIENT_ID is not set")
        self.client_id = client_id
        self.tokens = token_provider
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_retries = max_retries
        self.quote_batch_size = min(int(quote_batch_size), 1000)
        self.sleep = sleep
        self.quote_limiter = RateLimiter(quote_rps, clock, sleep)
        self.history_limiter = RateLimiter(historical_rps, clock, sleep)
        self.general_limiter = RateLimiter(5, clock, sleep)

    @classmethod
    def from_settings(cls, settings, token_provider: TokenProvider, session: Any | None = None) -> "DhanClient":
        g = settings.get
        return cls(
            client_id=settings.secrets.dhan_client_id,
            token_provider=token_provider,
            base_url=g("dhan.base_url", "https://api.dhan.co/v2"),
            session=session,
            timeout=g("dhan.request_timeout_seconds", 15),
            max_retries=g("dhan.max_retries", 4),
            quote_batch_size=g("dhan.quote_batch_size", 1000),
            quote_rps=g("dhan.quote_requests_per_second", 1),
            historical_rps=g("dhan.historical_requests_per_second", 4),
        )

    # ------------------------------------------------------------------
    def _headers(self, token: str) -> dict[str, str]:
        return {
            "access-token": token,
            "client-id": self.client_id,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, limiter: RateLimiter, payload: Any = None) -> Any:
        url = f"{self.base_url}{path}"
        try:
            token = self.tokens.get_token()
        except TokenError as exc:
            raise DhanAuthError(str(exc)) from exc

        refreshed = False
        attempt = 0
        while True:
            attempt += 1
            limiter.wait()
            try:
                resp = self.session.request(method, url, headers=self._headers(token),
                                            json=payload, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt > self.max_retries:
                    raise DhanError(f"{method} {path}: network error after {attempt} attempts: {exc}") from exc
                self._backoff(attempt, f"network error: {exc}")
                continue

            try:
                body = resp.json()
            except ValueError:
                body = None
            code = _error_code(body)
            status = resp.status_code

            if status == 200 and code is None and not (isinstance(body, dict) and body.get("status") == "failure"):
                return body

            msg = _error_message(body, (getattr(resp, "text", "") or "")[:200])

            # --- auth ---
            if status in (401, 403) or (code in AUTH_CODES):
                if "subscri" in msg.lower() or "data plan" in msg.lower():
                    raise DhanDataPlanError(f"Dhan Data API subscription not active: {msg}", code, status)
                if not refreshed:
                    refreshed = True
                    try:
                        token = self.tokens.refresh()
                        log.warning("Dhan auth failed (%s %s); token refreshed, retrying", status, code)
                        continue
                    except (TokenError, NotImplementedError) as exc:
                        raise DhanAuthError(f"Dhan auth failed ({status} {code}): {msg}. "
                                            f"Token refresh not possible: {exc}", code, status) from exc
                raise DhanAuthError(f"Dhan auth failed ({status} {code}): {msg}", code, status)

            # --- retryable ---
            if status == 429 or status >= 500 or code in RATE_CODES:
                if attempt > self.max_retries:
                    cls = DhanRateLimitError if (status == 429 or code in RATE_CODES) else DhanError
                    raise cls(f"{method} {path}: gave up after {attempt} attempts ({status} {code}): {msg}",
                              code, status)
                self._backoff(attempt, f"HTTP {status} {code or ''}")
                continue

            raise DhanRequestError(f"{method} {path}: HTTP {status} {code or ''}: {msg}", code, status)

    def _backoff(self, attempt: int, reason: str) -> None:
        delay = min(2 ** (attempt - 1), 16)
        log.warning("Dhan request retry %d in %ss (%s)", attempt, delay, reason)
        self.sleep(delay)

    # ------------------------------------------------------------------
    def profile(self) -> dict[str, Any]:
        return self._request("GET", "/profile", self.general_limiter)

    def ensure_data_plan(self) -> dict[str, Any]:
        prof = self.profile()
        if str(prof.get("dataPlan", "")).lower() != "active":
            raise DhanDataPlanError(
                f"Dhan Data API plan is '{prof.get('dataPlan')}' (validity {prof.get('dataValidity')}). "
                "Subscribe to Data APIs on web.dhan.co.")
        return prof

    def quote(self, security_ids: Iterable[str | int], segment: str = "NSE_EQ") -> dict[str, dict]:
        """Full quote for many instruments; returns {security_id(str): quote}.
        Ids missing from Dhan's response are simply absent from the result."""
        ids = sorted({str(i) for i in security_ids}, key=lambda x: int(x) if x.isdigit() else x)
        out: dict[str, dict] = {}
        for batch in _chunks(ids, self.quote_batch_size):
            payload = {segment: [int(i) if i.isdigit() else i for i in batch]}
            body = self._request("POST", "/marketfeed/quote", self.quote_limiter, payload)
            seg_data = ((body or {}).get("data") or {}).get(segment) or {}
            for sid, q in seg_data.items():
                if isinstance(q, dict):
                    out[str(sid)] = q
        return out

    def daily_history(self, security_id: str, from_date: date, to_date: date,
                      segment: str = "NSE_EQ", instrument: str = "EQUITY") -> list[Candle]:
        """Daily candles in [from_date, to_date) — Dhan's toDate is non-inclusive."""
        payload = {
            "securityId": str(security_id),
            "exchangeSegment": segment,
            "instrument": instrument,
            "expiryCode": 0,
            "oi": False,
            "fromDate": from_date.isoformat(),
            "toDate": to_date.isoformat(),
        }
        body = self._request("POST", "/charts/historical", self.history_limiter, payload) or {}
        ts = body.get("timestamp") or []
        candles = []
        for i, t in enumerate(ts):
            try:
                candles.append(Candle(
                    day=epoch_to_ist(t).date(),
                    open=float(body["open"][i]), high=float(body["high"][i]),
                    low=float(body["low"][i]), close=float(body["close"][i]),
                    volume=int(body.get("volume", [0] * len(ts))[i] or 0),
                ))
            except (KeyError, IndexError, TypeError, ValueError):
                log.warning("Skipping malformed candle %d for %s", i, security_id)
        candles.sort(key=lambda c: c.day)
        return candles



def download_file(url: str, dest: Path, session: Any | None = None, max_retries: int = 4,
                  sleep: Callable[[float], None] = time.sleep) -> Path:
    """Stream a public file (e.g. the ~30MB instrument master) to disk atomically.
    No Dhan credentials needed."""
    session = session or requests.Session()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 2):
        try:
            with session.get(url, stream=True, timeout=60) as resp:
                if resp.status_code != 200:
                    raise DhanError(f"Download HTTP {resp.status_code} for {url}")
                with tmp.open("wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1 << 16):
                        if chunk:
                            fh.write(chunk)
            tmp.replace(dest)
            return dest
        except (requests.RequestException, DhanError) as exc:
            last_exc = exc
            if attempt > max_retries:
                break
            delay = min(2 ** (attempt - 1), 16)
            log.warning("Download retry %d in %ss (%s)", attempt, delay, exc)
            sleep(delay)
    raise DhanError(f"Could not download {url}: {last_exc}")
