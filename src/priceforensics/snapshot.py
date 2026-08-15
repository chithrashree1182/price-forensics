"""Daily CSV snapshots — the durable record.

The problem: GitHub Actions runners are ephemeral. A SQLite file written during
a scheduled run vanishes when the job ends, and committing a binary database
every day produces an unusable diff history.

The solution: each day's observations are written to a small gzipped CSV under
`data/daily/YYYY-MM-DD.csv.gz` and committed. The database becomes a derived
artefact that can be rebuilt from the CSVs at any time with `pf rebuild`.

Three things fall out of this that are worth more than the storage trick:

  * The data is version-controlled, so any figure in the write-up can be traced
    back to the exact rows that produced it.
  * A corrupted or lost database costs nothing.
  * The commit history *is* the collection log — one commit per day, with the
    row count in the message. That is also the honest answer to "did you really
    run this for two months?"
"""

from __future__ import annotations

import csv
import gzip
import logging
from datetime import date
from pathlib import Path

from .config import DATA_DIR, ensure_dirs
from .db import connect, date_key

log = logging.getLogger(__name__)

DAILY_DIR = DATA_DIR / "daily"

COLUMNS = [
    "obs_date", "site_key", "url", "raw_title", "category",
    "selling_price", "mrp", "discount_pct", "in_stock", "rating_count",
    "source",
]

EXPORT_SQL = """
SELECT
    d.date          AS obs_date,
    l.site_key      AS site_key,
    l.url           AS url,
    l.raw_title     AS raw_title,
    p.category      AS category,
    o.selling_price AS selling_price,
    o.mrp           AS mrp,
    o.discount_pct  AS discount_pct,
    o.in_stock      AS in_stock,
    o.rating_count  AS rating_count,
    o.source        AS source
FROM fact_price_observation o
JOIN dim_listing l ON l.listing_id = o.listing_id
JOIN dim_product p ON p.product_id = o.product_id
JOIN dim_date    d ON d.date_key   = o.date_key
WHERE o.date_key = ? AND o.source = 'live'
ORDER BY l.site_key, l.url
"""


def write_snapshot(obs_date: date | None = None, db_path=None) -> Path | None:
    """Write one day's live observations to a gzipped CSV."""
    obs_date = obs_date or date.today()
    DAILY_DIR.mkdir(parents=True, exist_ok=True)

    with connect(db_path, readonly=True) as conn:
        rows = conn.execute(EXPORT_SQL, (date_key(obs_date),)).fetchall()

    if not rows:
        log.warning("no live observations for %s — nothing to snapshot", obs_date)
        return None

    out_path = DAILY_DIR / f"{obs_date.isoformat()}.csv.gz"
    with gzip.open(out_path, "wt", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row[c] for c in COLUMNS})

    log.info("snapshot %s: %s rows -> %s", obs_date, len(rows), out_path)
    return out_path


def rebuild(db_path=None, since: date | None = None) -> dict[str, int]:
    """Rebuild the warehouse from committed daily snapshots.

    Idempotent: re-running produces the same database, because the observation
    unique constraint is (listing_id, date_key, source).
    """
    from .collect import store_items
    from .scrapers.base import ScrapedItem

    ensure_dirs()
    if not DAILY_DIR.exists():
        log.warning("no daily snapshots at %s", DAILY_DIR)
        return {"files": 0, "rows": 0}

    files = sorted(DAILY_DIR.glob("*.csv.gz"))
    if since:
        files = [f for f in files if f.stem.replace(".csv", "") >= since.isoformat()]

    total = 0
    for path in files:
        day = path.stem.replace(".csv", "")
        items: list[ScrapedItem] = []

        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                items.append(ScrapedItem(
                    site_key=row["site_key"],
                    url=row["url"],
                    raw_title=row["raw_title"],
                    category=row["category"] or "unknown",
                    selling_price=_maybe_float(row["selling_price"]),
                    mrp=_maybe_float(row["mrp"]),
                    discount_pct=_maybe_float(row["discount_pct"]),
                    in_stock=_maybe_int(row["in_stock"]),
                    rating_count=_maybe_int(row["rating_count"]),
                    source=row.get("source") or "live",
                ))

        stats = store_items(items, day, db_path=db_path)
        total += stats["stored"]
        log.info("rebuilt %s: %s rows", day, stats["stored"])

    return {"files": len(files), "rows": total}


def _maybe_float(value: str | None) -> float | None:
    if value in (None, "", "None"):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _maybe_int(value: str | None) -> int | None:
    if value in (None, "", "None"):
        return None
    try:
        return int(float(value))
    except ValueError:
        return None
