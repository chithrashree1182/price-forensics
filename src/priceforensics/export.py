"""Export the warehouse into files Power BI consumes.

Power BI's SQLite ODBC story on Windows is fiddly and needs a driver install,
which makes the report non-portable. Exporting a clean star schema to Parquet
(with CSV fallback) sidesteps that entirely: Power BI reads the folder directly,
refresh is one click, and the model stays a genuine star schema rather than one
flat table.

Each exported table maps 1:1 to a table in the Power BI model. Relationships and
DAX measures are documented in powerbi/README.md.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .config import EXPORT_DIR, ROOT, ensure_dirs
from .db import connect

log = logging.getLogger(__name__)

# Only 'live' rows reach the report by default; synthetic fixtures must never
# appear in a published figure.
EXPORTS: dict[str, str] = {
    "dim_product": """
        SELECT product_id, match_key, brand, model, variant, category,
               canonical_title, first_seen_date
        FROM dim_product
    """,
    "dim_seller": """
        SELECT seller_id, site_key, site_name, seller_name, is_first_party
        FROM dim_seller
    """,
    "dim_listing": """
        SELECT listing_id, product_id, seller_id, site_key, url, raw_title,
               first_seen_date, last_seen_date, is_active
        FROM dim_listing
    """,
    "dim_date": """
        SELECT date_key, date, year, quarter, month, month_name, day,
               day_of_week, day_name, is_weekend, festival_window,
               is_festival, days_to_festival
        FROM dim_date
        WHERE date_key IN (SELECT DISTINCT date_key FROM fact_price_observation)
           OR date BETWEEN date('now', '-120 days') AND date('now', '+60 days')
    """,
    "fact_price_observation": """
        SELECT observation_id, listing_id, date_key, product_id, seller_id,
               selling_price, mrp, discount_pct, computed_discount_pct,
               in_stock, source
        FROM fact_price_observation
        WHERE source = :source_filter OR :include_all = 1
    """,
    "fact_dark_pattern": """
        SELECT pattern_id, listing_id, date_key, pattern_type, raw_text,
               numeric_value
        FROM fact_dark_pattern
    """,
    "inflation_event": """
        SELECT e.event_id, e.run_id, e.listing_id, e.product_id,
               e.baseline_price, e.peak_price, e.sale_price, e.rise_pct,
               e.claimed_discount_pct, e.real_discount_pct,
               e.discount_overstatement_pp, e.rise_start_date, e.sale_start_date,
               e.days_between, e.confidence, e.manually_reviewed,
               e.reviewer_verdict
        FROM inflation_event e
    """,
    "mrp_contradiction": """
        SELECT contradiction_id, run_id, product_id, date_key, n_sellers,
               min_mrp, max_mrp, median_mrp, spread_pct, worst_listing_id,
               worst_site_key, inflation_vs_median_pct
        FROM mrp_contradiction
    """,
    "analysis_run": """
        SELECT run_id, run_type, run_at, params_json, n_input, n_flagged, notes
        FROM analysis_run
    """,
}


def export_all(
    out_dir: Path | None = None,
    fmt: str = "parquet",
    include_synthetic: bool = False,
    db_path=None,
) -> dict[str, Any]:
    """Write every model table to `out_dir`. Returns row counts."""
    ensure_dirs()
    out_dir = Path(out_dir or EXPORT_DIR / "powerbi")
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        import pandas as pd
    except ImportError:
        raise RuntimeError("pandas is required for export; pip install -r requirements.txt")

    counts: dict[str, Any] = {}
    with connect(db_path, readonly=True) as conn:
        for table, sql in EXPORTS.items():
            params: dict[str, Any] = {}
            if ":source_filter" in sql:
                params = {
                    "source_filter": "live",
                    "include_all": 1 if include_synthetic else 0,
                }
            try:
                df = pd.read_sql_query(sql, conn, params=params or None)
            except Exception as exc:
                log.error("export failed for %s: %s", table, exc)
                counts[table] = f"error: {exc}"
                continue

            if fmt == "parquet":
                try:
                    df.to_parquet(out_dir / f"{table}.parquet", index=False)
                except Exception as exc:
                    log.warning("parquet failed for %s (%s); writing CSV", table, exc)
                    df.to_csv(out_dir / f"{table}.csv", index=False)
            else:
                df.to_csv(out_dir / f"{table}.csv", index=False)

            counts[table] = len(df)
            log.info("exported %s rows -> %s", len(df), table)

    if not include_synthetic:
        log.info("synthetic rows excluded from export (use --include-synthetic to override)")

    counts["_output_dir"] = str(out_dir)
    return counts


def export_findings_markdown(out_path: Path | None = None, db_path=None) -> Path:
    """Write a plain-text findings summary for the README.

    Regenerated on every analysis run so the numbers quoted in the README are
    never stale — a README claiming results the database no longer supports is
    the fastest way to lose an interviewer's trust.
    """
    from .analysis import dark_patterns, inflation, mrp_audit, mrp_credibility

    ensure_dirs()
    # Repo root, not data/exports: that directory is gitignored, so a findings
    # file written there could never be committed by the daily workflow and any
    # README link to it would 404 on GitHub.
    out_path = Path(out_path or ROOT / "FINDINGS.md")

    events = inflation.detect(db_path=db_path)
    with connect(db_path, readonly=True) as conn:
        n_listings = conn.execute(
            "SELECT COUNT(DISTINCT listing_id) AS n FROM fact_price_observation"
        ).fetchone()["n"]
        n_obs = conn.execute(
            "SELECT COUNT(*) AS n FROM fact_price_observation"
        ).fetchone()["n"]
        date_range = conn.execute(
            """
            SELECT MIN(d.date) AS lo, MAX(d.date) AS hi
            FROM fact_price_observation o JOIN dim_date d ON d.date_key = o.date_key
            """
        ).fetchone()

    infl = inflation.summarise(events, n_listings)
    scarcity = dark_patterns.summarise(dark_patterns.analyse(db_path=db_path))

    lines = [
        "# Findings",
        "",
        f"_Auto-generated from the warehouse. Observation window: "
        f"{date_range['lo']} to {date_range['hi']} "
        f"({n_obs:,} price observations across {n_listings:,} listings)._",
        "",
        "## Manufactured discounts",
        "",
    ]
    if infl.get("n_events"):
        lines += [
            f"- **{infl['n_events']}** listings show a price rise before a discount",
            f"  ({infl['n_high_confidence']} high confidence, "
            f"{infl['n_medium_confidence']} medium, {infl['n_low_confidence']} low)",
            f"- Median advertised discount: **{infl['median_claimed_discount_pct']}%**",
            f"- Median discount against the pre-rise price: **{infl['median_real_discount_pct']}%**",
            f"- Median overstatement: **{infl['median_overstatement_pp']} percentage points**",
            "",
            f"> {infl['worst_example']}",
            "",
        ]
    else:
        lines += ["_No inflation events detected yet — insufficient price history._", ""]

    # MRP credibility runs on a single day of data, so it is the one section
    # guaranteed to have content from day one. An earlier version of this
    # generator omitted it and silently erased the project's first finding on
    # the first automated regeneration — hence its place here.
    lines += ["## MRP credibility", ""]
    profiles = mrp_credibility.profile_retailers(db_path=db_path)
    if profiles:
        lines += [
            "An MRP is manufacturer-declared, so a genuine catalogue shows roughly",
            "one distinct MRP per product. A menu of stock values reused across",
            "unrelated products is not being read off packaging.",
            "",
            "| Retailer | Products | Distinct MRPs | Diversity | Top-5 conc. | Median disc. | Verdict |",
            "|---|---|---|---|---|---|---|",
        ]
        for p in profiles:
            lines.append(
                f"| {p.site_key} | {p.n_products} | {p.n_distinct_mrp} "
                f"| {p.diversity_ratio:.2f} | {p.top5_concentration:.0%} "
                f"| {p.median_discount_pct:.0f}% | {p.verdict} |"
            )
        for p in profiles:
            repeats = [f"Rs {v:,.0f} x{c}" for v, c in p.most_repeated[:3]]
            if repeats:
                lines.append(f"- {p.site_key} most-repeated MRPs: {', '.join(repeats)}")
        lines += [
            "",
            "_Distribution argument only: this cannot show any individual MRP is",
            "false, and part of any gap may reflect category and seller mix._",
            "",
        ]
    else:
        lines += ["_No MRP data collected yet._", ""]

    lines += ["## Scarcity claims", ""]
    if scarcity.get("n_assessed"):
        lines += [
            f"- **{scarcity['pct_static']}%** of listings with a stock counter "
            f"never changed it (n={scarcity['n_assessed']})",
            f"- Longest unchanged \"only N left\" claim: "
            f"**{scarcity['longest_static_run_days']} consecutive days**",
            "",
        ]
    else:
        lines += ["_Not enough observation days yet._", ""]

    out_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("wrote %s", out_path)
    return out_path
