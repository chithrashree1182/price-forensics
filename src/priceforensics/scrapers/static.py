"""Static HTML scraper — requests + BeautifulSoup.

Works for retailers that render prices server-side (Croma, Reliance Digital,
most mid-size Indian e-commerce). Cheap and fast: no browser process, so a full
category sweep costs a handful of requests.
"""

from __future__ import annotations

import logging
from typing import Any

from ..normalize import parse_int, parse_price
from .base import BaseScraper, ScrapedItem

log = logging.getLogger(__name__)


class StaticScraper(BaseScraper):
    """Scrapes listing and detail pages from raw HTML."""

    def scrape_category(self, category_id: str, pages: int = 1) -> list[ScrapedItem]:
        items: list[ScrapedItem] = []
        base = self.site.category_url(category_id)

        for page in range(1, pages + 1):
            url = base if page == 1 else self._paginate(base, page)
            html = self.fetch(url)
            if not html:
                log.warning("no HTML for %s page %s", category_id, page)
                continue

            self.archive_html(html, f"listing_{category_id}_p{page}")
            page_items = self.parse_listing(html, category_id)
            log.info("%s %s page %s -> %s items",
                     self.site.key, category_id, page, len(page_items))
            if not page_items:
                # Empty page means either pagination ran out or the selectors
                # broke. Either way there is no point requesting more.
                break
            items.extend(page_items)
            self.stats["parsed"] += len(page_items)

        return items

    @staticmethod
    def _paginate(url: str, page: int) -> str:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}page={page}"

    def parse_listing(self, html: str, category_id: str) -> list[ScrapedItem]:
        sel = self.site.listing
        doc = self.soup(html)
        item_selector = sel.get("item")
        if not item_selector:
            log.error("site %s has no listing.item selector", self.site.key)
            return []

        out: list[ScrapedItem] = []
        for node in doc.select(item_selector):
            title = self.select_text(node, sel.get("title"))
            href = self.select_text(node, sel.get("url"))
            if not title or not href:
                continue

            item = ScrapedItem(
                site_key=self.site.key,
                url=self.absolute_url(href) or href,
                raw_title=title,
                selling_price=parse_price(self.select_text(node, sel.get("selling_price"))),
                mrp=parse_price(self.select_text(node, sel.get("mrp"))),
                category=category_id,
                rating_count=parse_int(self.select_text(node, sel.get("rating_count"))),
            )

            # Trust the site's own discount label only as a cross-check; the
            # computed value from mrp/selling_price is what the analysis uses.
            label = self.select_text(node, sel.get("discount_label"))
            if label:
                pct = parse_int(label)
                item.discount_pct = float(pct) if pct is not None else None

            if item.is_usable():
                out.append(item)

        return out

    def health_check(self) -> dict[str, Any]:
        """Report which selectors currently return data. Used by `pf doctor`.

        E-commerce markup rots. This turns "the scraper silently returned zero
        rows for three weeks" into a same-day alert.
        """
        report: dict[str, Any] = {"site": self.site.key, "categories": {}}
        for category_id in self.site.categories:
            url = self.site.category_url(category_id)
            html = self.fetch(url)
            if not html:
                report["categories"][category_id] = {"ok": False, "reason": "fetch failed"}
                continue

            doc = self.soup(html)
            nodes = doc.select(self.site.listing.get("item", "")) if self.site.listing.get("item") else []
            field_hits = {}
            if nodes:
                probe = nodes[0]
                for field in ("title", "url", "selling_price", "mrp"):
                    field_hits[field] = self.select_text(probe, self.site.listing.get(field)) is not None
            report["categories"][category_id] = {
                "ok": bool(nodes) and all(field_hits.get(f) for f in ("title", "url", "selling_price")),
                "items_found": len(nodes),
                "fields": field_hits,
            }
        return report
