"""Feature engineering: turning a price series into a fixed-length vector.

This is the step that makes machine learning possible here. A listing's history
is a variable-length, irregularly-sampled sequence; a model needs a fixed-width
row. These features are chosen to describe *shape* rather than level, so a
Rs 800 pair of earphones and a Rs 80,000 laptop are directly comparable.

Everything is scale-invariant by construction: ratios, percentages and
normalised counts, never raw rupees. Without that, Isolation Forest would spend
its capacity discovering that laptops cost more than earphones — which is true,
and useless.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from ..db import connect

log = logging.getLogger(__name__)

# Order matters: this is the column order of the design matrix.
FEATURE_NAMES = [
    "n_observations",
    "coef_variation",
    "range_ratio",
    "n_price_levels_norm",
    "max_daily_rise_pct",
    "max_daily_drop_pct",
    "n_large_moves_norm",
    "max_rise_21d_pct",
    "max_drop_after_rise_pct",
    "frac_days_at_max",
    "frac_days_at_min",
    "time_at_elevated_frac",
    "peak_to_median_pct",
    "median_to_min_pct",
    "rise_then_fall_score",
    "lag1_autocorr",
    "mean_abs_daily_change_pct",
    "trend_slope_norm",
]


@dataclass
class ListingFeatures:
    listing_id: int
    site_key: str
    title: str
    n_observations: int
    coef_variation: float
    range_ratio: float
    n_price_levels_norm: float
    max_daily_rise_pct: float
    max_daily_drop_pct: float
    n_large_moves_norm: float
    max_rise_21d_pct: float
    max_drop_after_rise_pct: float
    frac_days_at_max: float
    frac_days_at_min: float
    time_at_elevated_frac: float
    peak_to_median_pct: float
    median_to_min_pct: float
    rise_then_fall_score: float
    lag1_autocorr: float
    mean_abs_daily_change_pct: float
    trend_slope_norm: float

    def vector(self) -> list[float]:
        d = asdict(self)
        return [float(d[name]) for name in FEATURE_NAMES]


SERIES_SQL = """
SELECT
    o.listing_id,
    l.site_key,
    COALESCE(p.canonical_title, l.raw_title) AS title,
    d.date          AS obs_date,
    o.selling_price
FROM fact_price_observation o
JOIN dim_listing l ON l.listing_id = o.listing_id
JOIN dim_product p ON p.product_id = o.product_id
JOIN dim_date    d ON d.date_key   = o.date_key
WHERE o.selling_price IS NOT NULL
  AND (? = 1 OR o.source = 'live')
ORDER BY o.listing_id, d.date
"""


def _safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b else default


def extract(prices: list[float], dates: list[date] | None = None) -> dict[str, float]:
    """Compute the feature vector for one price series."""
    n = len(prices)
    if n < 3:
        return {name: 0.0 for name in FEATURE_NAMES}

    mean = statistics.fmean(prices)
    median = statistics.median(prices)
    lo, hi = min(prices), max(prices)
    stdev = statistics.pstdev(prices)

    # --- day-over-day movements (as percentages, so scale-free) -------------
    changes = []
    for i in range(1, n):
        prev = prices[i - 1]
        if prev > 0:
            changes.append((prices[i] - prev) / prev * 100)
    changes = changes or [0.0]

    rises = [c for c in changes if c > 0]
    drops = [c for c in changes if c < 0]

    # --- the signature feature -------------------------------------------
    # Largest rise within any trailing 21-observation window, and the largest
    # drop that FOLLOWS it. A manufactured discount scores high on both; a
    # steady decline or an honest one-off cut does not.
    max_rise_21d, max_drop_after = 0.0, 0.0
    window = 21
    for i in range(n):
        lo_i = max(0, i - window)
        seg = prices[lo_i:i + 1]
        if len(seg) < 3:
            continue
        seg_min, seg_max = min(seg), max(seg)
        if seg_min <= 0:
            continue
        rise = (seg_max - seg_min) / seg_min * 100
        # Only count it if the max came after the min (i.e. it went up).
        if seg.index(seg_max) > seg.index(seg_min) and rise > max_rise_21d:
            max_rise_21d = rise
            tail = prices[i:]
            if tail and seg_max > 0:
                max_drop_after = (seg_max - min(tail)) / seg_max * 100

    rise_then_fall = min(max_rise_21d, max_drop_after)

    # --- level structure --------------------------------------------------
    # Quantise at 0.5% of the median rather than a fixed rupee amount. A fixed
    # tick (e.g. round to Rs 10) is an absolute tolerance and silently breaks
    # scale invariance: it collapses distinct prices on a Rs 500 product while
    # preserving noise on a Rs 80,000 one.
    tick = max(median * 0.005, 1e-9)
    n_levels = len({round(p / tick) for p in prices})
    at_max = sum(1 for p in prices if p >= hi * 0.99)
    at_min = sum(1 for p in prices if p <= lo * 1.01)
    elevated = sum(1 for p in prices if p > median * 1.05)

    # --- autocorrelation: how "sticky" is the price? ----------------------
    lag1 = 0.0
    if stdev > 0 and n > 3:
        cov = sum((prices[i] - mean) * (prices[i - 1] - mean) for i in range(1, n))
        lag1 = _safe_div(cov, (n - 1) * stdev * stdev)

    # --- trend: normalised least-squares slope ----------------------------
    xs = list(range(n))
    x_mean = statistics.fmean(xs)
    denom = sum((x - x_mean) ** 2 for x in xs)
    slope = _safe_div(sum((xs[i] - x_mean) * (prices[i] - mean) for i in range(n)), denom)
    trend_norm = _safe_div(slope * n, mean) * 100   # % change across the series

    return {
        "n_observations": float(n),
        "coef_variation": _safe_div(stdev, mean) * 100,
        "range_ratio": _safe_div(hi - lo, median) * 100,
        "n_price_levels_norm": _safe_div(n_levels, n),
        "max_daily_rise_pct": max(rises) if rises else 0.0,
        "max_daily_drop_pct": abs(min(drops)) if drops else 0.0,
        "n_large_moves_norm": _safe_div(sum(1 for c in changes if abs(c) >= 5), n),
        "max_rise_21d_pct": max_rise_21d,
        "max_drop_after_rise_pct": max_drop_after,
        "frac_days_at_max": _safe_div(at_max, n),
        "frac_days_at_min": _safe_div(at_min, n),
        "time_at_elevated_frac": _safe_div(elevated, n),
        "peak_to_median_pct": _safe_div(hi - median, median) * 100,
        "median_to_min_pct": _safe_div(median - lo, median) * 100,
        "rise_then_fall_score": rise_then_fall,
        "lag1_autocorr": lag1,
        "mean_abs_daily_change_pct": statistics.fmean(abs(c) for c in changes),
        "trend_slope_norm": trend_norm,
    }


def build_dataset(
    include_synthetic: bool = False,
    min_observations: int = 10,
    db_path=None,
) -> list[ListingFeatures]:
    """Build the design matrix: one row per listing."""
    with connect(db_path, readonly=True) as conn:
        rows = conn.execute(SERIES_SQL, (1 if include_synthetic else 0,)).fetchall()

    grouped: dict[int, dict[str, Any]] = {}
    for row in rows:
        entry = grouped.setdefault(row["listing_id"], {
            "site_key": row["site_key"], "title": row["title"],
            "prices": [], "dates": [],
        })
        entry["prices"].append(float(row["selling_price"]))
        entry["dates"].append(date.fromisoformat(row["obs_date"]))

    out: list[ListingFeatures] = []
    for listing_id, entry in grouped.items():
        if len(entry["prices"]) < min_observations:
            continue
        feats = extract(entry["prices"], entry["dates"])
        out.append(ListingFeatures(
            listing_id=listing_id,
            site_key=entry["site_key"],
            title=entry["title"],
            **feats,
        ))

    log.info("built feature matrix: %s listings x %s features", len(out), len(FEATURE_NAMES))
    return out
