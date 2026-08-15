"""MRP contradiction audit — the day-one finding.

The argument
-----------
In India, MRP (Maximum Retail Price) is a *manufacturer-declared* figure printed
on the package and governed by the Legal Metrology (Packaged Commodities) Rules.
It is a property of the product, not of the shop. Two retailers selling the same
SKU should therefore quote the same MRP.

When they do not, at least one of them is quoting a number that is not the MRP —
and since the struck-through price is what the advertised discount is calculated
against, an inflated MRP manufactures a discount that does not exist.

Why this matters for the study
------------------------------
This analysis needs exactly **one day** of data. It requires no price history at
all, because the contradiction is visible across sellers at a single moment.
It gives the project a real, defensible result in week one, while the
longitudinal inflation analysis accumulates.

What it deliberately does not claim
-----------------------------------
We cannot tell *which* seller is wrong — only that they cannot all be right.
The median is used as the reference point and the language throughout is
"disagrees with the consensus", not "is lying". Genuine causes of variation
(bundled accessories, different pack sizes, regional editions) are mitigated by
the variant-aware match key and a tolerance band, but not eliminated; the manual
validation pass in validation.py estimates how often they explain a flag.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..config import detection_config
from ..db import connect, date_key, finish_analysis_run, start_analysis_run

log = logging.getLogger(__name__)


@dataclass
class MRPContradiction:
    product_id: int
    canonical_title: str
    category: str
    obs_date: str
    n_sellers: int
    quotes: list[tuple[str, float]]          # (site_key, mrp)
    min_mrp: float
    max_mrp: float
    median_mrp: float
    spread_pct: float
    worst_listing_id: int
    worst_site_key: str
    inflation_vs_median_pct: float

    @property
    def headline(self) -> str:
        return (
            f"{self.canonical_title}: {self.n_sellers} sellers quote MRPs from "
            f"₹{self.min_mrp:,.0f} to ₹{self.max_mrp:,.0f} "
            f"({self.spread_pct:.0f}% spread)"
        )


QUOTES_SQL = """
SELECT
    p.product_id,
    p.canonical_title,
    p.category,
    l.listing_id,
    l.site_key,
    o.mrp,
    o.selling_price
FROM fact_price_observation o
JOIN dim_listing l ON l.listing_id = o.listing_id
JOIN dim_product p ON p.product_id = o.product_id
WHERE o.date_key = ?
  AND o.mrp IS NOT NULL
  AND o.mrp > 0
  -- Synthetic fixture rows are excluded unless explicitly requested, so a
  -- development database can never leak into a reported figure.
  AND (o.source = 'live' OR ? = 1)
  -- Only products we matched confidently enough to group across sellers.
  AND p.brand IS NOT NULL
  AND p.model IS NOT NULL
ORDER BY p.product_id, l.site_key
"""


def find_contradictions(
    obs_date: date | str | None = None,
    tolerance_pct: float | None = None,
    min_sellers: int | None = None,
    include_synthetic: bool = False,
    db_path=None,
) -> list[MRPContradiction]:
    """Find products whose claimed MRP disagrees across sellers on one day."""
    cfg = detection_config().get("mrp_dispersion", {})
    tolerance_pct = tolerance_pct if tolerance_pct is not None else float(cfg.get("tolerance_pct", 2.0))
    min_sellers = min_sellers if min_sellers is not None else int(cfg.get("min_sellers", 3))

    if obs_date is None:
        obs_date = date.today()
    if isinstance(obs_date, date):
        obs_date = obs_date.isoformat()

    with connect(db_path, readonly=True) as conn:
        rows = conn.execute(
            QUOTES_SQL, (date_key(obs_date), 1 if include_synthetic else 0)
        ).fetchall()

    # Group quotes by product, keeping one quote per site (the site is the unit
    # of accountability; a marketplace with 4 resellers should not outvote a
    # retailer with one).
    grouped: dict[int, dict[str, Any]] = {}
    for row in rows:
        entry = grouped.setdefault(row["product_id"], {
            "canonical_title": row["canonical_title"],
            "category": row["category"],
            "by_site": {},
        })
        site = row["site_key"]
        if site not in entry["by_site"]:
            entry["by_site"][site] = (row["listing_id"], float(row["mrp"]))

    findings: list[MRPContradiction] = []
    for product_id, entry in grouped.items():
        quotes = [(site, mrp) for site, (_, mrp) in entry["by_site"].items()]
        if len(quotes) < min_sellers:
            continue

        values = [mrp for _, mrp in quotes]
        lo, hi = min(values), max(values)
        median = statistics.median(values)
        if median <= 0:
            continue

        spread_pct = (hi - lo) / median * 100
        if spread_pct <= tolerance_pct:
            continue

        worst_site = max(entry["by_site"].items(), key=lambda kv: kv[1][1])
        worst_listing_id, worst_mrp = worst_site[1]

        findings.append(MRPContradiction(
            product_id=product_id,
            canonical_title=entry["canonical_title"] or f"product {product_id}",
            category=entry["category"],
            obs_date=obs_date,
            n_sellers=len(quotes),
            quotes=sorted(quotes, key=lambda kv: -kv[1]),
            min_mrp=lo,
            max_mrp=hi,
            median_mrp=median,
            spread_pct=round(spread_pct, 2),
            worst_listing_id=worst_listing_id,
            worst_site_key=worst_site[0],
            inflation_vs_median_pct=round((worst_mrp - median) / median * 100, 2),
        ))

    findings.sort(key=lambda f: -f.spread_pct)
    log.info("MRP audit %s: %s products checked, %s contradictions",
             obs_date, len(grouped), len(findings))
    return findings


def run_and_store(
    obs_date: date | str | None = None,
    include_synthetic: bool = False,
    db_path=None,
    notes: str = "",
) -> tuple[int, list[MRPContradiction]]:
    """Run the audit and persist results with the exact parameters used."""
    cfg = detection_config().get("mrp_dispersion", {})
    findings = find_contradictions(
        obs_date=obs_date, include_synthetic=include_synthetic, db_path=db_path
    )

    if obs_date is None:
        obs_date = date.today()
    if isinstance(obs_date, date):
        obs_date = obs_date.isoformat()

    with connect(db_path) as conn:
        n_checked = conn.execute(
            """
            SELECT COUNT(DISTINCT o.product_id) AS n
            FROM fact_price_observation o
            JOIN dim_product p ON p.product_id = o.product_id
            WHERE o.date_key = ? AND o.mrp IS NOT NULL AND p.brand IS NOT NULL
              AND (o.source = 'live' OR ? = 1)
            """,
            (date_key(obs_date), 1 if include_synthetic else 0),
        ).fetchone()["n"]

        run_id = start_analysis_run(conn, "mrp_audit", dict(cfg), notes)
        for f in findings:
            conn.execute(
                """
                INSERT INTO mrp_contradiction (
                    run_id, product_id, date_key, n_sellers, min_mrp, max_mrp,
                    median_mrp, spread_pct, worst_listing_id, worst_site_key,
                    inflation_vs_median_pct
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (run_id, f.product_id, date_key(f.obs_date), f.n_sellers,
                 f.min_mrp, f.max_mrp, f.median_mrp, f.spread_pct,
                 f.worst_listing_id, f.worst_site_key, f.inflation_vs_median_pct),
            )
        finish_analysis_run(conn, run_id, n_checked, len(findings))

    return run_id, findings


def summarise(findings: list[MRPContradiction]) -> dict[str, Any]:
    """Headline numbers for the README and the Power BI cards."""
    if not findings:
        return {"n_contradictions": 0}

    spreads = [f.spread_pct for f in findings]
    by_site: dict[str, int] = {}
    for f in findings:
        by_site[f.worst_site_key] = by_site.get(f.worst_site_key, 0) + 1

    return {
        "n_contradictions": len(findings),
        "median_spread_pct": round(statistics.median(spreads), 2),
        "max_spread_pct": round(max(spreads), 2),
        "mean_overstatement_pct": round(
            statistics.mean(f.inflation_vs_median_pct for f in findings), 2
        ),
        "highest_quote_by_site": dict(sorted(by_site.items(), key=lambda kv: -kv[1])),
        "worst_example": findings[0].headline,
    }
