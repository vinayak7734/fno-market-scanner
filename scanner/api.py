"""Read-only HTTP API + dashboard (FastAPI).

  GET /                                  dashboard (static page, no data in it)
  GET /api/health                        server time, market phase, jobs, file freshness
  GET /api/status                        status.json
  GET /api/{dataset}                     universe | previous-close | premarket | live | sectors
  GET /api/snapshots                     archived dates and the datasets each has
  GET /api/snapshots/{date}/{dataset}    one archived file

Every /api route requires the dashboard token when one is configured
(header "Authorization: Bearer <token>" or "X-Dashboard-Token: <token>",
compared in constant time). Tokens are never accepted in the URL. The static
dashboard page is public: it holds no data and asks for the token itself.

Files are served exactly as the jobs wrote them (atomic writes, so a read never
sees half a file), with an ETag so the dashboard's 5-second polling gets a
304 when nothing changed. Nothing here writes to disk.
"""

from __future__ import annotations

import ipaddress
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import ConfigError
from .market_clock import MarketCalendar, iso_ist, now_ist
from .storage import Storage, read_json

WEB_DIR = Path(__file__).resolve().parent / "web"

# URL name -> dataset file name
DATASETS = {
    "universe": "universe",
    "previous-close": "previous_close",
    "premarket": "premarket",
    "live": "live",
    "sectors": "sectors",
}
SNAPSHOT_DATASETS = ("universe", "previous_close", "premarket", "live", "sectors", "status")
PRODUCED_BY = {
    "universe": "python -m scanner build-universe",
    "previous_close": "python -m scanner previous-close",
    "premarket": "python -m scanner premarket",
    "live": "python -m scanner live",
    "sectors": "python -m scanner sectors",
    "status": "any scanner job",
}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NO_STORE = {"Cache-Control": "no-store"}


class ApiConfigError(ConfigError):
    pass


def is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_bind(host: str, token: str) -> None:
    """Refuse to listen beyond this PC without a dashboard token."""
    if not is_loopback(host) and not token:
        raise ApiConfigError(
            f"Refusing to serve on {host} without DASHBOARD_TOKEN. Set DASHBOARD_TOKEN in .env "
            "(other devices on your Wi-Fi will need it), or use --host 127.0.0.1 for this PC only.")


def token_health(info: dict | None, now: datetime, warn_hours: float) -> dict | None:
    """Dhan token expiry as last recorded by the jobs (metadata only, never the token).

    state: ok | expiring (< warn_hours left) | expired | renewal_failed | unknown
    """
    if not info:
        return None
    out = {k: info.get(k) for k in ("method", "expires_at", "last_check", "last_result", "message")}
    hours_left = None
    if info.get("expires_at"):
        try:
            hours_left = round((datetime.fromisoformat(info["expires_at"]) - now).total_seconds() / 3600, 2)
        except ValueError:
            hours_left = None
    out["hours_left"] = hours_left
    if hours_left is not None and hours_left <= 0:
        state = "expired"
    elif info.get("last_result") == "failed":
        state = "renewal_failed"
    elif hours_left is None:
        state = "unknown"
    elif hours_left < warn_hours:
        state = "expiring"
    else:
        state = "ok"
    out["state"] = state
    return out


def _etag(path: Path) -> str:
    st = path.stat()
    return f'"{st.st_mtime_ns:x}-{st.st_size:x}"'


def _file_response(request: Request, path: Path) -> Response:
    tag = _etag(path)
    if request.headers.get("if-none-match") == tag:
        return Response(status_code=304, headers={**NO_STORE, "ETag": tag})
    # Read bytes (not a streamed FileResponse): the job may replace the file at any moment.
    return Response(content=path.read_bytes(), media_type="application/json",
                    headers={**NO_STORE, "ETag": tag})


def _missing(dataset: str, where: str) -> HTTPException:
    return HTTPException(status_code=404, detail={
        "error": "not_available",
        "dataset": dataset,
        "message": f"No {dataset}.json {where} yet. It is produced by: {PRODUCED_BY.get(dataset, 'the scanner')}",
    })


def create_app(settings, storage: Storage, calendar: MarketCalendar, token: str = "",
               now_fn: Callable[[], datetime] = now_ist) -> FastAPI:
    app = FastAPI(title="F&O Market Scanner API", version="1.0", docs_url="/api/docs",
                  redoc_url=None, openapi_url="/api/openapi.json")

    def require_token(request: Request) -> None:
        if not token:
            return
        supplied = request.headers.get("x-dashboard-token", "")
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        if not supplied or not secrets.compare_digest(supplied.encode(), token.encode()):
            raise HTTPException(status_code=401, detail={"error": "unauthorized",
                                                         "message": "Dashboard token required"},
                                headers={"WWW-Authenticate": "Bearer"})

    auth = [Depends(require_token)]

    @app.get("/api/health", dependencies=auth)
    def health():
        now = now_fn()
        status = read_json(storage.latest_path("status")) or {}
        files = {}
        for ds in ("universe", "previous_close", "premarket", "live", "sectors"):
            path = storage.latest_path(ds)
            doc = read_json(path)
            if doc is None:
                files[ds] = {"available": False}
                continue
            files[ds] = {
                "available": True,
                "trade_date": doc.get("trade_date"),
                "status": doc.get("status"),
                "generated_at": doc.get("as_of") or doc.get("generated_at"),
                "age_seconds": round(now.timestamp() - path.stat().st_mtime, 1),
            }
        return JSONResponse({
            "server_time": iso_ist(now),
            "market_phase": calendar.phase(now),
            "trade_date": calendar.session_date_for(now).isoformat(),
            "is_trading_day": calendar.is_trading_day(now.date()),
            "jobs": status.get("jobs", {}),
            "alerts": status.get("alerts", []),
            "files": files,
            "token": token_health(status.get("token"), now, float(settings.get("token.warn_hours", 2))),
            "auth_required": bool(token),
        }, headers=NO_STORE)

    @app.get("/api/status", dependencies=auth)
    def status(request: Request):
        path = storage.latest_path("status")
        if not path.exists():
            raise _missing("status", "in data/latest")
        return _file_response(request, path)

    @app.get("/api/snapshots", dependencies=auth)
    def snapshots():
        out = []
        root = storage.snapshots_dir
        if root.is_dir():
            for d in sorted((p for p in root.iterdir() if p.is_dir() and DATE_RE.match(p.name)),
                            key=lambda p: p.name, reverse=True):
                out.append({"date": d.name,
                            "datasets": [ds for ds in SNAPSHOT_DATASETS if (d / f"{ds}.json").is_file()]})
        return JSONResponse({"snapshots": out}, headers=NO_STORE)

    @app.get("/api/snapshots/{day}/{dataset}", dependencies=auth)
    def snapshot(day: str, dataset: str, request: Request):
        ds = DATASETS.get(dataset, dataset)
        if not DATE_RE.match(day) or ds not in SNAPSHOT_DATASETS:
            raise HTTPException(status_code=404, detail={"error": "not_found",
                                                         "message": "Unknown snapshot date or dataset"})
        path = storage.snapshot_path(day, ds)
        if not path.is_file():
            raise _missing(ds, f"for {day}")
        return _file_response(request, path)

    @app.get("/api/{dataset}", dependencies=auth)
    def dataset(dataset: str, request: Request):
        ds = DATASETS.get(dataset)
        if ds is None:
            raise HTTPException(status_code=404, detail={
                "error": "not_found", "message": f"Unknown dataset '{dataset}'",
                "datasets": sorted(DATASETS)})
        path = storage.latest_path(ds)
        if not path.is_file():
            raise _missing(ds, "in data/latest")
        return _file_response(request, path)

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "index.html", headers=NO_STORE)

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app


def lan_address() -> str | None:
    """This PC's address on the local network (no packets are sent)."""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return None


def make_server(app: FastAPI, host: str, port: int):
    import uvicorn

    # log_config=None: uvicorn logs go through our root handlers (file + secret scrubbing)
    config = uvicorn.Config(app, host=host, port=port, log_level="info", access_log=False, log_config=None)
    return uvicorn.Server(config)
