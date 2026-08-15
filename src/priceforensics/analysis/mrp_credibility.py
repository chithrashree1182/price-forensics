"""MRP credibility — testing whether claimed MRPs behave like real ones.

Why this exists
---------------
The original plan was a cross-seller MRP audit: find the same SKU on several
sites and flag disagreement. That analysis is implemented (mrp_audit.py) and
unit-tested, but it cannot run. Four of the six reachable retailers block
scrapers outright, and the two that remain sell disjoint catalogues - Reliance
Digital lists branded electronics, Snapdeal lists unbranded marketplace stock.
No shared SKUs means nothing to compare.

This module tests the same question from a different angle, one that needs no
overlap at all.

The argument
------------
MRP is a manufacturer-declared price printed on the pack. It is therefore a
property of the *product*: different products have different MRPs, and those
values inherit the irregularity of real cost structures (Rs 89,600; Rs 23,999;
Rs 215,900).

If a retailer's claimed MRPs instead cluster on a small menu of round numbers -
Rs 999, Rs 1,499, Rs 1,999 - reused across dozens of unrelated products, then
those values are not being read off packaging. They are being chosen, and the
number chosen is whichever one makes the discount look largest.

This does not require knowing the true MRP of any single product. It is an
argument about the *distribution*, which is why one day of data is enough.

Three measures
--------------
1. **Diversity** - distinct MRP values divided by product count. Genuine
   catalogues approach 1.0; a menu of stock values drives it toward 0.
2. **Concentration** - what share of products sit on the five most-used MRP
   values. High concentration means a small menu.
3. **Round-number rate** - share of MRPs ending in 99, 00 or 000. Real prices
   do cluster somewhat on psychological endings, so this is corroborating
   evidence rather than proof, and is reported as such.

What this does NOT claim
------------------------
It cannot show any individual MRP is false - only that a set of them, taken
together, does not behave the way product-specific prices behave. The output
language is "inconsistent with product-specific pricing", never "fraudulent".
"""

from __future__ import annotations

import logging
import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ..db import connect

log = logging.getLogger(__name__)


@dataclass
class RetailerMRPProfile:
    site_key: str
    n_products: int
    n_distinct_mrp: int
    diversity_ratio: float          # distinct / n  -> 1.0 is healthy
    top5_concentration: float       # share on the 5 most common values
    round_number_rate: float        # share ending 99 / 00 / 000
    median_discount_pct: float
    discount_range: tuple[float, float]
    most_repeated: list[tuple[float, int]] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        """Conservative tiering. Thresholds are declared, not tuned to a result."""
        if self.n_products < 20:
            return "insufficient"
        if self.diversity_ratio < 0.35 and self.top5_concentration > 0.50:
            return "inconsistent with product-specific pricing"
        if self.diversity_ratio < 0.60:
            return "unusually concentrated"
        return "consistent with product-specific pricing"

    @property
    def headline(self) -> str:
        return (
            f"{self.site_key}: {self.n_products} products share only "
            f"{self.n_distinct_mrp} distinct MRP values "
            f"(diversity {self.diversity_ratio:.2f}, "
            f"top-5 concentration {self.top5_concentration:.0%}) — {self.verdict}"
        )


PROFILE_SQL = """
SELECT
    l.site_key,
    o.mrp,
    o.selling_price,
    o.computed_discount_pct
FROM fact_price_observation o
JOIN dim_listing l ON l.listing_id = o.listing_id
WHERE o.mrp IS NOT NULL
  AND o.mrp > 0
  AND o.selling_price IS NOT NULL
  AND (o.source = 'live' OR ? = 1)
  AND o.date_key = (
      SELECT MAX(date_key) FROM fact_price_observation
      WHERE listing_id = o.listing_id
  )
"""


def _is_round(value: float) -> bool:
    """Psychological-pricing ending: the value terminates in 99.

    A first attempt also counted anything ending in 00, which turned out to be
    useless: genuine manufacturer MRPs like Rs 89,600 and Rs 215,900 end in 00
    too. That definition scored Reliance Digital at 0.77 against Snapdeal's
    1.00 — barely discriminating, while implying the measure carried weight it
    did not.

    Narrowed to "ends in 99". Even so, this remains the **weakest** of the
    three measures and is deliberately excluded from `verdict`: real retailers
    price at Rs 23,999 all the time. The diversity ratio and top-5
    concentration carry the argument; this is reported for context only.
    """
    return int(round(value)) % 100 == 99


def profile_retailers(
    include_synthetic: bool = False, db_path=None
) -> list[RetailerMRPProfile]:
    """Build an MRP-credibility profile for each retailer."""
    with connect(db_path, readonly=True) as conn:
        rows = conn.execute(PROFILE_SQL, (1 if include_synthetic else 0,)).fetchall()

    by_site: dict[str, list[Any]] = {}
    for row in rows:
        by_site.setdefault(row["site_key"], []).append(row)

    out: list[RetailerMRPProfile] = []
    for site_key, site_rows in by_site.items():
        mrps = [float(r["mrp"]) for r in site_rows]
        discounts = [
            float(r["computed_discount_pct"])
            for r in site_rows if r["computed_discount_pct"] is not None
        ]
        if not mrps:
            continue

        counts = Counter(mrps)
        n = len(mrps)
        top5 = counts.most_common(5)
        top5_share = sum(c for _, c in top5) / n

        out.append(RetailerMRPProfile(
            site_key=site_key,
            n_products=n,
            n_distinct_mrp=len(counts),
            diversity_ratio=round(len(counts) / n, 4),
            top5_concentration=round(top5_share, 4),
            round_number_rate=round(sum(1 for m in mrps if _is_round(m)) / n, 4),
            median_discount_pct=round(statistics.median(discounts), 2) if discounts else 0.0,
            discount_range=(
                (round(min(discounts), 2), round(max(discounts), 2))
                if discounts else (0.0, 0.0)
            ),
            most_repeated=[(v, c) for v, c in counts.most_common(6) if c > 1],
        ))

    out.sort(key=lambda p: p.diversity_ratio)
    return out


def compare(profiles: list[RetailerMRPProfile]) -> dict[str, Any]:
    """Contrast retailer types. The comparison is the finding."""
    if len(profiles) < 2:
        return {
            "n_retailers": len(profiles),
            "note": "comparison needs at least two retailers with MRP data",
            "profiles": [p.__dict__ for p in profiles],
        }

    least, most = profiles[0], profiles[-1]
    return {
        "n_retailers": len(profiles),
        "profiles": [p.__dict__ for p in profiles],
        "least_credible": least.site_key,
        "most_credible": most.site_key,
        "diversity_gap": round(most.diversity_ratio - least.diversity_ratio, 3),
        "discount_gap_pp": round(
            least.median_discount_pct - most.median_discount_pct, 1
        ),
        "finding": (
            f"{least.site_key} advertises a median {least.median_discount_pct:.0f}% "
            f"discount against MRPs with a diversity ratio of {least.diversity_ratio:.2f} "
            f"({least.n_distinct_mrp} distinct values across {least.n_products} products). "
            f"{most.site_key} advertises a median {most.median_discount_pct:.0f}% discount "
            f"against MRPs with a diversity ratio of {most.diversity_ratio:.2f}. "
            "The larger advertised saving sits on the less product-specific set of "
            "reference prices."
        ),
    }


def summarise(profiles: list[RetailerMRPProfile]) -> dict[str, Any]:
    return {
        "retailers_profiled": len(profiles),
        "by_site": {
            p.site_key: {
                "n_products": p.n_products,
                "distinct_mrps": p.n_distinct_mrp,
                "diversity_ratio": p.diversity_ratio,
                "top5_concentration": p.top5_concentration,
                "round_number_rate": p.round_number_rate,
                "median_discount_pct": p.median_discount_pct,
                "verdict": p.verdict,
            }
            for p in profiles
        },
    }
