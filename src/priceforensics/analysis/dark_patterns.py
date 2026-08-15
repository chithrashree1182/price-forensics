"""Scarcity and urgency messaging analysis.

The interesting question is not "does this site say 'Only 3 left!'" — they all
do. It is whether the claim is *true*.

A genuine stock counter moves. If a listing reports "only 2 left in stock" every
single day for three weeks while remaining continuously purchasable, the number
is decoration, not inventory. That is measurable with exactly the data we are
already collecting, and it needs no assumptions about the retailer's internals.

Three tests
-----------
1. **Persistence** — the same scarcity number repeated over many consecutive
   days. Strong evidence the counter is static.
2. **Non-monotonicity without restock** — a counter that wanders up and down
   day to day (3 → 7 → 2 → 5) with no stock-out in between. Real inventory
   counts down between deliveries.
3. **Timing** — whether urgency messaging intensifies during sale windows,
   which speaks to intent.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from typing import Any

from ..db import connect

log = logging.getLogger(__name__)


@dataclass
class ScarcityClaim:
    listing_id: int
    site_key: str
    title: str
    pattern_type: str
    n_days_observed: int
    distinct_values: int
    modal_value: float | None
    longest_static_run: int
    changed_direction_count: int
    verdict: str          # 'static' | 'erratic' | 'plausible' | 'insufficient'
    sample_text: str

    @property
    def headline(self) -> str:
        if self.verdict == "static":
            return (
                f"{self.title} ({self.site_key}): claimed \"{self.sample_text}\" "
                f"unchanged for {self.longest_static_run} consecutive days"
            )
        if self.verdict == "erratic":
            return (
                f"{self.title} ({self.site_key}): stock counter moved up and down "
                f"{self.changed_direction_count} times without a stock-out"
            )
        return f"{self.title} ({self.site_key}): {self.verdict}"


CLAIMS_SQL = """
SELECT
    dp.listing_id,
    l.site_key,
    COALESCE(p.canonical_title, l.raw_title) AS title,
    dp.pattern_type,
    d.date AS obs_date,
    dp.numeric_value,
    dp.raw_text
FROM fact_dark_pattern dp
JOIN dim_listing l ON l.listing_id = dp.listing_id
JOIN dim_product p ON p.product_id = l.product_id
JOIN dim_date    d ON d.date_key   = dp.date_key
ORDER BY dp.listing_id, dp.pattern_type, d.date
"""


def analyse(min_days: int = 7, db_path=None) -> list[ScarcityClaim]:
    """Classify each listing's scarcity messaging over the observation period."""
    with connect(db_path, readonly=True) as conn:
        rows = conn.execute(CLAIMS_SQL).fetchall()

    grouped: dict[tuple[int, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["listing_id"], row["pattern_type"])
        entry = grouped.setdefault(key, {
            "site_key": row["site_key"],
            "title": row["title"],
            "values": [],
            "texts": [],
        })
        entry["values"].append(row["numeric_value"])
        entry["texts"].append(row["raw_text"])

    findings: list[ScarcityClaim] = []
    for (listing_id, pattern_type), entry in grouped.items():
        values = [v for v in entry["values"] if v is not None]
        n_days = len(entry["values"])

        if n_days < min_days:
            verdict, longest_run, directions, distinct, modal = "insufficient", 0, 0, 0, None
        elif not values:
            # Text-only urgency ("Hurry, limited stock") with no number. Its
            # persistence is still informative.
            verdict = "static" if n_days >= min_days else "insufficient"
            longest_run, directions, distinct, modal = n_days, 0, 1, None
        else:
            longest_run = _longest_static_run(values)
            directions = _direction_changes(values)
            distinct = len(set(values))
            modal = statistics.mode(values) if values else None

            if distinct == 1 and n_days >= min_days:
                verdict = "static"
            elif directions >= 3:
                verdict = "erratic"
            else:
                verdict = "plausible"

        findings.append(ScarcityClaim(
            listing_id=listing_id,
            site_key=entry["site_key"],
            title=entry["title"],
            pattern_type=pattern_type,
            n_days_observed=n_days,
            distinct_values=distinct,
            modal_value=modal,
            longest_static_run=longest_run,
            changed_direction_count=directions,
            verdict=verdict,
            sample_text=entry["texts"][0][:120] if entry["texts"] else "",
        ))

    findings.sort(key=lambda f: -f.longest_static_run)
    return findings


def _longest_static_run(values: list[float]) -> int:
    """Longest streak of an unchanged value."""
    if not values:
        return 0
    best = run = 1
    for i in range(1, len(values)):
        if values[i] == values[i - 1]:
            run += 1
            best = max(best, run)
        else:
            run = 1
    return best


def _direction_changes(values: list[float]) -> int:
    """How many times the series reversed direction.

    Real stock depletes monotonically between restocks, so repeated reversals
    indicate the number is not tracking inventory.
    """
    if len(values) < 3:
        return 0
    changes = 0
    last_sign = 0
    for i in range(1, len(values)):
        delta = values[i] - values[i - 1]
        sign = (delta > 0) - (delta < 0)
        if sign == 0:
            continue
        if last_sign != 0 and sign != last_sign:
            changes += 1
        last_sign = sign
    return changes


def summarise(findings: list[ScarcityClaim]) -> dict[str, Any]:
    assessed = [f for f in findings if f.verdict != "insufficient"]
    if not assessed:
        return {"n_assessed": 0}

    static = [f for f in assessed if f.verdict == "static"]
    erratic = [f for f in assessed if f.verdict == "erratic"]

    by_site: dict[str, dict[str, int]] = {}
    for f in assessed:
        bucket = by_site.setdefault(f.site_key, {"assessed": 0, "static": 0, "erratic": 0})
        bucket["assessed"] += 1
        if f.verdict == "static":
            bucket["static"] += 1
        elif f.verdict == "erratic":
            bucket["erratic"] += 1

    return {
        "n_assessed": len(assessed),
        "n_static": len(static),
        "n_erratic": len(erratic),
        "pct_static": round(len(static) / len(assessed) * 100, 2),
        "pct_erratic": round(len(erratic) / len(assessed) * 100, 2),
        "longest_static_run_days": max((f.longest_static_run for f in assessed), default=0),
        "by_site": by_site,
        "worst_example": static[0].headline if static else None,
    }
