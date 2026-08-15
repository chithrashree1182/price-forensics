"""Scraper implementations, selected by the `strategy` key in config/sites.yaml."""

from __future__ import annotations

from ..config import SiteConfig
from .base import BaseScraper, ScrapedItem
from .browser import BrowserScraper
from .jsonld import JsonLdScraper
from .jsonstate import JsonStateScraper
from .static import StaticScraper
from .wayback import WaybackScraper

__all__ = [
    "BaseScraper",
    "ScrapedItem",
    "StaticScraper",
    "BrowserScraper",
    "JsonStateScraper",
    "JsonLdScraper",
    "WaybackScraper",
    "get_scraper",
]

_REGISTRY = {
    "static": StaticScraper,
    "browser": BrowserScraper,
    "json_state": JsonStateScraper,
    "json_ld": JsonLdScraper,
    "wayback": WaybackScraper,
}


def get_scraper(site: SiteConfig, **kwargs) -> BaseScraper:
    """Instantiate the scraper matching a site's configured strategy."""
    try:
        cls = _REGISTRY[site.strategy]
    except KeyError:
        raise ValueError(
            f"unknown strategy {site.strategy!r} for site {site.key!r}; "
            f"expected one of {sorted(_REGISTRY)}"
        ) from None
    return cls(site, **kwargs)
