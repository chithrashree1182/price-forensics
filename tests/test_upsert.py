"""Regression test for the same-day upsert.

The bug this pins: on 2026-08-15 the sweep read 60 MRPs off Snapdeal's listing
pages, then the panel visited the same listings' detail pages, whose MRP
selector matched nothing, and the plain `SET mrp = excluded.mrp` upsert erased
every one of them with NULL. A later scrape the same day can know *less* than
an earlier one; absence of a value must never overwrite a value.
"""

from __future__ import annotations

import pytest

from priceforensics import db


@pytest.fixture()
def warehouse(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    with db.connect(path) as conn:
        product_id = db.upsert_product(
            conn, match_key="boat|rockerz 550", brand="boat", model="rockerz 550",
            variant=None, category="headphones", canonical_title="Boat Rockerz 550",
            seen_date="2026-08-15",
        )
        seller_id = db.upsert_seller(conn, site_key="snapdeal", site_name="Snapdeal")
        listing_id = db.upsert_listing(
            conn, product_id=product_id, seller_id=seller_id, site_key="snapdeal",
            url="https://example.com/p/1", raw_title="Boat Rockerz 550",
            site_product_id=None, seen_date="2026-08-15",
        )
    return path, listing_id, product_id, seller_id


def _observe(path, ids, **kw):
    listing_id, product_id, seller_id = ids
    with db.connect(path) as conn:
        db.insert_observation(
            conn, listing_id=listing_id, product_id=product_id,
            seller_id=seller_id, obs_date="2026-08-15", **kw,
        )


def _row(path):
    return db.query(
        "SELECT selling_price, mrp, computed_discount_pct, in_stock "
        "FROM fact_price_observation", path=path,
    )[0]


def test_null_mrp_does_not_erase_earlier_value(warehouse):
    path, *ids = warehouse
    _observe(path, ids, selling_price=1799.0, mrp=4499.0, in_stock=1)   # sweep
    _observe(path, ids, selling_price=1799.0, mrp=None)                 # panel, worse selector
    row = _row(path)
    assert row["mrp"] == 4499.0, "panel NULL erased the sweep's MRP"
    assert row["computed_discount_pct"] is not None


def test_real_new_value_still_overwrites(warehouse):
    """COALESCE must not freeze data — a genuine same-day change still lands."""
    path, *ids = warehouse
    _observe(path, ids, selling_price=1799.0, mrp=4499.0)
    _observe(path, ids, selling_price=1599.0, mrp=4499.0)               # price cut
    assert _row(path)["selling_price"] == 1599.0


def test_still_one_row_per_listing_per_day(warehouse):
    path, *ids = warehouse
    _observe(path, ids, selling_price=1799.0, mrp=4499.0)
    _observe(path, ids, selling_price=1699.0, mrp=None)
    rows = db.query("SELECT COUNT(*) AS n FROM fact_price_observation", path=path)
    assert rows[0]["n"] == 1
