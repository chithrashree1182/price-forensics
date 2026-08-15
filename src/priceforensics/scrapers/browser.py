"""Playwright scraper for JavaScript-rendered sites.

Flipkart (and Amazon) build the price into the DOM client-side, so `requests`
returns markup with no price in it. This driver runs a real headless browser,
waits for the price node to appear, then hands the rendered HTML to the same
BeautifulSoup parsing path used by the static scraper — so parsing logic is not
duplicated between the two.

Install once:
    pip install playwright && playwright install chromium
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

from .base import ScrapedItem
from .static import StaticScraper

log = logging.getLogger(__name__)


class BrowserScraper(StaticScraper):
    """Renders pages in headless Chromium, then reuses StaticScraper parsing."""

    def __init__(self, *args, headless: bool = True, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.headless = headless
        self._playwright = None
        self._browser = None
        self._context = None

    # -- lifecycle ---------------------------------------------------------

    def _ensure_browser(self) -> bool:
        if self._context is not None:
            return True
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.error(
                "playwright not installed. Run:\n"
                "    pip install playwright && playwright install chromium"
            )
            return False

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._context = self._browser.new_context(
            user_agent=self.site.user_agent,
            viewport={"width": 1440, "height": 900},
            locale="en-IN",
        )
        # Images and fonts are pure bandwidth for our purposes — we only ever
        # read text. Blocking them cuts page weight by roughly 80%, which is
        # both faster for us and lighter on the retailer.
        self._context.route(
            "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,mp4}",
            lambda route: route.abort(),
        )
        return True

    def close(self) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer:
                    closer.close()
            except Exception:
                pass
        try:
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
        self._context = self._browser = self._playwright = None
        super().close()

    # -- fetching ----------------------------------------------------------

    def fetch(self, url: str) -> str | None:
        """Render `url` and return the resulting HTML."""
        if not self.allowed(url):
            return None
        self.stats["requested"] += 1
        if self.dry_run:
            log.info("[dry-run] would render %s", url)
            return None
        if not self._ensure_browser():
            self.stats["failed"] += 1
            return None

        wait_for = self.site.listing.get("selling_price") or self.site.detail.get("selling_price")
        first_selector = wait_for.split(",")[0].strip() if wait_for else None

        for attempt in range(1, self.site.max_retries + 1):
            self._wait()
            page = None
            try:
                page = self._context.new_page()
                page.goto(url, timeout=self.site.timeout_seconds * 1000,
                          wait_until="domcontentloaded")
                if first_selector:
                    try:
                        page.wait_for_selector(first_selector, timeout=12000)
                    except Exception:
                        # Price node never appeared. Could be an empty category,
                        # a bot wall, or a markup change — archive it anyway so
                        # `pf doctor` and a human can tell which.
                        log.warning("price selector %r not found on %s",
                                    first_selector, url)
                # Small scroll: several Indian retailers lazy-render the grid.
                page.mouse.wheel(0, random.randint(1200, 2200))
                time.sleep(random.uniform(0.6, 1.4))
                html = page.content()
                self.stats["fetched"] += 1
                return html
            except Exception as exc:
                log.warning("render attempt %s/%s failed for %s: %s",
                            attempt, self.site.max_retries, url, exc)
                time.sleep(2 ** attempt)
            finally:
                if page:
                    try:
                        page.close()
                    except Exception:
                        pass

        self.stats["failed"] += 1
        return None

    def health_check(self) -> dict[str, Any]:
        report = super().health_check()
        report["strategy"] = "browser"
        return report
