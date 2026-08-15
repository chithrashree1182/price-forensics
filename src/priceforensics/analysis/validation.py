"""Manual validation — turning "my detector flagged 340 products" into a number
you can defend.

Why this module exists
----------------------
An automated flag is a hypothesis, not a finding. The first question anyone
serious will ask is: *how do you know those are real, and not artefacts of your
threshold?* The only honest answer is to hand-check a random sample and report
the precision, with a confidence interval.

Workflow
--------
    pf validate sample --run 3 --n 100     # draw a random sample -> CSV
    ...reviewer fills in the verdict column by opening each product's
       archived snapshots and price chart...
    pf validate load reviewed.csv          # write verdicts back
    pf validate report --run 3             # precision + Wilson 95% CI

Sampling is random and seeded, and the seed is recorded, so the same sample can
be reproduced. Reviewing only the most extreme flags would inflate precision;
that temptation is why the sampler does not let you sort.
"""

from __future__ import annotations

import csv
import logging
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import EXPORT_DIR, ensure_dirs
from ..db import connect

log = logging.getLogger(__name__)

VERDICTS = ("confirmed", "rejected", "unclear")


@dataclass
class PrecisionEstimate:
    n_reviewed: int
    n_confirmed: int
    n_rejected: int
    n_unclear: int
    precision: float
    ci_low: float
    ci_high: float
    method: str = "Wilson score interval, 95%"

    @property
    def headline(self) -> str:
        return (
            f"Manual review of {self.n_reviewed} randomly sampled flags: "
            f"{self.precision * 100:.0f}% confirmed "
            f"(95% CI {self.ci_low * 100:.0f}–{self.ci_high * 100:.0f}%)"
        )


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Used instead of the normal approximation because sample sizes here are small
    (50–150) and precision is often near 1.0, where the normal interval both
    misbehaves and can exceed 1.
    """
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


SAMPLE_SQL = """
SELECT
    e.event_id,
    e.listing_id,
    l.site_key,
    l.url,
    COALESCE(p.canonical_title, l.raw_title) AS title,
    e.baseline_price,
    e.peak_price,
    e.sale_price,
    e.rise_pct,
    e.claimed_discount_pct,
    e.real_discount_pct,
    e.discount_overstatement_pp,
    e.rise_start_date,
    e.sale_start_date,
    e.confidence
FROM inflation_event e
JOIN dim_listing l ON l.listing_id = e.listing_id
JOIN dim_product p ON p.product_id = e.product_id
WHERE e.run_id = ?
"""


def draw_sample(
    run_id: int,
    n: int = 100,
    seed: int = 20260816,
    out_path: Path | None = None,
    db_path=None,
) -> Path:
    """Draw a reproducible random sample of flags for manual review."""
    ensure_dirs()
    with connect(db_path, readonly=True) as conn:
        rows = [dict(r) for r in conn.execute(SAMPLE_SQL, (run_id,)).fetchall()]

    if not rows:
        raise ValueError(f"no inflation events found for run {run_id}")

    rng = random.Random(seed)
    sample = rng.sample(rows, min(n, len(rows)))
    # Shuffle again so the reviewer does not see them grouped by site or
    # severity — order effects are real in manual labelling.
    rng.shuffle(sample)

    out_path = out_path or (EXPORT_DIR / f"validation_sample_run{run_id}.csv")
    fieldnames = list(sample[0].keys()) + ["verdict", "reviewer_note"]

    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in sample:
            row["verdict"] = ""          # reviewer fills: confirmed/rejected/unclear
            row["reviewer_note"] = ""
            writer.writerow(row)

    log.info("wrote %s rows to %s (seed=%s, population=%s)",
             len(sample), out_path, seed, len(rows))
    return out_path


def load_verdicts(csv_path: Path, db_path=None) -> int:
    """Write reviewer verdicts back into inflation_event."""
    updated = 0
    with Path(csv_path).open(newline="", encoding="utf-8") as fh, connect(db_path) as conn:
        for row in csv.DictReader(fh):
            verdict = (row.get("verdict") or "").strip().lower()
            if verdict not in VERDICTS:
                continue
            conn.execute(
                """
                UPDATE inflation_event
                SET manually_reviewed = 1, reviewer_verdict = ?, reviewer_note = ?
                WHERE event_id = ?
                """,
                (verdict, (row.get("reviewer_note") or "").strip(), int(row["event_id"])),
            )
            updated += 1
    log.info("loaded %s verdicts from %s", updated, csv_path)
    return updated


def precision_report(run_id: int, db_path=None) -> PrecisionEstimate:
    """Compute precision and its confidence interval from reviewed flags.

    'unclear' verdicts are excluded from the denominator and reported separately
    — counting them as either successes or failures would bias the estimate, and
    hiding them would overstate how clean the labelling was.
    """
    with connect(db_path, readonly=True) as conn:
        rows = conn.execute(
            """
            SELECT reviewer_verdict, COUNT(*) AS n
            FROM inflation_event
            WHERE run_id = ? AND manually_reviewed = 1
            GROUP BY reviewer_verdict
            """,
            (run_id,),
        ).fetchall()

    counts = {r["reviewer_verdict"]: r["n"] for r in rows}
    confirmed = counts.get("confirmed", 0)
    rejected = counts.get("rejected", 0)
    unclear = counts.get("unclear", 0)
    decisive = confirmed + rejected

    precision = confirmed / decisive if decisive else 0.0
    lo, hi = wilson_interval(confirmed, decisive)

    return PrecisionEstimate(
        n_reviewed=decisive + unclear,
        n_confirmed=confirmed,
        n_rejected=rejected,
        n_unclear=unclear,
        precision=round(precision, 4),
        ci_low=round(lo, 4),
        ci_high=round(hi, 4),
    )


def required_sample_size(margin: float = 0.10, p_expected: float = 0.85, z: float = 1.96) -> int:
    """How many flags must be reviewed for a given margin of error.

    At p≈0.85 a ±10pp margin needs ~49 reviews and ±5pp needs ~196 — which is
    the honest reason the study reviews ~100 and reports a ±7pp interval rather
    than claiming a precise figure.
    """
    n = (z**2 * p_expected * (1 - p_expected)) / margin**2
    return math.ceil(n)
