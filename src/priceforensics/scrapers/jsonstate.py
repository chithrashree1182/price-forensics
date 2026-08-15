"""Scraper for sites that embed their catalogue as JSON in the page.

Why this exists
---------------
The original design assumed prices live in the rendered DOM, so selectors were
CSS. Running `pf doctor` against live sites showed that assumption was wrong in
two different ways:

  * Flipkart and Croma sit behind enterprise bot protection and refuse to serve
    a scraper at all. They were dropped rather than worked around (see
    docs/ethics.md) - a block is a legitimate answer.

  * Reliance Digital serves everything, but the prices are not in the HTML. They
    are in a `window.__INITIAL_STATE__` JSON blob that the front end hydrates
    from. A CSS selector finds nothing; the data was there all along.

Parsing that blob turns out to be *better* than scraping the DOM, not a
compromise:

  * Class names are generated and rotate on every front-end deploy. The JSON
    field names are the retailer's own API contract and move far more slowly.
  * The JSON carries fields the DOM never renders - EAN, model code, brand,
    stock flags - which makes cross-seller product matching much more reliable.
  * One request yields the full catalogue slice, already structured.

The path into the blob is configured per site in config/sites.yaml, so a
restructure is still a YAML edit rather than a code change.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..normalize import parse_price
from .base import ScrapedItem
from .static import StaticScraper

log = logging.getLogger(__name__)


def extract_state_blob(html: str, marker: str) -> dict[str, Any] | None:
    """Pull a JSON object assigned to `marker` out of a page.

    Cannot use a regex for the object itself: the blob is ~750KB with nested
    braces and brace-containing strings. This scans with a depth counter that
    is string- and escape-aware, which is the only reliable way short of a full
    JS parser.
    """
    m = re.search(re.escape(marker) + r"\s*=\s*", html)
    if not m:
        return None

    start = m.end()
    if start >= len(html) or html[start] != "{":
        return None

    depth, i = 0, start
    in_string, escaped = False, False
    while i < len(html):
        c = html[i]
        if in_string:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_string = False
        else:
            if c == '"':
                in_string = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    break
        i += 1

    if depth != 0:
        log.warning("unbalanced braces while extracting %s", marker)
        return None

    try:
        return json.loads(html[start:i + 1])
    except json.JSONDecodeError as exc:
        log.warning("state blob for %s did not parse: %s", marker, exc)
        return None


def dig(obj: Any, path: str) -> Any:
    """Follow a dotted/bracketed path: 'a.b[0].c'. Returns None if absent."""
    if not path:
        return obj
    cur = obj
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        if cur is None:
            return None
        if part.startswith("["):
            idx = int(part[1:-1])
            if not isinstance(cur, list) or idx >= len(cur):
                return None
            cur = cur[idx]
        else:
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
    return cur


class JsonStateScraper(StaticScraper):
    """Reads the catalogue from an embedded JSON state object.

    Config (config/sites.yaml, under the site's `json_state` key):

        json_state:
          marker:      window.__INITIAL_STATE__
          items_path:  appcustom.seoSchema[0].target_json.collection_listing.items
          fields:
            title:          name
            selling_price:  price.effective.min
            mrp:            price.marked.min
            url_slug:       slug
          url_template: /product/{slug}
    """

    def parse_listing(self, html: str, category_id: str) -> list[ScrapedItem]:
        cfg = self.site.extra.get("json_state") or {}
        marker = cfg.get("marker", "window.__INITIAL_STATE__")
        items_path = cfg.get("items_path", "")
        fields = cfg.get("fields", {}) or {}
        url_template = cfg.get("url_template", "/product/{slug}")

        state = extract_state_blob(html, marker)
        if state is None:
            log.error("no %s found on %s/%s - site may have restructured",
                      marker, self.site.key, category_id)
            return []

        raw_items = dig(state, items_path)
        if not isinstance(raw_items, list):
            log.error("items_path %r did not resolve to a list for %s/%s",
                      items_path, self.site.key, category_id)
            return []

        out: list[ScrapedItem] = []
        for node in raw_items:
            if not isinstance(node, dict):
                continue

            title = dig(node, fields.get("title", "name"))
            if not title:
                continue

            selling = parse_price(str(dig(node, fields.get("selling_price", "")) or ""))
            mrp = parse_price(str(dig(node, fields.get("mrp", "")) or ""))

            slug = dig(node, fields.get("url_slug", "slug")) or ""
            url = self.absolute_url(url_template.format(slug=slug)) or ""

            item = ScrapedItem(
                site_key=self.site.key,
                url=url,
                raw_title=str(title),
                selling_price=selling,
                mrp=mrp,
                category=category_id,
                site_product_id=str(dig(node, fields.get("product_id", "uid")) or "") or None,
            )

            in_stock = dig(node, fields.get("in_stock", "sellable"))
            if in_stock is not None:
                item.in_stock = 1 if in_stock else 0

            # An MRP below the selling price is a data error, not a discount.
            # Dropping it here stops a negative discount reaching the analysis.
            if item.mrp and item.selling_price and item.mrp < item.selling_price:
                item.mrp = None

            if item.is_usable():
                out.append(item)

        log.info("%s/%s -> %s items from JSON state",
                 self.site.key, category_id, len(out))
        return out

    def health_check(self) -> dict[str, Any]:
        """Report whether the state blob and item path still resolve."""
        cfg = self.site.extra.get("json_state") or {}
        marker = cfg.get("marker", "window.__INITIAL_STATE__")
        items_path = cfg.get("items_path", "")

        report: dict[str, Any] = {"site": self.site.key, "strategy": "json_state",
                                  "categories": {}}
        for category_id in self.site.categories:
            html = self.fetch(self.site.category_url(category_id))
            if not html:
                report["categories"][category_id] = {"ok": False, "reason": "fetch failed"}
                continue

            state = extract_state_blob(html, marker)
            if state is None:
                report["categories"][category_id] = {
                    "ok": False, "reason": f"{marker} not found or unparseable"}
                continue

            items = dig(state, items_path)
            parsed = self.parse_listing(html, category_id)
            priced = [i for i in parsed if i.selling_price]
            with_mrp = [i for i in parsed if i.mrp]

            report["categories"][category_id] = {
                "ok": bool(priced),
                "items_in_state": len(items) if isinstance(items, list) else 0,
                "items_parsed": len(parsed),
                "with_price": len(priced),
                "with_mrp": len(with_mrp),
                "sample": priced[0].raw_title[:60] if priced else None,
            }
        return report
