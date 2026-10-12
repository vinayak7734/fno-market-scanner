"""Dhan access-token management.

Two methods (settings: dhan.token_method):

* manual — you generate a 24h token on web.dhan.co and put it in .env
  (DHAN_ACCESS_TOKEN). `renew()` calls Dhan's RenewToken API to extend an
  *active* web token by 24h; an expired token cannot be renewed.
* totp   — fully automated. Requires TOTP enabled on your Dhan account;
  a fresh token is generated from client ID + PIN + TOTP code.

The current token is cached in data/state/token.json (git-ignored) so a
renewed/generated token survives restarts.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import struct
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from .market_clock import IST, iso_ist, now_ist
from .storage import read_json, write_json_atomic

REFRESH_MARGIN = timedelta(minutes=30)


class TokenError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# TOTP (RFC 6238) — stdlib only
# ---------------------------------------------------------------------------
def generate_totp(secret_b32: str, for_time: float | None = None, step: int = 30, digits: int = 6) -> str:
    secret = secret_b32.strip().replace(" ", "").upper()
    secret += "=" * (-len(secret) % 8)
    try:
        key = base64.b32decode(secret, casefold=True)
    except Exception as exc:  # noqa: BLE001
        raise TokenError("DHAN_TOTP_SECRET is not valid base32") from exc
    counter = int((time.time() if for_time is None else for_time) // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


# ---------------------------------------------------------------------------
# Token expiry helpers
# ---------------------------------------------------------------------------
def jwt_expiry(token: str) -> datetime | None:
    """Read the `exp` claim from a JWT without verifying it."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return datetime.fromtimestamp(int(exp), tz=IST) if exp else None
    except Exception:  # noqa: BLE001
        return None


def parse_dhan_expiry(value: str | None) -> datetime | None:
    """Dhan returns expiryTime like '2026-01-01T00:00:00.000' (IST, naive)."""
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Token cache
# ---------------------------------------------------------------------------
class TokenStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any] | None:
        return read_json(self.path)

    def save(self, token: str, expires_at: datetime | None, source: str) -> None:
        write_json_atomic(self.path, {
            "access_token": token,
            "expires_at": iso_ist(expires_at) if expires_at else None,
            "source": source,
            "saved_at": iso_ist(now_ist()),
        })


class HttpLike(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> Any: ...


class TokenProvider:
    method = "base"

    def get_token(self) -> str:
        raise NotImplementedError

    def refresh(self) -> str:
        """Obtain a new token (called on 401). Raise TokenError if impossible."""
        raise NotImplementedError

    def expires_at(self) -> datetime | None:
        return None


def _valid(expires: datetime | None, now: datetime) -> bool:
    return expires is None or expires - now > REFRESH_MARGIN


def _expiry_of(cached: dict[str, Any]) -> datetime | None:
    raw = cached.get("expires_at")
    return datetime.fromisoformat(raw) if raw else jwt_expiry(cached.get("access_token", ""))


class ManualTokenProvider(TokenProvider):
    method = "manual"

    def __init__(self, client_id: str, env_token: str, store: TokenStore, http: HttpLike,
                 base_url: str, clock=now_ist):
        if not client_id:
            raise TokenError("DHAN_CLIENT_ID is not set")
        self.client_id = client_id
        self.env_token = env_token
        self.store = store
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.clock = clock

    def _current(self) -> tuple[str, datetime | None]:
        cached = self.store.load()
        if cached and cached.get("access_token"):
            exp = _expiry_of(cached)
            env_exp = jwt_expiry(self.env_token) if self.env_token else None
            # A token pasted into .env that is newer than the cache wins.
            if not (self.env_token and env_exp and exp and env_exp > exp):
                return cached["access_token"], exp
        if not self.env_token:
            raise TokenError("No Dhan token: set DHAN_ACCESS_TOKEN in .env (token_method=manual)")
        return self.env_token, jwt_expiry(self.env_token)

    def get_token(self) -> str:
        token, exp = self._current()
        if exp and exp <= self.clock():
            raise TokenError(
                f"Dhan access token expired at {iso_ist(exp)}. Generate a new one on web.dhan.co "
                "and update DHAN_ACCESS_TOKEN (expired tokens cannot be renewed).")
        return token

    def expires_at(self) -> datetime | None:
        return self._current()[1]

    def renew(self) -> str:
        token = self.get_token()
        resp = self.http.request(
            "GET", f"{self.base_url}/RenewToken",
            headers={"access-token": token, "dhanClientId": self.client_id, "Accept": "application/json"},
            timeout=15,
        )
        if resp.status_code != 200:
            raise TokenError(f"RenewToken failed: HTTP {resp.status_code} {resp.text[:200]}")
        body = resp.json()
        new_token = body.get("accessToken") or body.get("token") or body.get("access_token")
        if not new_token:
            raise TokenError(f"RenewToken returned no token: {str(body)[:200]}")
        exp = parse_dhan_expiry(body.get("expiryTime")) or jwt_expiry(new_token)
        self.store.save(new_token, exp, "renew")
        return new_token

    def refresh(self) -> str:
        return self.renew()


class TotpTokenProvider(TokenProvider):
    method = "totp"

    def __init__(self, client_id: str, pin: str, totp_secret: str, store: TokenStore, http: HttpLike,
                 auth_url: str, clock=now_ist, totp_fn=generate_totp):
        missing = [n for n, v in (("DHAN_CLIENT_ID", client_id), ("DHAN_PIN", pin),
                                  ("DHAN_TOTP_SECRET", totp_secret)) if not v]
        if missing:
            raise TokenError(f"token_method=totp needs {', '.join(missing)} in .env")
        self.client_id = client_id
        self.pin = pin
        self.totp_secret = totp_secret
        self.store = store
        self.http = http
        self.auth_url = auth_url.rstrip("/")
        self.clock = clock
        self.totp_fn = totp_fn

    def get_token(self) -> str:
        cached = self.store.load()
        if cached and cached.get("access_token") and _valid(_expiry_of(cached), self.clock()):
            return cached["access_token"]
        return self.refresh()

    def expires_at(self) -> datetime | None:
        cached = self.store.load()
        return _expiry_of(cached) if cached else None

    def refresh(self) -> str:
        resp = self.http.request(
            "POST", f"{self.auth_url}/app/generateAccessToken",
            params={"dhanClientId": self.client_id, "pin": self.pin, "totp": self.totp_fn(self.totp_secret)},
            timeout=15,
        )
        if resp.status_code != 200:
            raise TokenError(f"generateAccessToken failed: HTTP {resp.status_code} {resp.text[:200]}")
        body = resp.json()
        token = body.get("accessToken")
        if not token:
            raise TokenError(f"generateAccessToken returned no token: {str(body)[:200]}")
        exp = parse_dhan_expiry(body.get("expiryTime")) or jwt_expiry(token) or self.clock() + timedelta(hours=24)
        self.store.save(token, exp, "totp")
        return token


def make_token_provider(settings, http: HttpLike) -> TokenProvider:
    s = settings.secrets
    store = TokenStore(settings.data_dir / "state" / "token.json")
    if settings.get("dhan.token_method", "manual") == "totp":
        return TotpTokenProvider(s.dhan_client_id, s.dhan_pin, s.dhan_totp_secret, store, http,
                                 settings.require("dhan.auth_url"))
    return ManualTokenProvider(s.dhan_client_id, s.dhan_access_token, store, http,
                               settings.require("dhan.base_url"))
