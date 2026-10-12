"""Dhan v2 market-feed WebSocket client.

Protocol (all binary responses little-endian):

  connect    wss://api-feed.dhan.co?version=2&token=..&clientId=..&authType=2
  subscribe  JSON {"RequestCode": 15, "InstrumentCount": n, "InstrumentList": [...]}
             (15 = ticker, max 100 instruments per message)
  response   8-byte header: code u8, length i16, segment u8, security_id i32
               code 2  ticker      LTP f32, LTT i32
               code 4  quote       LTP f32 ... (only LTP is used)
               code 6  prev close  prev close f32, prev OI i32
               code 8  full        LTP f32 ... (only LTP is used)
               code 50 disconnect  reason i16

The access token travels in the connection URL, so the URL is never logged and
the token is scrubbed from any error text.
"""

from __future__ import annotations

import json
import logging
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .auth import TokenError, TokenProvider

log = logging.getLogger(__name__)

FEED_URL = "wss://api-feed.dhan.co"

SEGMENTS = {"IDX_I": 0, "NSE_EQ": 1, "NSE_FNO": 2, "NSE_CURRENCY": 3, "BSE_EQ": 4,
            "MCX_COMM": 5, "BSE_CURRENCY": 7, "BSE_FNO": 8}
SEGMENT_NAMES = {v: k for k, v in SEGMENTS.items()}

REQ_TICKER = 15
REQ_DISCONNECT = 12
SUBSCRIBE_BATCH = 100

RESP_TICKER = 2
RESP_QUOTE = 4
RESP_OI = 5
RESP_PREV_CLOSE = 6
RESP_MARKET_STATUS = 7
RESP_FULL = 8
RESP_DISCONNECT = 50

PACKET_SIZES = {RESP_TICKER: 16, RESP_QUOTE: 50, RESP_OI: 12, RESP_PREV_CLOSE: 16,
                RESP_MARKET_STATUS: 8, RESP_FULL: 162, RESP_DISCONNECT: 10}

# Disconnect reasons that a new token can fix vs. ones it cannot.
AUTH_REASONS = {807, 809}          # token expired / authentication failed
FATAL_REASONS = {805, 806, 808}    # too many connections / no data plan / bad client id

HEADER = struct.Struct("<BhBi")


class FeedError(RuntimeError):
    pass


class FeedAuthError(FeedError):
    pass


@dataclass(frozen=True)
class FeedEvent:
    kind: str                  # "ltp" | "prev_close" | "disconnect"
    security_id: str
    segment: str | None = None
    ltp: float | None = None
    prev_close: float | None = None
    reason: int | None = None


def _price(x: float) -> float | None:
    v = round(float(x), 2)
    return v if v > 0 else None


def parse_message(data: bytes) -> list[FeedEvent]:
    """Decode one WebSocket binary message (may hold several packets)."""
    events: list[FeedEvent] = []
    pos = 0
    while len(data) - pos >= HEADER.size:
        code, length, seg, sid = HEADER.unpack_from(data, pos)
        size = PACKET_SIZES.get(code)
        if not (HEADER.size <= length <= len(data) - pos):
            length = size or 0
        if not length or len(data) - pos < length:
            break
        body = pos + HEADER.size
        segment = SEGMENT_NAMES.get(seg)
        if code in (RESP_TICKER, RESP_QUOTE, RESP_FULL):
            events.append(FeedEvent("ltp", str(sid), segment, ltp=_price(struct.unpack_from("<f", data, body)[0])))
        elif code == RESP_PREV_CLOSE:
            events.append(FeedEvent("prev_close", str(sid), segment,
                                    prev_close=_price(struct.unpack_from("<f", data, body)[0])))
        elif code == RESP_DISCONNECT:
            reason = struct.unpack_from("<h", data, body)[0] if length >= HEADER.size + 2 else None
            events.append(FeedEvent("disconnect", str(sid), segment, reason=reason))
        pos += length
    return events


def subscribe_messages(security_ids: Iterable[str], segment: str = "NSE_EQ",
                       request_code: int = REQ_TICKER, batch: int = SUBSCRIBE_BATCH) -> list[str]:
    ids = [str(i) for i in security_ids]
    out = []
    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        out.append(json.dumps({
            "RequestCode": request_code,
            "InstrumentCount": len(chunk),
            "InstrumentList": [{"ExchangeSegment": segment, "SecurityId": s} for s in chunk],
        }))
    return out


def _default_connect(url: str, timeout: float):
    import websocket  # websocket-client

    return websocket.create_connection(url, timeout=timeout, enable_multithread=True)


class LiveFeed:
    """Blocking feed loop with reconnect; call run() from a worker thread.

    on_event(FeedEvent) receives every decoded packet.
    on_state(state, detail) receives "connecting" | "connected" | "reconnecting" | "stopped" | "failed".
    """

    def __init__(
        self,
        client_id: str,
        tokens: TokenProvider,
        on_event: Callable[[FeedEvent], None],
        on_state: Callable[[str, str | None], None] | None = None,
        url: str = FEED_URL,
        connect: Callable[[str, float], Any] = _default_connect,
        recv_timeout: float = 1.0,
        max_backoff: float = 30.0,
        silence_timeout: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not client_id:
            raise FeedAuthError("DHAN_CLIENT_ID is not set")
        self.client_id = client_id
        self.tokens = tokens
        self.on_event = on_event
        self.on_state = on_state or (lambda s, d: None)
        self.url = url
        self.connect = connect
        self.recv_timeout = recv_timeout
        self.max_backoff = max_backoff
        self.silence_timeout = silence_timeout
        self.sleep = sleep
        self.clock = clock
        self.reconnects = 0
        self._token = ""
        self._was_connected = False

    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    def _feed_url(self) -> str:
        return f"{self.url}?version=2&token={self._token}&clientId={self.client_id}&authType=2"

    def _wait(self, seconds: float, stop: threading.Event) -> None:
        remaining = seconds
        while not stop.is_set() and remaining > 0:
            step = min(0.5, remaining)
            self.sleep(step)
            remaining -= step

    def run(self, security_ids: list[str], stop: threading.Event, segment: str = "NSE_EQ") -> None:
        attempt = 0
        refreshed = False
        try:
            self._token = self.tokens.get_token()
        except TokenError as exc:
            self.on_state("failed", str(exc))
            raise FeedAuthError(str(exc)) from exc

        while not stop.is_set():
            self.on_state("connecting" if self.reconnects == 0 else "reconnecting", None)
            self._was_connected = False
            try:
                self._session(security_ids, stop, segment)
            except FeedAuthError as exc:
                if refreshed:
                    self.on_state("failed", str(exc))
                    raise
                refreshed = True
                log.warning("Feed auth rejected; refreshing token once")
                try:
                    self._token = self.tokens.refresh()
                except (TokenError, NotImplementedError) as rexc:
                    msg = f"{exc}. Token refresh not possible: {self._scrub(str(rexc))}"
                    self.on_state("failed", msg)
                    raise FeedAuthError(msg) from rexc
                continue
            except FeedError as exc:
                self.on_state("failed", str(exc))
                raise
            except Exception as exc:  # noqa: BLE001 — network errors of any flavour
                log.warning("Feed connection error: %s", self._scrub(f"{type(exc).__name__}: {exc}"))
            if stop.is_set():
                break
            if self._was_connected:
                attempt = 0          # a healthy session resets the backoff
            attempt += 1
            self.reconnects += 1
            delay = min(2 ** (attempt - 1), self.max_backoff)
            log.warning("Feed reconnect %d in %ss", attempt, delay)
            self._wait(delay, stop)
        self.on_state("stopped", None)

    def _session(self, security_ids: list[str], stop: threading.Event, segment: str) -> None:
        try:
            ws = self.connect(self._feed_url(), 10.0)
        except Exception as exc:  # noqa: BLE001
            text = self._scrub(str(exc))
            if any(s in text for s in ("401", "403", "Unauthorized", "Forbidden")):
                raise FeedAuthError(f"Feed handshake rejected: {text[:200]}") from None
            raise ConnectionError(text[:200]) from None
        try:
            for msg in subscribe_messages(security_ids, segment):
                ws.send(msg)
            ws.settimeout(self.recv_timeout)
            self._was_connected = True
            self.on_state("connected", f"{len(security_ids)} instruments")
            last_data = self.clock()
            while not stop.is_set():
                try:
                    data = ws.recv()
                except Exception as exc:  # noqa: BLE001
                    if "timed out" in str(exc).lower() or type(exc).__name__ == "WebSocketTimeoutException":
                        # Watchdog: an open socket that has gone quiet is treated as dead.
                        if self.silence_timeout and self.clock() - last_data > self.silence_timeout:
                            raise ConnectionError(f"no feed data for {self.silence_timeout:.0f}s") from None
                        continue
                    raise
                if not data:
                    raise ConnectionError("feed closed by server")
                if isinstance(data, str):
                    continue
                last_data = self.clock()
                for ev in parse_message(data):
                    if ev.kind == "disconnect":
                        if ev.reason in AUTH_REASONS:
                            raise FeedAuthError(f"Feed disconnected: auth reason {ev.reason}")
                        if ev.reason in FATAL_REASONS:
                            raise FeedError(f"Feed disconnected: reason {ev.reason} "
                                            "(805 too many connections, 806 no data plan, 808 bad client id)")
                        raise ConnectionError(f"feed disconnect reason {ev.reason}")
                    self.on_event(ev)
            try:
                ws.send(json.dumps({"RequestCode": REQ_DISCONNECT}))
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
