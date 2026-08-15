"""Scraper built on schema.org JSON-LD.

Why this ended up being the right approach
------------------------------------------
Three strategies were tried against live sites before this one, and the failures
are worth recording because they shaped the design:

  1. **CSS selectors on listing pages.** Assumed prices were in the rendered
     DOM. On Reliance Digital they are not - the grid is hydrated client-side,
     so a selector finds nothing.

  2. **`window.__INITIAL_STATE__` parsing.** The blob exists and contains
     products, but `pf doctor` showed every category returning the same twelve
     iPhones - even `/collection/air-conditioners`. That path was static SEO
     boilerplate, identical on every page. The health check was validating a
     constant and reporting success, which is the most dangerous kind of bug:
     it would have produced a confident, entirely fabricated dataset.

  3. **JSON-LD.** Category pages carry a schema.org `ItemList` with the real
     products (name + URL, no prices); product pages carry a `Product` with an
     `offers.price`. Two hops, but both are stable published contracts rather
     than implementation details.

JSON-LD is the most durable surface a retailer exposes. It exists because Google
requires it for rich results, which means breaking it costs the retailer search
traffic - a far stronger stability guarantee than any CSS class name.

The lesson from failure 2 is baked into `health_check` below: it now asserts
that different categories return *different* products, because "returns data"
and "returns the right data" are not the same claim.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from typing import Any, Iterator

from ..normalize import parse_price
from .base import ScrapedItem
from .static import StaticScraper

log = logging.getLogger(__name__)


def iter_jsonld(doc) -> Iterator[dict[str, Any]]:
    """Yield every JSON-LD object on the page, flattening lists and @graph."""
    for tag in doc.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Some sites emit trailing commas or embedded newlines in strings.
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw))
            except json.JSONDecodeError:
                continue
        for block in (data if isinstance(data, list) else [data]):
            if not isinstance(block, dict):
                continue
            if "@graph" in block and isinstance(block["@graph"], list):
                for sub in block["@graph"]:
                    if isinstance(sub, dict):
                        yield sub
            else:
                yield block


def _clean(text: str | None) -> str:
    """JSON-LD from server-side templating is often double HTML-escaped."""
    if not text:
        return ""
    out = html_lib.unescape(html_lib.unescape(str(text))).strip()
    return re.sub(r"\s+", " ", out)


class JsonLdScraper(StaticScraper):
    """Reads catalogue structure from schema.org markup.

    Config (config/sites.yaml):

        json_ld:
          listing_type: ItemList     # category pages
          detail_type:  Product      # product pages
          mrp_paths:                 # optional: where a struck-through price hides
            - offers.highPrice
    """

    # -- listing pages -----------------------------------------------------

    def parse_listing(self, html: str, category_id: str) -> list[ScrapedItem]:
        """Extract product names and URLs from an ItemList.

        Listing pages carry no prices, so these items are placeholders: the
        panel scrape fills in prices from each product's own page. That is why
        `is_usable()` is not applied here.
        """
        doc = self.soup(html)
        out: list[ScrapedItem] = []
        seen: set[str] = set()

        for block in iter_jsonld(doc):
            if block.get("@type") != "ItemList":
                continue
            for element in block.get("itemListElement", []) or []:
                if not isinstance(element, dict):
                    continue
                name = _clean(element.get("name"))
                url = _clean(element.get("url"))
                if not name or not url:
                    continue
                if not url.startswith("http"):
                    url = "https://" + url.lstrip("/")
                if url in seen:
                    continue
                seen.add(url)
                out.append(ScrapedItem(
                    site_key=self.site.key,
                    url=url,
                    raw_title=name,
                    category=category_id,
                ))

        log.info("%s/%s -> %s products discovered", self.site.key, category_id, len(out))
        return out

    # -- detail pages ------------------------------------------------------

    def parse_detail(self, html: str, url: str) -> ScrapedItem | None:
        """Extract price from a Product block."""
        doc = self.soup(html)
        cfg = self.site.extra.get("json_ld") or {}

        for block in iter_jsonld(doc):
            if str(block.get("@type", "")).lower() != "product":
                continue

            name = _clean(block.get("name"))
            if not name:
                continue

            offers = block.get("offers") or {}
            if isinstance(offers, list):
                offers = next((o for o in offers if isinstance(o, dict)), {})
            if not isinstance(offers, dict):
                offers = {}

            selling = parse_price(str(offers.get("price", "")))
            if selling is None:
                continue

            item = ScrapedItem(
                site_key=self.site.key,
                url=url,
                raw_title=name,
                selling_price=selling,
            )

            # MRP is not part of the core Offer schema. Retailers put it in
            # different places, so the candidate paths are configurable.
            for path in (cfg.get("mrp_paths") or ["offers.highPrice", "offers.listPrice"]):
                node: Any = block
                for part in path.split("."):
                    node = node.get(part) if isinstance(node, dict) else None
                    if node is None:
                        break
                if isinstance(node, dict):
                    node = node.get("price") or node.get("value")
                mrp = parse_price(str(node)) if node is not None else None
                if mrp:
                    item.mrp = mrp
                    break

            # Fall back to the retailer's own marked/effective price pair, which
            # several Indian sites expose alongside the schema block.
            if item.mrp is None:
                m = re.search(r'"marked"\s*:\s*\{[^}]*?"min"\s*:\s*([\d.]+)', html)
                if m:
                    item.mrp = parse_price(m.group(1))

            if item.mrp and item.selling_price and item.mrp < item.selling_price:
                item.mrp = None      # data error, not a discount

            availability = str(offers.get("availability", "")).lower()
            if availability:
                item.in_stock = 0 if "outofstock" in availability.replace("_", "") else 1

            brand = block.get("brand")
            if isinstance(brand, dict):
                brand = brand.get("name")

            item.dark_patterns = self.extract_dark_patterns(doc)
            return item

        return None

    # -- health ------------------------------------------------------------

    def health_check(self) -> dict[str, Any]:
        """Verify the JSON-LD contract still holds.

        Critically, this checks that different categories return *different*
        products. An earlier strategy passed every other check while returning
        identical data for every category, including air conditioners. Data
        presence is not data correctness.
        """
        report: dict[str, Any] = {"site": self.site.key, "strategy": "json_ld",
                                  "categories": {}}
        fingerprints: dict[str, frozenset[str]] = {}

        for category_id in self.site.categories:
            html = self.fetch(self.site.category_url(category_id))
            if not html:
                report["categories"][category_id] = {"ok": False, "reason": "fetch failed"}
                continue

            items = self.parse_listing(html, category_id)
            fingerprints[category_id] = frozenset(i.url for i in items)
            report["categories"][category_id] = {
                "ok": len(items) > 0,
                "products_found": len(items),
                "sample": items[0].raw_title[:64] if items else None,
            }

        # cross-category distinctness
        distinct = True
        collisions: list[str] = []
        keys = list(fingerprints)
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                a, b = keys[i], keys[j]
                if fingerprints[a] and fingerprints[a] == fingerprints[b]:
                    distinct = False
                    collisions.append(f"{a} == {b}")
        report["categories_distinct"] = distinct
        if collisions:
            report["collisions"] = collisions
            for cat in report["categories"].values():
                if isinstance(cat, dict):
                    cat["ok"] = False
            report["reason"] = (
                "categories returned identical product sets - the configured "
                "path is probably static boilerplate, not the real listing"
            )

        # verify one detail page actually yields a price
        first = next((c for c in report["categories"].values()
                      if isinstance(c, dict) and c.get("products_found")), None)
        if first:
            for category_id in self.site.categories:
                html = self.fetch(self.site.category_url(category_id))
                items = self.parse_listing(html, category_id) if html else []
                if items:
                    detail_html = self.fetch(items[0].url)
                    parsed = self.parse_detail(detail_html, items[0].url) if detail_html else None
                    report["detail_probe"] = {
                        "url": items[0].url[:90],
                        "ok": bool(parsed and parsed.selling_price),
                        "price": parsed.selling_price if parsed else None,
                        "mrp": parsed.mrp if parsed else None,
                        "title": parsed.raw_title[:56] if parsed else None,
                    }
                    break

        return report
