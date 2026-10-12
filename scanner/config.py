"""Load settings.yaml + .env into one immutable Settings object."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Secrets:
    dhan_client_id: str = ""
    dhan_access_token: str = ""
    dhan_pin: str = ""
    dhan_totp_secret: str = ""
    github_token: str = ""
    dashboard_token: str = ""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Secrets":
        env = os.environ if env is None else env
        return cls(
            dhan_client_id=env.get("DHAN_CLIENT_ID", "").strip(),
            dhan_access_token=env.get("DHAN_ACCESS_TOKEN", "").strip(),
            dhan_pin=env.get("DHAN_PIN", "").strip(),
            dhan_totp_secret=env.get("DHAN_TOTP_SECRET", "").strip().replace(" ", ""),
            github_token=env.get("GITHUB_TOKEN", "").strip(),
            dashboard_token=env.get("DASHBOARD_TOKEN", "").strip(),
        )


@dataclass(frozen=True)
class Settings:
    raw: dict[str, Any]
    root: Path
    secrets: Secrets = field(default_factory=Secrets)

    # ---- convenience accessors -------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted: str) -> Any:
        value = self.get(dotted)
        if value is None:
            raise ConfigError(f"Missing setting: {dotted}")
        return value

    def path(self, dotted: str) -> Path:
        p = Path(self.require(dotted))
        return p if p.is_absolute() else self.root / p

    @property
    def data_dir(self) -> Path:
        return self.path("paths.data_dir")

    def threshold(self, dataset: str) -> float:
        return float(self.require(f"thresholds.{dataset}"))


def load_settings(
    settings_file: Path | None = None,
    root: Path | None = None,
    env: dict[str, str] | None = None,
    load_dotenv_file: bool = True,
) -> Settings:
    root = root or PROJECT_ROOT
    settings_file = settings_file or root / "config" / "settings.yaml"
    if not settings_file.exists():
        raise ConfigError(f"Settings file not found: {settings_file}")

    if load_dotenv_file and env is None:
        try:
            from dotenv import load_dotenv

            load_dotenv(root / ".env", override=False)
        except ImportError:  # dotenv is optional at runtime
            pass

    with settings_file.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    for section in ("thresholds", "sector_thresholds"):
        for key in ("previous_close", "premarket", "live"):
            val = (raw.get(section) or {}).get(key)
            if val is None or float(val) <= 0:
                raise ConfigError(f"{section}.{key} must be a positive number")

    coverage = float((raw.get("sectors") or {}).get("min_coverage_pct", 50))
    if not 0 < coverage <= 100:
        raise ConfigError("sectors.min_coverage_pct must be between 0 and 100")

    method = raw.get("dhan", {}).get("token_method", "manual")
    if method not in ("manual", "totp"):
        raise ConfigError("dhan.token_method must be 'manual' or 'totp'")

    return Settings(raw=raw, root=root, secrets=Secrets.from_env(env))
