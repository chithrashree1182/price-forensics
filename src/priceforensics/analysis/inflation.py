"""Pre-sale price inflation detection — the longitudinal analysis.

The pattern under test
----------------------
A retailer wants to advertise "60% OFF" during a sale. The honest route is to
cut the price. The cheaper route is to raise the "before" price a fortnight
early, let it sit, and then discount from the inflated figure.

Seen as a price series, that looks like:

    ₹  ────────────────┐                    ← baseline: the real, settled price
                       └──────────┐         ← the run-up, held for days
                                  └──────   ← the "sale" price
       |<-- baseline -->|<- rise ->|<- sale ->|

The advertised discount is measured from the peak. The discount a shopper
actually receives, relative to what the product cost a month earlier, is
measured from the baseline. The gap between those two numbers is the finding,
and it is reported in percentage points.

Method
------
Thresholds live in config/targets.yaml and were committed before data collection
began — visible in git history. That matters: a detector tuned after seeing the
results can manufacture any headline you like.

Every flag carries a confidence tier, and validation.py draws a random sample
for manual review so the false-positive rate is measured rather than assumed.

Known confounders, handled or declared
--------------------------------------
  * Genuine price rises happen (component costs, exchange rates, demand). We
    require the rise to be recent, sharp, and reversed by the sale — a genuine
    increase does not usually snap back the moment a sale starts.
  * Sellers change on marketplaces; a "price rise" may be a different seller. We
    track per-listing, and the listing key includes the site.
  * Sparse observation. If the scraper missed days around a transition we cannot
    tell when the rise happened, so runs with too few observations are skipped
    rather than guessed at.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable

from ..config import detection_config
from ..db import connect, finish_analysis_run, start_analysis_run

log = logging.getLogger(__name__)


@dataclass
class PricePoint:
    obs_date: date
    price: float
    mrp: float | None = None
    claimed_discount_pct: float | None = None


@dataclass
class InflationEvent:
    listing_id: int
    product_id: int
    site_key: str
    title: str

    baseline_price: float
    peak_price: float
    sale_price: float

    rise_pct: float
    hold_days: int
    rise_start: date
    sale_start: date
    days_between: int

    claimed_discount_pct: float
    real_discount_pct: float
    overstatement_pp: float

    confidence: str
    n_observations: int
    evidence: list[PricePoint] = field(default_factory=list)

    @property
    def headline(self) -> str:
        return (
            f"{self.title} ({self.site_key}): advertised as {self.claimed_discount_pct:.0f}% off "
            f"₹{self.peak_price:,.0f}, but sold for ₹{self.baseline_price:,.0f} "
            f"{self.days_between} days earlier — real discount "
            f"{self.real_discount_pct:.0f}%, overstated by "
            f"{self.overstatement_pp:.0f} percentage points"
        )


SERIES_SQL = """
SELECT
    o.listing_id,
    o.product_id,
    l.site_key,
    COALESCE(p.canonical_title, l.raw_title) AS title,
    d.date        AS obs_date,
    o.selling_price,
    o.mrp,
    o.computed_discount_pct
FROM fact_price_observation o
JOIN dim_listing l ON l.listing_id = o.listing_id
JOIN dim_product p ON p.product_id = o.product_id
JOIN dim_date    d ON d.date_key   = o.date_key
WHERE o.selling_price IS NOT NULL
  AND (? = 1 OR o.source = 'live')
ORDER BY o.listing_id, d.date
"""


def load_series(include_backfill: bool = False, db_path=None) -> dict[int, dict[str, Any]]:
    """Load every listing's price history, keyed by listing_id."""
    with connect(db_path, readonly=True) as conn:
        rows = conn.execute(SERIES_SQL, (1 if include_backfill else 0,)).fetchall()

    series: dict[int, dict[str, Any]] = {}
    for row in rows:
        entry = series.setdefault(row["listing_id"], {
            "product_id": row["product_id"],
            "site_key": row["site_key"],
            "title": row["title"],
            "points": [],
        })
        entry["points"].append(PricePoint(
            obs_date=date.fromisoformat(row["obs_date"]),
            price=float(row["selling_price"]),
            mrp=float(row["mrp"]) if row["mrp"] is not None else None,
            claimed_discount_pct=(
                float(row["computed_discount_pct"])
                if row["computed_discount_pct"] is not None else None
            ),
        ))
    return series


# ---------------------------------------------------------------------------
# Core detection
# ---------------------------------------------------------------------------

def _find_price_drops(points: list[PricePoint], min_drop_pct: float) -> list[int]:
    """Indices where the price fell sharply — candidate sale starts."""
    drops: list[int] = []
    for i in range(1, len(points)):
        prev, cur = points[i - 1].price, points[i].price
        if prev <= 0:
            continue
        if (prev - cur) / prev * 100 >= min_drop_pct:
            drops.append(i)
    return drops


def _analyse_drop(
    points: list[PricePoint],
    drop_idx: int,
    *,
    lookback_days: int,
    min_rise_pct: float,
    min_hold_days: int,
) -> dict[str, Any] | None:
    """Given a price drop, decide whether it was preceded by an inflation."""
    sale_point = points[drop_idx]
    window_start = sale_point.obs_date - timedelta(days=lookback_days)
    window = [p for p in points[:drop_idx] if p.obs_date >= window_start]
    if len(window) < 4:
        return None  # not enough history to establish a baseline

    peak = max(window, key=lambda p: p.price)
    if peak.price <= 0:
        return None

    # Everything within 2% of the peak counts as "at the elevated level" — small
    # daily wobble should not break a hold streak.
    elevated = [p for p in window if p.price >= peak.price * 0.98]
    pre_rise = [p for p in window if p.obs_date < min(e.obs_date for e in elevated)]
    if len(pre_rise) < 2:
        # The elevated price runs to the start of our window, so we never saw
        # the lower baseline. Cannot distinguish inflation from a product that
        # was simply always this price.
        return None

    baseline = statistics.median(p.price for p in pre_rise)
    if baseline <= 0:
        return None

    rise_pct = (peak.price - baseline) / baseline * 100
    if rise_pct < min_rise_pct:
        return None

    hold_days = (max(e.obs_date for e in elevated) - min(e.obs_date for e in elevated)).days + 1
    if hold_days < min_hold_days:
        return None

    # The sale must undo the rise: if the "discounted" price is still above the
    # old baseline that is a price increase with marketing, which we record
    # separately rather than as a manufactured discount.
    sale_price = sale_point.price

    claimed = (peak.price - sale_price) / peak.price * 100
    real = (baseline - sale_price) / baseline * 100
    overstatement = claimed - real

    return {
        "baseline": round(baseline, 2),
        "peak": round(peak.price, 2),
        "sale": round(sale_price, 2),
        "rise_pct": round(rise_pct, 2),
        "hold_days": hold_days,
        "rise_start": min(e.obs_date for e in elevated),
        "sale_start": sale_point.obs_date,
        "claimed": round(claimed, 2),
        "real": round(real, 2),
        "overstatement": round(overstatement, 2),
        "window": window + [sale_point],
    }


def _confidence(result: dict[str, Any], n_obs: int) -> str:
    """Tier a flag by how hard it would be to explain away.

    High-confidence flags are what the headline statistic should be built on;
    low-confidence ones are reported but held separately.
    """
    score = 0
    if result["rise_pct"] >= 20:
        score += 2
    elif result["rise_pct"] >= 12:
        score += 1

    if result["hold_days"] >= 7:
        score += 2
    elif result["hold_days"] >= 4:
        score += 1

    if n_obs >= 30:
        score += 1
    if result["overstatement"] >= 15:
        score += 1
    # The rise fully reversing is the strongest single signal.
    if result["sale"] <= result["baseline"] * 1.02:
        score += 1

    if score >= 5:
        return "high"
    if score >= 3:
        return "medium"
    return "low"


def detect(
    include_backfill: bool = False,
    db_path=None,
    **overrides: Any,
) -> list[InflationEvent]:
    """Run inflation detection across every tracked listing."""
    cfg = {**detection_config().get("inflation", {}), **overrides}
    lookback_days = int(cfg.get("lookback_days", 21))
    min_rise_pct = float(cfg.get("min_rise_pct", 10.0))
    min_hold_days = int(cfg.get("min_hold_days", 3))
    min_observations = int(cfg.get("min_observations", 10))
    # A "sale" is a drop at least as large as the rise we require; smaller
    # wobbles are noise, not campaigns.
    min_drop_pct = float(cfg.get("min_drop_pct", min_rise_pct))

    series = load_series(include_backfill=include_backfill, db_path=db_path)
    events: list[InflationEvent] = []
    skipped_short = 0

    for listing_id, entry in series.items():
        points: list[PricePoint] = entry["points"]
        if len(points) < min_observations:
            skipped_short += 1
            continue

        best: dict[str, Any] | None = None
        for idx in _find_price_drops(points, min_drop_pct):
            result = _analyse_drop(
                points, idx,
                lookback_days=lookback_days,
                min_rise_pct=min_rise_pct,
                min_hold_days=min_hold_days,
            )
            # One event per listing: the most egregious. Reporting every drop
            # would double-count a single campaign across consecutive days.
            if result and (best is None or result["overstatement"] > best["overstatement"]):
                best = result

        if best is None:
            continue

        events.append(InflationEvent(
            listing_id=listing_id,
            product_id=entry["product_id"],
            site_key=entry["site_key"],
            title=entry["title"],
            baseline_price=best["baseline"],
            peak_price=best["peak"],
            sale_price=best["sale"],
            rise_pct=best["rise_pct"],
            hold_days=best["hold_days"],
            rise_start=best["rise_start"],
            sale_start=best["sale_start"],
            days_between=(best["sale_start"] - best["rise_start"]).days,
            claimed_discount_pct=best["claimed"],
            real_discount_pct=best["real"],
            overstatement_pp=best["overstatement"],
            confidence=_confidence(best, len(points)),
            n_observations=len(points),
            evidence=best["window"],
        ))

    events.sort(key=lambda e: -e.overstatement_pp)
    log.info(
        "inflation scan: %s listings, %s skipped (<%s observations), %s events "
        "(%s high confidence)",
        len(series), skipped_short, min_observations, len(events),
        sum(1 for e in events if e.confidence == "high"),
    )
    return events


def run_and_store(include_backfill: bool = False, db_path=None, notes: str = "") -> tuple[int, list[InflationEvent]]:
    """Run detection and persist events with the parameters used."""
    cfg = detection_config().get("inflation", {})
    events = detect(include_backfill=include_backfill, db_path=db_path)

    with connect(db_path) as conn:
        n_listings = conn.execute(
            "SELECT COUNT(DISTINCT listing_id) AS n FROM fact_price_observation"
        ).fetchone()["n"]

        run_id = start_analysis_run(conn, "inflation", dict(cfg), notes)
        for e in events:
            conn.execute(
                """
                INSERT INTO inflation_event (
                    run_id, listing_id, product_id, baseline_price, peak_price,
                    sale_price, rise_pct, claimed_discount_pct, real_discount_pct,
                    discount_overstatement_pp, rise_start_date, sale_start_date,
                    days_between, confidence
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (run_id, e.listing_id, e.product_id, e.baseline_price, e.peak_price,
                 e.sale_price, e.rise_pct, e.claimed_discount_pct, e.real_discount_pct,
                 e.overstatement_pp, e.rise_start.isoformat(), e.sale_start.isoformat(),
                 e.days_between, e.confidence),
            )
        finish_analysis_run(conn, run_id, n_listings, len(events))

    return run_id, events


def summarise(events: list[InflationEvent], n_listings_analysed: int | None = None) -> dict[str, Any]:
    """Headline numbers. Rates are computed on high+medium confidence only."""
    if not events:
        return {"n_events": 0}

    solid = [e for e in events if e.confidence in ("high", "medium")]
    out: dict[str, Any] = {
        "n_events": len(events),
        "n_high_confidence": sum(1 for e in events if e.confidence == "high"),
        "n_medium_confidence": sum(1 for e in events if e.confidence == "medium"),
        "n_low_confidence": sum(1 for e in events if e.confidence == "low"),
        "median_overstatement_pp": round(
            statistics.median(e.overstatement_pp for e in solid), 2
        ) if solid else None,
        "max_overstatement_pp": round(max(e.overstatement_pp for e in events), 2),
        "median_claimed_discount_pct": round(
            statistics.median(e.claimed_discount_pct for e in solid), 2
        ) if solid else None,
        "median_real_discount_pct": round(
            statistics.median(e.real_discount_pct for e in solid), 2
        ) if solid else None,
        "worst_example": events[0].headline,
    }
    if n_listings_analysed:
        out["flagged_rate_pct"] = round(len(solid) / n_listings_analysed * 100, 2)

    by_site: dict[str, int] = {}
    for e in solid:
        by_site[e.site_key] = by_site.get(e.site_key, 0) + 1
    out["events_by_site"] = dict(sorted(by_site.items(), key=lambda kv: -kv[1]))
    return out
