"""SQLite warehouse access.

Thin layer over sqlite3 — deliberately not an ORM. The analysis is SQL-heavy by
design (window functions do most of the work), so hiding SQL behind objects
would cost more than it saves.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .config import DB_PATH, ROOT, ensure_dirs, festival_windows

SCHEMA_PATH = ROOT / "sql" / "schema.sql"


def date_key(d: date | str) -> int:
    """yyyy-mm-dd -> yyyymmdd integer surrogate key."""
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return d.year * 10000 + d.month * 100 + d.day


@contextmanager
def connect(path: Path | None = None, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    ensure_dirs()
    target = Path(path or DB_PATH)
    if readonly and target.exists():
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        if not readonly:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(path: Path | None = None) -> None:
    """Create the schema. Idempotent."""
    ddl = SCHEMA_PATH.read_text(encoding="utf-8")
    with connect(path) as conn:
        conn.executescript(ddl)
    populate_dim_date(path=path)


# ---------------------------------------------------------------------------
# dim_date
# ---------------------------------------------------------------------------

def populate_dim_date(
    start: date | None = None,
    end: date | None = None,
    path: Path | None = None,
) -> int:
    """Fill the date dimension, including festival-window flags.

    Power BI requires a contiguous, gap-free date table for time intelligence,
    so we generate every day in the range rather than only days with data.
    """
    start = start or date(2023, 1, 1)   # early enough to cover Wayback backfill
    end = end or (date.today() + timedelta(days=400))

    windows = []
    for w in festival_windows():
        windows.append((w["name"], date.fromisoformat(w["start"]), date.fromisoformat(w["end"])))

    rows: list[tuple] = []
    cur = start
    while cur <= end:
        window_name: str | None = None
        is_festival = 0
        days_to: int | None = None

        for name, w_start, w_end in windows:
            if w_start <= cur <= w_end:
                window_name, is_festival, days_to = name, 1, 0
                break
        if not is_festival and windows:
            # distance to the nearest upcoming window (negative = before)
            upcoming = [(w_start - cur).days for _, w_start, _ in windows if w_start > cur]
            if upcoming:
                days_to = -min(upcoming)

        rows.append((
            date_key(cur), cur.isoformat(), cur.year, (cur.month - 1) // 3 + 1,
            cur.month, cur.strftime("%B"), cur.day, cur.isoweekday(),
            cur.strftime("%A"), 1 if cur.isoweekday() >= 6 else 0,
            window_name, is_festival, days_to,
        ))
        cur += timedelta(days=1)

    with connect(path) as conn:
        conn.executemany(
            """
            INSERT INTO dim_date (
                date_key, date, year, quarter, month, month_name, day,
                day_of_week, day_name, is_weekend, festival_window,
                is_festival, days_to_festival
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(date_key) DO UPDATE SET
                festival_window  = excluded.festival_window,
                is_festival      = excluded.is_festival,
                days_to_festival = excluded.days_to_festival
            """,
            rows,
        )
    return len(rows)


# ---------------------------------------------------------------------------
# Dimension upserts
# ---------------------------------------------------------------------------

def upsert_product(
    conn: sqlite3.Connection,
    *,
    match_key: str,
    brand: str | None,
    model: str | None,
    variant: str | None,
    category: str,
    canonical_title: str,
    seen_date: str,
) -> int:
    row = conn.execute(
        "SELECT product_id FROM dim_product WHERE match_key = ?", (match_key,)
    ).fetchone()
    if row:
        return int(row["product_id"])
    cur = conn.execute(
        """
        INSERT INTO dim_product
            (match_key, brand, model, variant, category, canonical_title, first_seen_date)
        VALUES (?,?,?,?,?,?,?)
        """,
        (match_key, brand, model, variant, category, canonical_title, seen_date),
    )
    return int(cur.lastrowid)


def upsert_seller(
    conn: sqlite3.Connection,
    *,
    site_key: str,
    site_name: str,
    seller_name: str | None = None,
) -> int:
    row = conn.execute(
        "SELECT seller_id FROM dim_seller WHERE site_key = ? AND seller_name IS ?",
        (site_key, seller_name),
    ).fetchone()
    if row:
        return int(row["seller_id"])
    cur = conn.execute(
        """
        INSERT INTO dim_seller (site_key, site_name, seller_name, is_first_party)
        VALUES (?,?,?,?)
        """,
        (site_key, site_name, seller_name, 1 if seller_name is None else 0),
    )
    return int(cur.lastrowid)


def upsert_listing(
    conn: sqlite3.Connection,
    *,
    product_id: int,
    seller_id: int,
    site_key: str,
    url: str,
    raw_title: str,
    site_product_id: str | None,
    seen_date: str,
) -> int:
    row = conn.execute(
        "SELECT listing_id FROM dim_listing WHERE site_key = ? AND url = ?",
        (site_key, url),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE dim_listing SET last_seen_date = ?, is_active = 1 WHERE listing_id = ?",
            (seen_date, row["listing_id"]),
        )
        return int(row["listing_id"])
    cur = conn.execute(
        """
        INSERT INTO dim_listing
            (product_id, seller_id, site_key, site_product_id, url, raw_title,
             first_seen_date, last_seen_date)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (product_id, seller_id, site_key, site_product_id, url, raw_title,
         seen_date, seen_date),
    )
    return int(cur.lastrowid)


def insert_observation(
    conn: sqlite3.Connection,
    *,
    listing_id: int,
    product_id: int,
    seller_id: int,
    obs_date: str,
    selling_price: float | None,
    mrp: float | None,
    discount_pct: float | None = None,
    in_stock: int | None = None,
    rating: float | None = None,
    rating_count: int | None = None,
    source: str = "live",
    raw_snapshot: str | None = None,
) -> None:
    computed = None
    if mrp and selling_price and mrp > 0 and mrp >= selling_price:
        computed = round((mrp - selling_price) / mrp * 100, 2)

    conn.execute(
        """
        INSERT INTO fact_price_observation (
            listing_id, date_key, product_id, seller_id, selling_price, mrp,
            discount_pct, computed_discount_pct, in_stock, rating, rating_count,
            scraped_at, source, raw_snapshot
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(listing_id, date_key, source) DO UPDATE SET
            -- COALESCE: a later scrape the same day may know LESS than an
            -- earlier one. Concretely: the sweep reads MRPs off Snapdeal's
            -- listing page, then the panel visits detail pages whose MRP
            -- selector may match nothing — and a plain "SET mrp=excluded.mrp"
            -- erased every MRP collected an hour earlier. Never overwrite a
            -- value with the absence of one.
            selling_price = COALESCE(excluded.selling_price, selling_price),
            mrp           = COALESCE(excluded.mrp, mrp),
            discount_pct  = COALESCE(excluded.discount_pct, discount_pct),
            computed_discount_pct = COALESCE(excluded.computed_discount_pct,
                                             computed_discount_pct),
            in_stock      = COALESCE(excluded.in_stock, in_stock),
            scraped_at    = excluded.scraped_at
        """,
        (listing_id, date_key(obs_date), product_id, seller_id, selling_price,
         mrp, discount_pct, computed, in_stock, rating, rating_count,
         datetime.now().isoformat(timespec="seconds"), source, raw_snapshot),
    )


def insert_dark_pattern(
    conn: sqlite3.Connection,
    *,
    listing_id: int,
    obs_date: str,
    pattern_type: str,
    raw_text: str,
    numeric_value: float | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO fact_dark_pattern
            (listing_id, date_key, pattern_type, raw_text, numeric_value, scraped_at)
        VALUES (?,?,?,?,?,?)
        """,
        (listing_id, date_key(obs_date), pattern_type, raw_text, numeric_value,
         datetime.now().isoformat(timespec="seconds")),
    )


def start_analysis_run(
    conn: sqlite3.Connection, run_type: str, params: dict[str, Any], notes: str = ""
) -> int:
    cur = conn.execute(
        "INSERT INTO analysis_run (run_type, params_json, notes) VALUES (?,?,?)",
        (run_type, json.dumps(params, sort_keys=True), notes),
    )
    return int(cur.lastrowid)


def finish_analysis_run(
    conn: sqlite3.Connection, run_id: int, n_input: int, n_flagged: int
) -> None:
    conn.execute(
        "UPDATE analysis_run SET n_input = ?, n_flagged = ? WHERE run_id = ?",
        (n_input, n_flagged, run_id),
    )


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def query(sql: str, params: Sequence[Any] = (), path: Path | None = None) -> list[sqlite3.Row]:
    with connect(path, readonly=True) as conn:
        return conn.execute(sql, params).fetchall()


def run_sql_file(name: str, path: Path | None = None) -> list[sqlite3.Row]:
    """Execute a numbered file from sql/ and return its rows."""
    sql = (ROOT / "sql" / name).read_text(encoding="utf-8")
    return query(sql, path=path)


def table_counts(path: Path | None = None) -> dict[str, int]:
    tables = [
        "dim_product", "dim_seller", "dim_listing", "dim_date",
        "fact_price_observation", "fact_dark_pattern",
        "inflation_event", "mrp_contradiction", "analysis_run",
    ]
    out: dict[str, int] = {}
    with connect(path, readonly=True) as conn:
        for t in tables:
            try:
                out[t] = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            except sqlite3.OperationalError:
                out[t] = -1
    return out
