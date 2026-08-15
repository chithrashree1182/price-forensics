"""Historical price backfill from the Internet Archive.

The problem this solves: a longitudinal price study normally cannot say anything
until it has run for months. But archive.org has been snapshotting product pages
for years, and those snapshots contain the prices that were live at the time.

So we can reconstruct partial price history on day one.

Honest limitations, which belong in the write-up rather than being hidden:

  * Coverage is irregular. Popular products may have weekly snapshots; most have
    a handful per year, and some none at all.
  * Snapshot timing is not random — pages get archived more when they are being
    linked to and shared, which correlates with sale events. Backfilled data is
    therefore used for *illustrating* individual product histories, and excluded
    from the headline rate statistics, which use live panel data only.
  * Old snapshots use old markup, so today's selectors often miss. We fall back
    to JSON-LD structured data, which is far more stable over time.

Every backfilled observation is stored with source='wayback' so it can be
filtered out of any analysis where it does not belong.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime
from typing import Any, Iterator

import requests

from ..normalize import parse_price
from .base import BaseScraper, ScrapedItem

log = logging.getLogger(__name__)

CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
SNAPSHOT_URL = "https://web.archive.org/web/{timestamp}id_/{url}"


class WaybackScraper(BaseScraper):
    """Pulls archived snapshots of a product URL and extracts historical prices."""

    def scrape_category(self, category_id: str, pages: int = 1) -> list[ScrapedItem]:
        raise NotImplementedError(
            "Wayback backfill works on specific product URLs, not category pages. "
            "Use backfill_url()."
        )

    # -- snapshot discovery ------------------------------------------------

    def list_snapshots(
        self,
        url: str,
        from_date: date | None = None,
        to_date: date | None = None,
        limit: int = 400,
    ) -> list[dict[str, str]]:
        """Query the CDX API for available snapshots of `url`."""
        params: dict[str, Any] = {
            "url": url,
            "output": "json",
            "fl": "timestamp,original,statuscode,digest",
            "filter": "statuscode:200",
            # One snapshot per day is plenty; the behaviour we study is daily.
            "collapse": "timestamp:8",
            "limit": limit,
        }
        if from_date:
            params["from"] = from_date.strftime("%Y%m%d")
        if to_date:
            params["to"] = to_date.strftime("%Y%m%d")

        try:
            resp = self.session.get(CDX_ENDPOINT, params=params, timeout=60)
            resp.raise_for_status()
            rows = resp.json()
        except (requests.RequestException, json.JSONDecodeError) as exc:
            log.warning("CDX query failed for %s: %s", url, exc)
            return []

        if not rows or len(rows) < 2:
            return []

        header, *data = rows
        return [dict(zip(header, row)) for row in data]

    # -- extraction --------------------------------------------------------

    def backfill_url(
        self,
        url: str,
        from_date: date | None = None,
        to_date: date | None = None,
        max_snapshots: int = 60,
    ) -> Iterator[tuple[date, ScrapedItem]]:
        """Yield (observation_date, item) for each usable archived snapshot."""
        snapshots = self.list_snapshots(url, from_date, to_date)
        if not snapshots:
            log.info("no archived snapshots for %s", url)
            return

        log.info("%s archived snapshots for %s", len(snapshots), url)

        for snap in snapshots[:max_snapshots]:
            ts = snap.get("timestamp", "")
            if len(ts) < 8:
                continue
            try:
                snap_date = datetime.strptime(ts[:8], "%Y%m%d").date()
            except ValueError:
                continue

            archive_url = SNAPSHOT_URL.format(timestamp=ts, url=url)
            # archive.org is a free public good being used at our convenience;
            # the rate limit here is deliberately gentler than for retailers.
            time.sleep(self.site.request_delay_seconds)

            try:
                resp = self.session.get(archive_url, timeout=60)
                if resp.status_code != 200:
                    continue
                html = resp.text
            except requests.RequestException as exc:
                log.debug("snapshot fetch failed (%s): %s", ts, exc)
                continue

            item = self._extract(html, url)
            if item and item.selling_price:
                item.source = "wayback"
                yield snap_date, item

    def _extract(self, html: str, url: str) -> ScrapedItem | None:
        """Extract price from an archived page.

        Tries JSON-LD first: schema.org Product/Offer markup has been stable for
        a decade, whereas CSS class names on these sites change every few weeks.
        Falls back to configured selectors, then to a regex over rupee amounts.
        """
        doc = self.soup(html)

        title, price, mrp = self._from_jsonld(doc)

        if price is None:
            sel = self.site.detail
            title = title or self.select_text(doc, sel.get("title"))
            price = parse_price(self.select_text(doc, sel.get("selling_price")))
            mrp = mrp or parse_price(self.select_text(doc, sel.get("mrp")))

        if price is None:
            title = title or (doc.title.get_text(strip=True) if doc.title else None)
            price = self._regex_price(html)

        if not title or price is None:
            return None

        return ScrapedItem(
            site_key=self.site.key,
            url=url,
            raw_title=title,
            selling_price=price,
            mrp=mrp,
        )

    @staticmethod
    def _from_jsonld(doc) -> tuple[str | None, float | None, float | None]:
        """Read schema.org Product markup if present."""
        for tag in doc.find_all("script", type="application/ld+json"):
            try:
                payload = json.loads(tag.string or "{}")
            except (json.JSONDecodeError, TypeError):
                continue

            candidates = payload if isinstance(payload, list) else [payload]
            for block in candidates:
                if not isinstance(block, dict):
                    continue
                if block.get("@type") not in ("Product", "product"):
                    continue

                name = block.get("name")
                offers = block.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                if not isinstance(offers, dict):
                    continue

                price = parse_price(str(offers.get("price"))) if offers.get("price") else None
                mrp = None
                for key in ("highPrice", "listPrice", "priceSpecification"):
                    raw = offers.get(key)
                    if isinstance(raw, dict):
                        raw = raw.get("price")
                    if raw:
                        mrp = parse_price(str(raw))
                        break
                if price:
                    return name, price, mrp
        return None, None, None

    @staticmethod
    def _regex_price(html: str) -> float | None:
        """Last resort: the most frequently repeated rupee amount on the page.

        On a product page the selling price is echoed in several places (buy box,
        breadcrumb, sticky bar), so the modal value is usually correct. Crude,
        but it recovers data from very old snapshots that nothing else parses.
        """
        matches = re.findall(r"₹\s?([\d,]{3,12})", html)
        if not matches:
            return None
        counts: dict[float, int] = {}
        for raw in matches:
            value = parse_price(raw)
            if value and value >= 100:
                counts[value] = counts.get(value, 0) + 1
        if not counts:
            return None
        return max(counts.items(), key=lambda kv: (kv[1], -kv[0]))[0]
