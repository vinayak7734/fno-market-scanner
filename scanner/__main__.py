"""Command line entry point.

    python -m scanner check-auth          # token + Data API plan check
    python -m scanner renew-token         # manual method: extend web token 24h
    python -m scanner build-universe [--csv PATH]
    python -m scanner previous-close [--force]
    python -m scanner phase1              # build-universe, then previous-close
    python -m scanner premarket           # NSE 9:10 pre-open snapshot
    python -m scanner live [--minutes N] [--force]
    python -m scanner phase2              # premarket, then live until 15:30
    python -m scanner run-day             # the whole trading day on schedule
    python -m scanner sectors             # recompute sectors.json from today's files (no network)
    python -m scanner serve [--host H] [--port P]   # API + dashboard
    python -m scanner run-day --serve     # the trading day and the dashboard together
    python -m scanner renew-token --if-needed [--min-hours H]   # daily task
    python -m scanner publish [--date D] [--no-push]            # commit + push a snapshot folder
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import requests

from .auth import ManualTokenProvider, TokenError, make_token_provider
from .config import ConfigError, load_settings
from .dhan_client import DhanAuthError, DhanClient, DhanError
from .jobs.build_universe import run_build_universe
from .jobs.live import run_live
from .jobs.premarket import run_premarket
from .jobs.previous_close import PreviousCloseExists, run_previous_close
from .jobs.publish import PublishError, run_publish
from .jobs.run_day import DayRunner, archive_day
from .jobs.sectors import rebuild_sectors
from .jobs.token import ensure_token, token_info
from .live_feed import FEED_URL, FeedError, LiveFeed
from .logging_setup import close_file_logs, setup_logging
from .market_clock import MarketCalendar, at, iso_ist, now_ist
from .nse_client import NseClient
from .sectors import SectorConfigError, load_sectors
from .storage import Storage
from .system import AlreadyRunning, InstanceLock, keep_awake, preflight
from .universe import UniverseError, stocks_from_document

log = logging.getLogger("scanner")

# Commands that open the Dhan feed / run the day: never two at once.
LOCKED_COMMANDS = {"run-day", "live", "phase2"}


def _context():
    settings = load_settings()
    storage = Storage(settings.data_dir)
    calendar = MarketCalendar.from_files(*sorted((settings.root / "config").glob("nse_holidays_*.json")))
    return settings, storage, calendar


def _client(settings):
    session = requests.Session()
    tokens = make_token_provider(settings, session)
    return DhanClient.from_settings(settings, tokens, session=session), tokens


def cmd_check_auth(args) -> int:
    settings, storage, _ = _context()
    client, tokens = _client(settings)
    prof = client.ensure_data_plan()
    exp = tokens.expires_at()
    storage.record_token({**token_info(tokens, now_ist()), "last_check": iso_ist(now_ist()),
                          "last_result": "ok", "message": "check-auth"})
    print(f"OK  client={prof.get('dhanClientId')}  method={tokens.method}  "
          f"token_expires={iso_ist(exp) if exp else prof.get('tokenValidity')}  "
          f"dataPlan={prof.get('dataPlan')} (valid until {prof.get('dataValidity')})")
    return 0


def cmd_renew_token(args) -> int:
    settings, storage, _ = _context()
    _, tokens = _client(settings)
    if args.if_needed:
        hours = args.min_hours if args.min_hours is not None else float(settings.get("token.renew_min_hours", 12))
        msg = ensure_token(storage, tokens, now_ist() + timedelta(hours=hours))
        print(f"renew-token ({tokens.method}): {msg}")
        return 0
    if isinstance(tokens, ManualTokenProvider):
        tokens.renew()
    else:
        tokens.refresh()
    exp = tokens.expires_at()
    storage.record_token({**token_info(tokens, now_ist()), "last_check": iso_ist(now_ist()),
                          "last_result": "ok", "message": "renewed by renew-token"})
    print(f"Token refreshed ({tokens.method}); expires {iso_ist(exp) if exp else 'unknown'}")
    return 0


def cmd_publish(args) -> int:
    settings, storage, _ = _context()
    day = date.fromisoformat(args.date) if args.date else now_ist().date()
    msg = run_publish(settings, storage, day, push=False if args.no_push else None)
    print(f"publish {day}: {msg}")
    return 2 if ("not pushed" in msg or "no remote" in msg) else 0


def cmd_build_universe(args) -> int:
    settings, storage, calendar = _context()
    sectors = load_sectors(settings.path("paths.sectors_file"))
    doc = run_build_universe(settings, storage, sectors, calendar,
                             csv_path=Path(args.csv) if args.csv else None)
    print(f"universe: {doc['status']}  {doc['count']} stocks  "
          f"(no sector: {len(doc['unmapped_sector'])}, no NSE_EQ: {len(doc['missing_equity'])})")
    if doc["status_reason"]:
        print(f"  reason: {doc['status_reason']}")
    return 0 if doc["status"] == "ok" else 2


def cmd_previous_close(args) -> int:
    settings, storage, calendar = _context()
    uni = storage.read_dataset("universe")
    if not uni:
        print("No universe.json — run: python -m scanner build-universe", file=sys.stderr)
        return 1
    client, _ = _client(settings)
    try:
        doc = run_previous_close(settings, client, storage, calendar, stocks_from_document(uni),
                                 force=getattr(args, "force", False))
    except PreviousCloseExists as exc:
        print(f"previous_close: kept — {exc}")
        return 0
    s = doc["summary"]
    print(f"previous_close: {doc['status']}  close_date={doc['close_date']}  total={s['total']}  "
          f"gainers={s['gainers']}  losers={s['losers']}  >+{doc['thresholds']['up']}%={s['highlighted_up']}  "
          f"<{doc['thresholds']['down']}%={s['highlighted_down']}  missing={s['missing']}  "
          f"mismatch={doc['checks']['mismatch']}")
    return 0 if doc["status"] == "ok" else 2


def _feed_factory(settings):
    session = requests.Session()
    tokens = make_token_provider(settings, session)

    def factory(on_event, on_state):
        return LiveFeed(settings.secrets.dhan_client_id, tokens, on_event, on_state,
                        url=settings.get("dhan.feed_url", FEED_URL),
                        silence_timeout=float(settings.get("live.silence_reconnect_seconds", 60)))
    return factory


def _print_live(doc: dict) -> None:
    s = doc["summary"]
    print(f"live: {doc['status']}  total={s['total']}  gainers={s['gainers']}  losers={s['losers']}  "
          f">+{doc['thresholds']['up']}%={s['highlighted_up']}  <{doc['thresholds']['down']}%={s['highlighted_down']}  "
          f"missing={s['missing']}  stale={s['stale']}  ticks={doc['feed']['ticks']}  "
          f"reconnects={doc['feed']['reconnects']}  checks={doc['checks']}")
    if doc["status_reason"]:
        print(f"  reason: {doc['status_reason']}")


def cmd_live(args) -> int:
    settings, storage, calendar = _context()
    uni = storage.read_dataset("universe")
    if not uni:
        print("No universe.json — run: python -m scanner build-universe", file=sys.stderr)
        return 1
    now = now_ist()
    end = at(now.date(), settings.get("schedule.live_end", "15:30"))
    if calendar.phase(now) == "CLOSED" and not args.force:
        print("Market is closed (live runs 09:15-15:30 IST on trading days); use --force to connect anyway",
              file=sys.stderr)
        return 1
    until = now + timedelta(minutes=args.minutes) if args.minutes else end
    if not args.force:
        until = min(until, end)
    doc = run_live(settings, storage, calendar, stocks_from_document(uni), storage.read_dataset("previous_close"),
                   _feed_factory(settings), until)
    _print_live(doc)
    return 0 if doc["status"] == "ok" else 2


def _print_premarket(doc: dict) -> None:
    s = doc["summary"]
    print(f"premarket: {doc['status']}  nse_timestamp={doc['nse_timestamp']}  attempts={doc['attempts']}  "
          f"total={s['total']}  gainers={s['gainers']}  losers={s['losers']}  "
          f">+{doc['thresholds']['up']}%={s['highlighted_up']}  <{doc['thresholds']['down']}%={s['highlighted_down']}  "
          f"missing={s['missing']}  checks={doc['checks']}")
    if doc["status_reason"]:
        print(f"  reason: {doc['status_reason']}")


def cmd_premarket(args) -> int:
    settings, storage, calendar = _context()
    uni = storage.read_dataset("universe")
    if not uni:
        print("No universe.json — run: python -m scanner build-universe", file=sys.stderr)
        return 1
    doc = run_premarket(settings, NseClient.from_settings(settings), storage, calendar,
                        stocks_from_document(uni), storage.read_dataset("previous_close"))
    _print_premarket(doc)
    return 0 if doc["status"] == "ok" else 2


def _print_sectors(doc: dict) -> None:
    print(f"sectors: {doc['status']}  trade_date={doc['trade_date']}  method={doc['method']}  "
          f"min_coverage={doc['min_coverage_pct']}%")
    for ds in ("previous_close", "premarket", "live"):
        block = doc.get(ds)
        if not block:
            print(f"\n{ds}: not available for {doc['trade_date']}")
            continue
        s = block["summary"]
        print(f"\n{ds}: {block['status']}  threshold=+/-{block['thresholds']['up']}%  "
              f"advancing={s['advancing']}  declining={s['declining']}  missing={s['missing']}")
        for r in block["sectors"]:
            avg = "   n/a" if r["avg_change_percent"] is None else f"{r['avg_change_percent']:+6.2f}"
            mark = {"UP": " UP", "DOWN": " DN"}.get(r["highlight"], "")
            print(f"  {r['rank'] or '-':>2} {r['sector']:28s} {avg}%{mark:3s}  "
                  f"{r['priced']}/{r['constituents']}  +{r['advances']}/-{r['declines']}")
        if block["unmapped"]:
            print(f"     unmapped: {block['unmapped']['constituents']} stocks")


def cmd_sectors(args) -> int:
    settings, storage, calendar = _context()
    doc = rebuild_sectors(settings, storage, calendar.session_date_for(now_ist()))
    _print_sectors(doc)
    return 0 if doc["status"] == "ok" else 2


def cmd_phase1(args) -> int:
    rc = cmd_build_universe(argparse.Namespace(csv=args.csv))
    if rc not in (0, 2):
        return rc
    return max(rc, cmd_previous_close(args))


def cmd_phase2(args) -> int:
    """premarket, then the live feed until schedule.live_end."""
    rc = cmd_premarket(args)
    if rc not in (0, 2):
        return rc
    return max(rc, cmd_live(argparse.Namespace(minutes=None, force=False)))


def _prepare_server(settings, storage, calendar, host: str | None, port: int | None):
    """Build the API server; refuses a non-local bind without DASHBOARD_TOKEN."""
    from .api import check_bind, create_app, is_loopback, lan_address, make_server

    host = host or settings.get("api.host", "0.0.0.0")
    port = int(port or settings.get("api.port", 8000))
    token = settings.secrets.dashboard_token
    check_bind(host, token)
    server = make_server(create_app(settings, storage, calendar, token=token), host, port)
    urls = [f"http://127.0.0.1:{port}/"]
    if not is_loopback(host):
        lan = lan_address()
        if lan:
            urls.append(f"http://{lan}:{port}/  (other devices on your Wi-Fi)")
    print("Dashboard: " + "\n           ".join(urls))
    print("Access: " + ("DASHBOARD_TOKEN required" if token else "this PC only, no token"))
    return server


def cmd_serve(args) -> int:
    settings, storage, calendar = _context()
    server = _prepare_server(settings, storage, calendar, args.host, args.port)
    server.run()
    return 0


def cmd_run_day(args) -> int:
    settings, storage, calendar = _context()
    server = server_thread = None
    if args.serve:
        server = _prepare_server(settings, storage, calendar, args.host, args.port)
        server_thread = threading.Thread(target=server.run, name="api-server", daemon=True)
        server_thread.start()
    try:
        with keep_awake(bool(settings.get("run_day.keep_awake", True))):
            return _run_day(settings, storage, calendar)
    finally:
        if server is not None:
            server.should_exit = True
            server_thread.join(timeout=10)


def _run_day(settings, storage, calendar) -> int:

    def stocks():
        uni = storage.read_dataset("universe")
        if not uni:
            raise UniverseError("No universe.json for today")
        return stocks_from_document(uni)

    def preflight_checks():
        alerts = preflight(settings, now_ist().date())
        storage.record_job("preflight", "partial" if alerts else "ok",
                           "; ".join(alerts) or "holiday files and disk space ok", alerts)
        if alerts:
            raise RuntimeError("; ".join(alerts))
        return "ok"

    def token_check():
        _, tokens = _client(settings)
        return ensure_token(storage, tokens, at(now_ist().date(), settings.get("schedule.eod_archive", "15:35")))

    def publish():
        return run_publish(settings, storage, now_ist().date())

    def build_universe():
        doc = run_build_universe(settings, storage, load_sectors(settings.path("paths.sectors_file")), calendar)
        return f"{doc['status']} ({doc['count']} stocks)"

    def previous_close():
        client, _ = _client(settings)
        doc = run_previous_close(settings, client, storage, calendar, stocks())
        return f"{doc['status']} (missing {doc['summary']['missing']})"

    def premarket():
        doc = run_premarket(settings, NseClient.from_settings(settings), storage, calendar, stocks(),
                            storage.read_dataset("previous_close"))
        return f"{doc['status']} (missing {doc['summary']['missing']})"

    def live(until):
        doc = run_live(settings, storage, calendar, stocks(), storage.read_dataset("previous_close"),
                       _feed_factory(settings), until)
        return f"{doc['status']} ({doc['feed']['ticks']} ticks, {doc['feed']['reconnects']} reconnects)"

    def eod_archive():
        return archive_day(storage, now_ist().date())

    actions = {
        "preflight": preflight_checks, "token_check": token_check, "build_universe": build_universe,
        "previous_close": previous_close, "premarket": premarket, "live": live, "eod_archive": eod_archive}
    if settings.get("publish.enabled", True):
        actions["publish"] = publish
    runner = DayRunner(settings, storage, calendar, actions)
    try:
        results = runner.run()
    except KeyboardInterrupt:
        runner.stop.set()
        print("run-day interrupted", file=sys.stderr)
        return 1
    for name, result in results.items():
        print(f"{name:15s} {result}")
    return 2 if any(r.startswith("failed") for r in results.values()) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scanner", description="F&O Market Scanner")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--retries", type=int, default=0,
                        help="re-run the command up to N times if it fails with exit code 1 (scheduled tasks)")
    parser.add_argument("--retry-wait", type=float, default=300, help="seconds between retries (default 300)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check-auth").set_defaults(fn=cmd_check_auth)
    p = sub.add_parser("renew-token")
    p.add_argument("--if-needed", action="store_true",
                   help="only renew when the token expires within --min-hours (daily task)")
    p.add_argument("--min-hours", type=float, help="default: settings token.renew_min_hours (12)")
    p.set_defaults(fn=cmd_renew_token)
    p = sub.add_parser("publish")
    p.add_argument("--date", help="YYYY-MM-DD (default: today)")
    p.add_argument("--no-push", action="store_true", help="commit locally only")
    p.set_defaults(fn=cmd_publish)
    p = sub.add_parser("build-universe")
    p.add_argument("--csv", help="use a local detailed instrument-master CSV instead of downloading")
    p.set_defaults(fn=cmd_build_universe)
    p = sub.add_parser("previous-close")
    p.add_argument("--force", action="store_true",
                   help="rebuild even if today's ok file exists and the market has opened")
    p.set_defaults(fn=cmd_previous_close)
    p = sub.add_parser("phase1")
    p.add_argument("--csv")
    p.set_defaults(fn=cmd_phase1)
    p = sub.add_parser("live")
    p.add_argument("--minutes", type=float, help="stop after N minutes (default: at schedule.live_end)")
    p.add_argument("--force", action="store_true", help="connect even outside market hours")
    p.set_defaults(fn=cmd_live)
    sub.add_parser("premarket").set_defaults(fn=cmd_premarket)
    sub.add_parser("phase2").set_defaults(fn=cmd_phase2)
    p = sub.add_parser("run-day")
    p.add_argument("--serve", action="store_true", help="also run the API + dashboard during the day")
    p.add_argument("--host", help="API bind address (default: settings api.host)")
    p.add_argument("--port", type=int, help="API port (default: settings api.port)")
    p.set_defaults(fn=cmd_run_day)
    sub.add_parser("sectors").set_defaults(fn=cmd_sectors)
    p = sub.add_parser("serve")
    p.add_argument("--host", help="bind address (default: settings api.host; 127.0.0.1 = this PC only)")
    p.add_argument("--port", type=int, help="port (default: settings api.port)")
    p.set_defaults(fn=cmd_serve)

    args = parser.parse_args(argv)
    try:
        log_settings = load_settings()
    except ConfigError:
        log_settings = None          # the command itself reports the config error
    setup_logging(args.cmd, log_settings, args.verbose)
    attempt = 0
    while True:
        attempt += 1
        rc, retryable = _run_command(args, log_settings, attempt)
        if rc != 1 or not retryable or attempt > args.retries:
            break
        log.warning("%s failed (attempt %d of %d); retrying in %.0fs",
                    args.cmd, attempt, args.retries + 1, args.retry_wait)
        time.sleep(args.retry_wait)
    close_file_logs()
    return rc


def _run_command(args, log_settings, attempt: int) -> tuple[int, bool]:
    """Run the command once. Returns (exit code, whether a retry could help)."""
    log.info("start: %s (pid %d, attempt %d)", args.cmd, os.getpid(), attempt)
    retryable = False
    try:
        if args.cmd in LOCKED_COMMANDS and log_settings is not None:
            with InstanceLock(log_settings.data_dir / "state" / "scanner-jobs.lock", args.cmd):
                rc = args.fn(args)
        else:
            rc = args.fn(args)
    except (ConfigError, SectorConfigError, AlreadyRunning) as exc:   # retrying will not fix these
        log.error("%s", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        rc = 1
    except (TokenError, DhanAuthError, UniverseError, PublishError) as exc:
        log.error("%s", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        rc, retryable = 1, True
    except (DhanError, FeedError) as exc:
        log.error("%s", exc)
        print(f"DHAN ERROR: {exc}", file=sys.stderr)
        rc, retryable = 1, True
    except KeyboardInterrupt:
        log.warning("interrupted")
        rc = 1
    except Exception:  # noqa: BLE001 — log the traceback (scrubbed) for unattended runs
        log.exception("unhandled error in %s", args.cmd)
        rc, retryable = 1, True
    log.info("exit: %s rc=%d", args.cmd, rc)
    return rc, retryable


if __name__ == "__main__":
    sys.exit(main())
