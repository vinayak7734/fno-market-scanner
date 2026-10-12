"""Job: commit the day's snapshot folder and push it to GitHub.

Only data/snapshots/<date>/ is ever staged or committed (`git commit -- <path>`
leaves anything else you have staged untouched). Before committing it refuses
when:
  - the branch is not publish.branch (main)
  - .env, data/state/, data/cache/ or logs/ are tracked by git at all
  - a staged file is outside data/snapshots/<date>/ or is not .json
  - a staged file contains a token-like value or a configured secret
Push: fetch publish.remote; if it has nothing we lack, push directly (your
working tree and staged files are never touched). If it has new commits, rebase
onto them only when the working tree is clean; otherwise report and leave it.
Git runs with prompts disabled (GIT_TERMINAL_PROMPT=0, GCM_INTERACTIVE=never)
and a timeout, so an unattended run can never hang waiting for a password. A
failed fetch/push is reported (the commit stays local and goes out with the
next push); a rebase conflict is aborted, never forced.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ..storage import Storage

log = logging.getLogger(__name__)
JOB = "publish"

FORBIDDEN_TRACKED = (".env", "data/state", "data/cache", "logs")
SECRET_PATTERNS = [
    re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]*"),
    re.compile(r'"access_?token"\s*:\s*"[^"]+"', re.I),
]


class PublishError(RuntimeError):
    pass


@dataclass
class GitResult:
    code: int
    out: str
    err: str


def find_git() -> str | None:
    found = shutil.which("git")
    if found:
        return found
    for candidate in (r"C:\Program Files\Git\cmd\git.exe", r"C:\Program Files (x86)\Git\cmd\git.exe"):
        if Path(candidate).exists():
            return candidate
    return None


class Git:
    def __init__(self, root: Path, timeout: float = 120, executable: str | None = None):
        self.root = root
        self.timeout = timeout
        self.executable = executable or find_git()
        if not self.executable:
            raise PublishError("git is not installed or not on PATH")
        self.env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never",
                    "GIT_ASKPASS": "", "SSH_ASKPASS": ""}

    def run(self, *args: str, check: bool = True) -> GitResult:
        try:
            p = subprocess.run([self.executable, *args], cwd=self.root, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=self.timeout, env=self.env)
        except subprocess.TimeoutExpired as exc:
            raise PublishError(f"git {args[0]} timed out after {self.timeout:.0f}s") from exc
        res = GitResult(p.returncode, p.stdout.strip(), p.stderr.strip())
        if check and res.code != 0:
            raise PublishError(f"git {' '.join(args[:2])} failed: {res.err or res.out}"[:400])
        return res


def _secret_literals(settings) -> list[str]:
    s = settings.secrets
    return [v for v in (s.dhan_access_token, s.dhan_pin, s.dhan_totp_secret, s.dashboard_token, s.github_token)
            if v and len(v) >= 6]


def scan_for_secrets(text: str, literals: list[str]) -> str | None:
    for lit in literals:
        if lit in text:
            return "a configured secret value"
    for pat in SECRET_PATTERNS:
        if pat.search(text):
            return "a token-like value"
    return None


def run_publish(settings, storage: Storage, trade_date: date, push: bool | None = None,
                git: Git | None = None) -> str:
    g = settings.get
    if not g("publish.enabled", True):
        return "publish disabled (publish.enabled: false)"
    push = bool(g("publish.push", True)) if push is None else push
    remote, branch = g("publish.remote", "origin"), g("publish.branch", "main")
    try:
        msg = _publish(settings, storage, trade_date, push, remote, branch,
                       git or Git(settings.root, timeout=float(g("publish.timeout_seconds", 120))))
    except PublishError as exc:
        storage.record_job(JOB, "failed", str(exc), [f"Publish {trade_date} failed: {exc}"])
        log.error("publish: %s", exc)
        raise
    warn = "no remote" in msg or "not pushed" in msg
    storage.record_job(JOB, "partial" if warn else "ok", msg,
                       [f"Publish {trade_date}: {msg}"] if warn else [])
    log.info("publish: %s", msg)
    return msg


def _publish(settings, storage: Storage, trade_date: date, push: bool, remote: str, branch: str, git: Git) -> str:
    rel = f"data/snapshots/{trade_date.isoformat()}"
    folder = settings.root / rel
    if git.run("rev-parse", "--is-inside-work-tree", check=False).out != "true":
        raise PublishError(f"{settings.root} is not a git repository")
    current = git.run("rev-parse", "--abbrev-ref", "HEAD").out
    if current != branch:
        raise PublishError(f"on branch '{current}', expected '{branch}'; not committing")
    tracked = git.run("ls-files", "--", *FORBIDDEN_TRACKED).out
    if tracked:
        raise PublishError("secret/runtime files are tracked by git — fix before publishing: "
                           + ", ".join(tracked.splitlines()[:10]))
    if not folder.is_dir():
        return f"nothing to publish: {rel} does not exist"

    git.run("add", "--", rel)
    staged = [p for p in git.run("diff", "--cached", "--name-only", "--", rel).out.splitlines() if p]
    if not staged:
        committed = "nothing new to commit"
    else:
        literals = _secret_literals(settings)
        for path in staged:
            problem = None
            if not path.startswith(rel + "/") or not path.endswith(".json"):
                problem = "unexpected file"
            else:
                full = settings.root / path
                if full.exists():
                    problem = scan_for_secrets(full.read_text(encoding="utf-8", errors="replace"), literals)
            if problem:
                git.run("reset", "-q", "--", rel, check=False)
                raise PublishError(f"refusing to commit {path}: {problem}")
        git.run("commit", "-q", "-m", f"Snapshots {trade_date.isoformat()}", "--", rel)
        sha = git.run("rev-parse", "--short", "HEAD").out
        committed = f"committed {len(staged)} file(s) as {sha}"

    if not push:
        return f"{committed}; not pushed (push disabled)"
    if not git.run("remote", "get-url", remote, check=False).out:
        return f"{committed}; no remote '{remote}' configured, not pushed"

    # Fast path (one PC): the remote has nothing we lack -> plain push, working tree untouched.
    heads = git.run("ls-remote", "--heads", remote, branch, check=False)
    if heads.code != 0:
        return f"{committed}; not pushed (cannot reach {remote}: {(heads.err or heads.out)[:200]})"
    if heads.out:
        fetched = git.run("fetch", "--quiet", remote, branch, check=False)
        if fetched.code != 0:
            return f"{committed}; not pushed (fetch failed: {(fetched.err or fetched.out)[:200]})"
        if git.run("merge-base", "--is-ancestor", f"{remote}/{branch}", "HEAD", check=False).code != 0:
            # The remote has commits we do not have: rebase onto them, but only with a clean tree.
            dirty = git.run("status", "--porcelain", "--untracked-files=no").out
            if dirty:
                return (f"{committed}; not pushed ({remote}/{branch} has new commits and the working tree "
                        "has local changes; pull and push manually)")
            rebased = git.run("rebase", "--quiet", f"{remote}/{branch}", check=False)
            if rebased.code != 0:
                git.run("rebase", "--abort", check=False)
                raise PublishError(f"rebase onto {remote}/{branch} conflicted and was aborted; "
                                   "resolve manually, then push")
    pushed = git.run("push", "--quiet", remote, f"HEAD:{branch}", check=False)
    if pushed.code != 0:
        return f"{committed}; not pushed (push failed: {(pushed.err or pushed.out)[:200]})"
    return f"{committed}; pushed to {remote}/{branch}"
