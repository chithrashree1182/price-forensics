"""Collection pipeline: scrapers -> normalisation -> warehouse.

One run per day. The daily job is deliberately idempotent — re-running it for
the same date updates rather than duplicates, because a partially failed run
followed by a retry is the normal case, not the exception.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Iterable

from .config import load_sites, load_targets
from .db import (
    connect,
    insert_dark_pattern,
    insert_observation,
    upsert_listing,
    upsert_product,
    upsert_seller,
)
from .normalize import parse_title
from .scrapers import ScrapedItem, get_scraper

log = logging.getLogger(__name__)


def store_items(
    items: Iterable[ScrapedItem],
    obs_date: date | str,
    db_path=None,
) -> dict[str, int]:
    """Normalise and persist scraped items. Returns counts."""
    if isinstance(obs_date, date):
        obs_date = obs_date.isoformat()

    stats = {"stored": 0, "skipped": 0, "products_created": 0,
             "dark_patterns": 0, "discovered": 0}
    sites = load_sites()

    with connect(db_path) as conn:
        for item in items:
            # A price-less item is not a failure: JSON-LD category pages carry
            # names and URLs but no prices, so the sweep's job is *discovery*.
            # Register the listing so the panel knows to visit it, and record
            # no observation. Rejecting these outright left the panel with
            # nothing to scrape and the whole pipeline silently empty.
            discovery_only = item.selling_price is None

            if not item.raw_title or not item.url:
                stats["skipped"] += 1
                continue

            parsed = parse_title(item.raw_title, item.category)
            site = sites.get(item.site_key)

            before = conn.execute(
                "SELECT COUNT(*) AS n FROM dim_product"
            ).fetchone()["n"]

            product_id = upsert_product(
                conn,
                match_key=parsed.match_key,
                brand=parsed.brand,
                model=parsed.model,
                variant=parsed.variant,
                category=item.category or "unknown",
                canonical_title=parsed.canonical_title,
                seen_date=obs_date,
            )
            after = conn.execute("SELECT COUNT(*) AS n FROM dim_product").fetchone()["n"]
            if after > before:
                stats["products_created"] += 1

            seller_id = upsert_seller(
                conn,
                site_key=item.site_key,
                site_name=site.display_name if site else item.site_key,
                seller_name=item.seller_name,
            )
            listing_id = upsert_listing(
                conn,
                product_id=product_id,
                seller_id=seller_id,
                site_key=item.site_key,
                url=item.url,
                raw_title=item.raw_title,
                site_product_id=item.site_product_id,
                seen_date=obs_date,
            )
            if discovery_only:
                stats["discovered"] += 1
                continue

            insert_observation(
                conn,
                listing_id=listing_id,
                product_id=product_id,
                seller_id=seller_id,
                obs_date=obs_date,
                selling_price=item.selling_price,
                mrp=item.mrp,
                discount_pct=item.discount_pct,
                in_stock=item.in_stock,
                rating=item.rating,
                rating_count=item.rating_count,
                source=item.source,
                raw_snapshot=item.raw_snapshot,
            )
            stats["stored"] += 1

            for kind, text, value in item.dark_patterns:
                insert_dark_pattern(
                    conn,
                    listing_id=listing_id,
                    obs_date=obs_date,
                    pattern_type=kind,
                    raw_text=text,
                    numeric_value=value,
                )
                stats["dark_patterns"] += 1

    return stats


def run_sweep(
    site_keys: list[str] | None = None,
    category_ids: list[str] | None = None,
    obs_date: date | None = None,
    pages: int | None = None,
    dry_run: bool = False,
    db_path=None,
) -> dict[str, Any]:
    """Daily category sweep across configured sites."""
    obs_date = obs_date or date.today()
    targets = load_targets()
    sweep_cfg = targets.get("sweep", {})
    pages = pages or int(sweep_cfg.get("pages_per_category", 2))

    categories = sweep_cfg.get("categories", []) or []
    if category_ids:
        categories = [c for c in categories if c["id"] in category_ids]

    sites = load_sites()
    report: dict[str, Any] = {"date": obs_date.isoformat(), "sites": {}}

    # Invert the config: iterate site-first so each browser is launched once.
    plan: dict[str, list[str]] = {}
    for cat in categories:
        for site_key in cat.get("sites", []):
            if site_keys and site_key not in site_keys:
                continue
            plan.setdefault(site_key, []).append(cat["id"])

    for site_key, cats in plan.items():
        site = sites.get(site_key)
        if site is None:
            log.warning("unknown site %r in targets.yaml", site_key)
            continue

        log.info("sweeping %s: %s", site_key, ", ".join(cats))
        all_items: list[ScrapedItem] = []
        with get_scraper(site, run_date=obs_date, dry_run=dry_run) as scraper:
            for category_id in cats:
                if category_id not in site.categories:
                    log.warning("site %s has no category %r", site_key, category_id)
                    continue
                items = scraper.scrape_category(category_id, pages=pages)
                for item in items:
                    item.category = category_id
                all_items.extend(items)
            scraper_stats = dict(scraper.stats)

        store_stats = store_items(all_items, obs_date, db_path=db_path) if not dry_run else {}
        report["sites"][site_key] = {
            "categories": cats,
            "items_scraped": len(all_items),
            "scraper": scraper_stats,
            "storage": store_stats,
        }

    return report


def run_panel(
    obs_date: date | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    db_path=None,
) -> dict[str, Any]:
    """Scrape the fixed daily panel of product detail pages."""
    obs_date = obs_date or date.today()
    sites = load_sites()

    with connect(db_path, readonly=True) as conn:
        rows = conn.execute(
            """
            SELECT l.listing_id, l.site_key, l.url
            FROM dim_listing l
            WHERE l.is_active = 1
            ORDER BY l.listing_id
            """
        ).fetchall()

    by_site: dict[str, list[str]] = {}
    for row in rows:
        by_site.setdefault(row["site_key"], []).append(row["url"])

    report: dict[str, Any] = {"date": obs_date.isoformat(), "sites": {}}
    for site_key, urls in by_site.items():
        site = sites.get(site_key)
        if site is None:
            continue
        if limit:
            urls = urls[:limit]

        with get_scraper(site, run_date=obs_date, dry_run=dry_run) as scraper:
            items = scraper.scrape_listing_urls(urls)
            scraper_stats = dict(scraper.stats)

        store_stats = store_items(items, obs_date, db_path=db_path) if not dry_run else {}
        report["sites"][site_key] = {
            "urls_attempted": len(urls),
            "items_scraped": len(items),
            "scraper": scraper_stats,
            "storage": store_stats,
        }

    return report


def seed_panel_from_sweep(db_path=None) -> int:
    """Mark products listed on multiple sites as panel members.

    Cross-seller MRP comparison only works where the same product appears on
    several sites, so the panel prioritises exactly that overlap.
    """
    targets = load_targets().get("panel", {}).get("auto_seed", {})
    min_sites = int(targets.get("min_sites_listing_product", 2))
    max_products = int(targets.get("max_products", 300))

    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT product_id, COUNT(DISTINCT site_key) AS n_sites
            FROM dim_listing
            GROUP BY product_id
            HAVING n_sites >= ?
            ORDER BY n_sites DESC, product_id
            LIMIT ?
            """,
            (min_sites, max_products),
        ).fetchall()

        product_ids = [r["product_id"] for r in rows]
        if product_ids:
            placeholders = ",".join("?" * len(product_ids))
            conn.execute(
                f"UPDATE dim_listing SET is_active = 1 WHERE product_id IN ({placeholders})",
                product_ids,
            )
    log.info("panel seeded with %s multi-site products", len(product_ids))
    return len(product_ids)
