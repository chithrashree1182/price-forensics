"""Scraper foundations: politeness, robots.txt, retries, HTML archiving.

Design notes
------------
*Archive first, parse later.* Every fetched page is written to disk gzipped
before anything is extracted from it. Selector bugs are discovered weeks later,
and without the raw HTML the affected days are simply lost — you cannot re-scrape
the past. Storage is cheap; a rescrape is impossible.

*Config-driven selectors.* Nothing in this module knows what Flipkart's markup
looks like. Selectors come from config/sites.yaml, so a site redesign is a YAML
edit rather than a code change.
"""

from __future__ import annotations

import gzip
import logging
import random
import time
import urllib.robotparser
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ..config import RAW_DIR, SiteConfig

log = logging.getLogger(__name__)


@dataclass
class ScrapedItem:
    """One product observation, before it reaches the warehouse."""

    site_key: str
    url: str
    raw_title: str
    selling_price: float | None = None
    mrp: float | None = None
    discount_pct: float | None = None
    category: str = ""
    site_product_id: str | None = None
    in_stock: int | None = None
    rating: float | None = None
    rating_count: int | None = None
    seller_name: str | None = None
    dark_patterns: list[tuple[str, str, float | None]] = field(default_factory=list)
    raw_snapshot: str | None = None
    source: str = "live"

    def is_usable(self) -> bool:
        return bool(self.raw_title) and self.selling_price is not None


class RobotsCache:
    """Per-host robots.txt, fetched once per run."""

    def __init__(self, user_agent: str) -> None:
        self.user_agent = user_agent
        self._parsers: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def can_fetch(self, url: str) -> bool:
        host = urlparse(url).netloc
        if host not in self._parsers:
            self._parsers[host] = self._load(url)
        parser = self._parsers[host]
        if parser is None:
            # If robots.txt is unreachable we proceed, but stay at the same
            # conservative rate limit. A missing file is not a disallow.
            return True
        return parser.can_fetch(self.user_agent, url)

    def _load(self, url: str):
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        try:
            parser.read()
            log.info("loaded robots.txt for %s", parsed.netloc)
            return parser
        except Exception as exc:  # network error, 404, malformed
            log.warning("could not read %s (%s)", robots_url, exc)
            return None


class BaseScraper(ABC):
    """Shared fetch/archive/extract behaviour."""

    def __init__(self, site: SiteConfig, run_date: date | None = None,
                 archive: bool = True, dry_run: bool = False) -> None:
        self.site = site
        self.run_date = run_date or date.today()
        self.archive = archive
        self.dry_run = dry_run
        self.robots = RobotsCache(site.user_agent) if site.respect_robots_txt else None
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": site.user_agent,
            "Accept-Language": "en-IN,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        self._last_request_at = 0.0
        self.stats: dict[str, int] = {
            "requested": 0, "fetched": 0, "blocked": 0, "failed": 0, "parsed": 0,
        }

    # -- politeness --------------------------------------------------------

    def _wait(self) -> None:
        """Sleep so requests stay at the configured rate, with jitter."""
        delay = self.site.request_delay_seconds + random.uniform(0, self.site.jitter_seconds)
        elapsed = time.time() - self._last_request_at
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_request_at = time.time()

    def allowed(self, url: str) -> bool:
        if self.robots is None:
            return True
        ok = self.robots.can_fetch(url)
        if not ok:
            self.stats["blocked"] += 1
            log.warning("robots.txt disallows %s — skipping", url)
        return ok

    # -- fetching ----------------------------------------------------------

    def fetch(self, url: str) -> str | None:
        """Fetch a URL with retries and exponential backoff. Returns HTML."""
        if not self.allowed(url):
            return None
        self.stats["requested"] += 1
        if self.dry_run:
            log.info("[dry-run] would fetch %s", url)
            return None

        for attempt in range(1, self.site.max_retries + 1):
            self._wait()
            try:
                resp = self.session.get(url, timeout=self.site.timeout_seconds)
                if resp.status_code == 200:
                    self.stats["fetched"] += 1
                    return resp.text
                if resp.status_code in (429, 503):
                    backoff = min(60, 2 ** attempt * 5)
                    log.warning("%s returned %s — backing off %ss",
                                url, resp.status_code, backoff)
                    time.sleep(backoff)
                    continue
                log.warning("%s returned %s", url, resp.status_code)
                break
            except requests.RequestException as exc:
                log.warning("attempt %s/%s failed for %s: %s",
                            attempt, self.site.max_retries, url, exc)
                time.sleep(2 ** attempt)

        self.stats["failed"] += 1
        return None

    # -- archiving ---------------------------------------------------------

    def archive_html(self, html: str, label: str) -> str | None:
        """Write a gzipped snapshot; return the repo-relative path."""
        if not self.archive or not html:
            return None
        day_dir = RAW_DIR / self.run_date.isoformat() / self.site.key
        day_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:120]
        path = day_dir / f"{safe}.html.gz"
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write(html)
        return str(path.relative_to(RAW_DIR.parent))

    # -- selector helpers --------------------------------------------------

    @staticmethod
    def select_text(node, selector: str | None) -> str | None:
        """Return text for the first matching selector.

        Selectors may be comma-separated alternatives — retailers A/B-test their
        markup, so several class names are often live at once. The first that
        matches wins.
        """
        if not selector or node is None:
            return None
        for candidate in [s.strip() for s in selector.split(",") if s.strip()]:
            attr = None
            if "@" in candidate:
                candidate, attr = candidate.rsplit("@", 1)
            try:
                found = node.select_one(candidate)
            except Exception:
                continue
            if found is None:
                continue
            value = found.get(attr) if attr else found.get_text(" ", strip=True)
            if value:
                return value.strip() if isinstance(value, str) else value
        return None

    def absolute_url(self, href: str | None) -> str | None:
        if not href:
            return None
        return urljoin(self.site.base_url, href)

    @staticmethod
    def soup(html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "lxml")

    # -- interface ---------------------------------------------------------

    @abstractmethod
    def scrape_category(self, category_id: str, pages: int = 1) -> list[ScrapedItem]:
        """Scrape listing pages for a category."""

    def scrape_listing_urls(self, urls: Iterable[str]) -> list[ScrapedItem]:
        """Scrape individual product pages (the daily panel)."""
        items: list[ScrapedItem] = []
        for url in urls:
            html = self.fetch(url)
            if not html:
                continue
            snapshot = self.archive_html(html, urlparse(url).path)
            item = self.parse_detail(html, url)
            if item:
                item.raw_snapshot = snapshot
                items.append(item)
                self.stats["parsed"] += 1
        return items

    def parse_detail(self, html: str, url: str) -> ScrapedItem | None:
        """Parse a product detail page using the site's `detail` selectors."""
        from ..normalize import parse_int, parse_price  # local: avoid cycle

        sel = self.site.detail
        doc = self.soup(html)
        title = self.select_text(doc, sel.get("title"))
        if not title:
            return None

        item = ScrapedItem(
            site_key=self.site.key,
            url=url,
            raw_title=title,
            selling_price=parse_price(self.select_text(doc, sel.get("selling_price"))),
            mrp=parse_price(self.select_text(doc, sel.get("mrp"))),
        )
        item.dark_patterns = self.extract_dark_patterns(doc)
        stock_text = self.select_text(doc, sel.get("stock_claim"))
        if stock_text:
            item.in_stock = 0 if "out of stock" in stock_text.lower() else 1
        rating_count = self.select_text(doc, sel.get("rating_count"))
        item.rating_count = parse_int(rating_count)
        return item

    def extract_dark_patterns(self, doc) -> list[tuple[str, str, float | None]]:
        """Find urgency/scarcity messaging on the page."""
        import re

        from ..config import detection_config

        cfg = detection_config().get("dark_patterns", {})
        found: list[tuple[str, str, float | None]] = []
        text = doc.get_text(" ", strip=True).lower()

        for kind, key in (("stock_claim", "stock_claim_patterns"),
                          ("viewer_claim", "viewer_claim_patterns")):
            for pattern in cfg.get(key, []) or []:
                for match in re.finditer(pattern, text, re.IGNORECASE):
                    value = None
                    if match.groups():
                        try:
                            value = float(match.group(1))
                        except (ValueError, TypeError, IndexError):
                            value = None
                    snippet = text[max(0, match.start() - 30): match.end() + 30]
                    found.append((kind, snippet.strip(), value))
                    break  # one hit per pattern is enough
        return found

    def close(self) -> None:
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()
