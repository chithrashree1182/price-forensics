"""Configuration loading.

Everything tunable lives in config/*.yaml so that the analysis parameters are
visible and version-controlled rather than buried in code. This matters for the
credibility of the study: the detection thresholds were committed *before* the
data was collected, which is checkable in git history.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

# Repo root = three levels up from this file (src/priceforensics/config.py)
ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
DATA_DIR = Path(os.environ.get("PD_DATA_DIR", ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"
EXPORT_DIR = DATA_DIR / "exports"
DB_PATH = Path(os.environ.get("PD_DB_PATH", DATA_DIR / "prices.db"))


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@dataclass(frozen=True)
class SiteConfig:
    """Scraping configuration for one retailer."""

    key: str
    display_name: str
    strategy: str
    base_url: str
    listing: dict[str, str] = field(default_factory=dict)
    detail: dict[str, str] = field(default_factory=dict)
    categories: dict[str, str] = field(default_factory=dict)
    request_delay_seconds: float = 4.0
    jitter_seconds: float = 2.0
    timeout_seconds: int = 30
    max_retries: int = 3
    respect_robots_txt: bool = True
    user_agent: str = "Mozilla/5.0"
    extra: dict[str, Any] = field(default_factory=dict)

    def category_url(self, category_id: str) -> str:
        path = self.categories[category_id]
        return f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"


@lru_cache(maxsize=1)
def load_sites() -> dict[str, SiteConfig]:
    raw = _read_yaml(CONFIG_DIR / "sites.yaml")
    defaults = raw.get("defaults", {})
    out: dict[str, SiteConfig] = {}

    for key, spec in (raw.get("sites") or {}).items():
        merged = {**defaults, **spec}
        known = {
            "display_name",
            "strategy",
            "base_url",
            "listing",
            "detail",
            "categories",
            "request_delay_seconds",
            "jitter_seconds",
            "timeout_seconds",
            "max_retries",
            "respect_robots_txt",
            "user_agent",
        }
        out[key] = SiteConfig(
            key=key,
            display_name=merged.get("display_name", key.title()),
            strategy=merged.get("strategy", "static"),
            base_url=merged.get("base_url", ""),
            listing=merged.get("listing", {}) or {},
            detail=merged.get("detail", {}) or {},
            categories=merged.get("categories", {}) or {},
            request_delay_seconds=float(merged.get("request_delay_seconds", 4.0)),
            jitter_seconds=float(merged.get("jitter_seconds", 2.0)),
            timeout_seconds=int(merged.get("timeout_seconds", 30)),
            max_retries=int(merged.get("max_retries", 3)),
            respect_robots_txt=bool(merged.get("respect_robots_txt", True)),
            user_agent=merged.get("user_agent", "Mozilla/5.0"),
            extra={k: v for k, v in merged.items() if k not in known},
        )
    return out


@lru_cache(maxsize=1)
def load_targets() -> dict[str, Any]:
    return _read_yaml(CONFIG_DIR / "targets.yaml")


def detection_config() -> dict[str, Any]:
    return load_targets().get("detection", {})


def festival_windows() -> list[dict[str, str]]:
    return load_targets().get("study", {}).get("festival_windows", [])


def ensure_dirs() -> None:
    for d in (DATA_DIR, RAW_DIR, EXPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)
