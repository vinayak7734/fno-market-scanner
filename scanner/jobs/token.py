"""Job: keep the Dhan access token valid and record its expiry (never the token).

ensure_token() renews (manual: RenewToken) or regenerates (TOTP) the token when
it would expire before `must_last_until`, then writes
status.json -> "token": {method, expires_at, state, last_check, last_result, message}
which /api/health and the dashboard use for the expiry banner.

Used by run-day's token_check step (must last until eod_archive) and by
`renew-token --if-needed` (must last renew_min_hours from now), which the
daily Windows task runs every day — weekends included — so a manual token
never lapses over a weekend while the PC is on.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from ..auth import ManualTokenProvider, TokenError
from ..market_clock import iso_ist, now_ist
from ..storage import Storage

log = logging.getLogger(__name__)
JOB = "token"


def check_token(tokens, must_last_until: datetime) -> str:
    """Renew/refresh the token if it would expire before `must_last_until`."""
    exp = tokens.expires_at()
    if exp is not None and exp >= must_last_until:
        return f"token valid until {iso_ist(exp)}"
    if isinstance(tokens, ManualTokenProvider):
        tokens.renew()
    else:
        tokens.refresh()
    exp = tokens.expires_at()
    if exp is not None and exp < must_last_until:
        raise TokenError(f"Dhan token expires at {iso_ist(exp)}, before {iso_ist(must_last_until)}; "
                         "generate a new one on web.dhan.co")
    return f"token renewed, valid until {iso_ist(exp) if exp else 'unknown'}"


def token_info(tokens, now: datetime) -> dict:
    try:
        exp = tokens.expires_at()
    except TokenError as exc:
        return {"method": getattr(tokens, "method", None), "expires_at": None, "state": "missing",
                "detail": str(exc)}
    if exp is None:
        state = "unknown"
    elif exp <= now:
        state = "expired"
    else:
        state = "ok"
    return {"method": getattr(tokens, "method", None), "expires_at": iso_ist(exp) if exp else None, "state": state}


def ensure_token(storage: Storage, tokens, must_last_until: datetime,
                 now_fn: Callable[[], datetime] = now_ist) -> str:
    try:
        message = check_token(tokens, must_last_until)
        result = "ok"
    except Exception as exc:  # noqa: BLE001 — TokenError, HTTP errors, NotImplementedError ...
        message = f"{type(exc).__name__}: {exc}" if not isinstance(exc, TokenError) else str(exc)
        result = "failed"
    now = now_fn()
    info = token_info(tokens, now)
    info.update({"last_check": iso_ist(now), "last_result": result, "message": message,
                 "needed_until": iso_ist(must_last_until)})
    storage.record_token(info)
    storage.record_job(JOB, result, message, [f"Dhan token: {message}"] if result == "failed" else [])
    log.info("token: %s — %s", result, message)
    if result == "failed":
        raise TokenError(message)
    return message
