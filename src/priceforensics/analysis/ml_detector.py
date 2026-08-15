"""Learned and statistical anomaly detectors, benchmarked against the rules.

Three detectors run over the same price series, and the point of the module is
the comparison between them:

  1. **Rule-based** (analysis/inflation.py) — encodes the exact pattern we are
     hunting: rise, hold, reverse. High precision by construction; blind to any
     manipulation that doesn't match the template.

  2. **Isolation Forest** (unsupervised) — learns what a *typical* price series
     looks like from 18 engineered shape features and flags the unusual ones.
     Needs no labels, which matters because real data has none.

  3. **Changepoint detection** (ruptures, PELT) — segments each series into
     piecewise-constant regimes and inspects the segment means for an
     up-then-down structure.

The honest finding this is designed to surface
--------------------------------------------
Isolation Forest does **not** detect manufactured discounts. It detects
*unusual series*, and only some unusual series are manufactured discounts. A
product with a genuine volatile price, a launch discount, or a stock-out gap is
equally anomalous to it and equally flagged.

That is not a bug in the implementation — it is what unsupervised anomaly
detection means, and it is the most useful thing in this whole module to be able
to explain. The rule-based detector wins on precision because it was told what
to look for; the learned detector earns its place by surfacing candidates the
rules were never written to catch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .features import FEATURE_NAMES, ListingFeatures, build_dataset

log = logging.getLogger(__name__)


@dataclass
class MLFlag:
    listing_id: int
    site_key: str
    title: str
    score: float                      # higher = more anomalous
    rank: int
    detector: str
    top_contributors: list[tuple[str, float]] = field(default_factory=list)

    @property
    def headline(self) -> str:
        drivers = ", ".join(f"{n}={v:.1f}" for n, v in self.top_contributors[:3])
        return (f"{self.title[:38]} ({self.site_key}) score={self.score:.3f} "
                f"| {drivers}")


@dataclass
class BenchmarkResult:
    detector: str
    n_flagged: int
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float
    recall: float
    f1: float
    note: str = ""

    def row(self) -> list[str]:
        return [self.detector, str(self.n_flagged), str(self.true_positives),
                str(self.false_positives), str(self.false_negatives),
                f"{self.precision:.2f}", f"{self.recall:.2f}", f"{self.f1:.2f}"]


# ---------------------------------------------------------------------------
# 1. Isolation Forest
# ---------------------------------------------------------------------------

def isolation_forest(
    contamination: float = 0.2,
    n_estimators: int = 200,
    random_state: int = 42,
    include_synthetic: bool = False,
    db_path=None,
) -> list[MLFlag]:
    """Unsupervised outlier detection over engineered price-series features.

    `contamination` is the expected proportion of anomalies. It is a *prior*,
    not a discovery: set it to 0.2 and the model will return 20% of listings
    no matter what the data looks like. That property is why the benchmark
    below reports it explicitly rather than tuning it until the numbers improve.
    """
    try:
        import numpy as np
        from sklearn.ensemble import IsolationForest
        from sklearn.preprocessing import StandardScaler
    except ImportError:
        raise RuntimeError(
            "scikit-learn is required. Install with: pip install -e '.[ml]'"
        ) from None

    rows = build_dataset(include_synthetic=include_synthetic, db_path=db_path)
    if len(rows) < 10:
        log.warning("only %s listings with enough history — too few to fit", len(rows))
        return []

    X = np.array([r.vector() for r in rows], dtype=float)

    # Isolation Forest is tree-based and does not strictly need scaling, but
    # scaling makes the per-feature contribution numbers below comparable.
    scaler = StandardScaler()
    Xs = scaler.fit_transform(X)

    model = IsolationForest(
        n_estimators=n_estimators,
        contamination=contamination,
        random_state=random_state,
        n_jobs=-1,
    )
    model.fit(Xs)

    # score_samples: lower = more anomalous. Negate so higher = more anomalous.
    scores = -model.score_samples(Xs)
    predictions = model.predict(Xs)          # -1 anomaly, 1 normal

    flagged: list[MLFlag] = []
    for i, r in enumerate(rows):
        if predictions[i] != -1:
            continue
        # Crude but honest attribution: which standardised features are most
        # extreme for this listing. Not SHAP; described as what it is.
        deviations = sorted(
            ((FEATURE_NAMES[j], float(Xs[i][j])) for j in range(len(FEATURE_NAMES))),
            key=lambda kv: -abs(kv[1]),
        )
        flagged.append(MLFlag(
            listing_id=r.listing_id, site_key=r.site_key, title=r.title,
            score=float(scores[i]), rank=0, detector="isolation_forest",
            top_contributors=[(n, v) for n, v in deviations[:5]],
        ))

    flagged.sort(key=lambda f: -f.score)
    for rank, f in enumerate(flagged, 1):
        f.rank = rank

    log.info("isolation forest: %s/%s listings flagged (contamination=%.2f)",
             len(flagged), len(rows), contamination)
    return flagged


# ---------------------------------------------------------------------------
# 2. Changepoint detection
# ---------------------------------------------------------------------------

def changepoint(
    penalty: float = 3.0,
    min_segment: int = 3,
    min_rise_pct: float = 10.0,
    include_synthetic: bool = False,
    db_path=None,
) -> list[MLFlag]:
    """PELT changepoint segmentation, looking for an up-then-down regime shift.

    Where the rule-based detector asks "did the price rise then fall by more
    than X?", this asks "how many distinct price regimes were there, and what
    shape do their means make?". It finds the segment boundaries itself rather
    than being told the window size, which is the interesting difference.
    """
    try:
        import numpy as np
        import ruptures as rpt
    except ImportError:
        raise RuntimeError(
            "ruptures is required. Install with: pip install -e '.[ml]'"
        ) from None

    from datetime import date as _date

    from ..db import connect

    with connect(db_path, readonly=True) as conn:
        rows = conn.execute(
            """
            SELECT o.listing_id, l.site_key,
                   COALESCE(p.canonical_title, l.raw_title) AS title,
                   d.date AS obs_date, o.selling_price
            FROM fact_price_observation o
            JOIN dim_listing l ON l.listing_id = o.listing_id
            JOIN dim_product p ON p.product_id = o.product_id
            JOIN dim_date    d ON d.date_key   = o.date_key
            WHERE o.selling_price IS NOT NULL AND (? = 1 OR o.source = 'live')
            ORDER BY o.listing_id, d.date
            """,
            (1 if include_synthetic else 0,),
        ).fetchall()

    series: dict[int, dict[str, Any]] = {}
    for row in rows:
        e = series.setdefault(row["listing_id"], {
            "site_key": row["site_key"], "title": row["title"], "prices": []})
        e["prices"].append(float(row["selling_price"]))

    flagged: list[MLFlag] = []
    for listing_id, entry in series.items():
        prices = entry["prices"]
        if len(prices) < min_segment * 3:
            continue

        signal = np.array(prices, dtype=float).reshape(-1, 1)
        try:
            algo = rpt.Pelt(model="l2", min_size=min_segment).fit(signal)
            breaks = algo.predict(pen=penalty * float(np.var(signal)) or 1.0)
        except Exception as exc:
            log.debug("changepoint failed for listing %s: %s", listing_id, exc)
            continue

        bounds = [0] + list(breaks)
        means = []
        for a, b in zip(bounds[:-1], bounds[1:]):
            seg = prices[a:b]
            if seg:
                means.append(sum(seg) / len(seg))
        if len(means) < 3:
            continue

        # Look for a peak that is not at either end: low -> high -> low.
        best = 0.0
        for i in range(1, len(means) - 1):
            before, peak, after = min(means[:i]), means[i], min(means[i + 1:])
            if before <= 0 or peak <= before:
                continue
            rise = (peak - before) / before * 100
            fall = (peak - after) / peak * 100
            if rise >= min_rise_pct and fall > 0:
                best = max(best, min(rise, fall))

        if best > 0:
            flagged.append(MLFlag(
                listing_id=listing_id, site_key=entry["site_key"],
                title=entry["title"], score=best, rank=0,
                detector="changepoint",
                top_contributors=[("n_segments", float(len(means))),
                                  ("rise_fall_pct", best)],
            ))

    flagged.sort(key=lambda f: -f.score)
    for rank, f in enumerate(flagged, 1):
        f.rank = rank

    log.info("changepoint: %s/%s listings flagged", len(flagged), len(series))
    return flagged


# ---------------------------------------------------------------------------
# 3. Benchmark
# ---------------------------------------------------------------------------

def _score(name: str, flagged_ids: set[int], planted: set[int],
           population: set[int], note: str = "") -> BenchmarkResult:
    tp = len(flagged_ids & planted)
    fp = len(flagged_ids - planted)
    fn = len(planted - flagged_ids)
    precision = tp / len(flagged_ids) if flagged_ids else 0.0
    recall = tp / len(planted) if planted else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return BenchmarkResult(name, len(flagged_ids), tp, fp, fn,
                           round(precision, 4), round(recall, 4), round(f1, 4), note)


def benchmark(db_path=None, contamination: float = 0.25) -> dict[str, Any]:
    """Score all three detectors against the planted synthetic ground truth.

    Only meaningful on a synthetic fixture, because it is the only place a
    complete label set exists. On live data, precision comes from manual review
    (analysis/validation.py) and recall is simply not knowable.
    """
    import json
    from pathlib import Path

    from ..config import EXPORT_DIR
    from . import inflation

    truth_path = Path(EXPORT_DIR) / "synthetic_ground_truth.json"
    if not truth_path.exists():
        raise FileNotFoundError(
            f"no ground truth at {truth_path} — run `pf synth generate` first"
        )
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    planted = {p["listing_id"] for p in truth["planted"]}

    population = {r.listing_id for r in build_dataset(include_synthetic=True, db_path=db_path)}

    rule_ids = {e.listing_id for e in inflation.detect(include_backfill=True, db_path=db_path)}
    if_ids = {f.listing_id for f in isolation_forest(
        contamination=contamination, include_synthetic=True, db_path=db_path)}
    cp_ids = {f.listing_id for f in changepoint(include_synthetic=True, db_path=db_path)}

    results = [
        _score("rule_based", rule_ids, planted, population,
               "encodes the target pattern explicitly"),
        _score("isolation_forest", if_ids, planted, population,
               f"unsupervised; contamination={contamination} is a prior, not a discovery"),
        _score("changepoint", cp_ids, planted, population,
               "finds segment boundaries itself; no window size supplied"),
        _score("rules_AND_if", rule_ids & if_ids, planted, population,
               "intersection — highest precision, use for headline claims"),
        _score("rules_OR_if", rule_ids | if_ids, planted, population,
               "union — widest net, use for review queues"),
    ]

    return {
        "n_listings": len(population),
        "n_planted": len(planted),
        "results": [r.__dict__ for r in results],
        "table": [r.row() for r in results],
        "interpretation": (
            "The rule-based detector should win on precision: it was written to "
            "match this exact pattern. Isolation Forest flags unusual series, "
            "which overlaps with but is not the same as manufactured discounts - "
            "its false positives are genuinely volatile products, not bugs. The "
            "intersection is the highest-confidence set."
        ),
    }


def contamination_sensitivity(
    values: list[float] | None = None,
    db_path=None,
) -> dict[str, Any]:
    """How much does Isolation Forest depend on its contamination prior?

    This is the most important result in the module, and it is a negative one.

    `contamination` tells the model what fraction of the data to treat as
    anomalous. It does not discover that fraction - it obeys it. So the model's
    apparent quality is largely a function of how close the analyst's guess was
    to the truth, which on real data is unknowable.

    Sweeping it makes the dependency visible instead of letting a single
    well-chosen value imply a competence the model does not have.
    """
    import json
    from pathlib import Path

    from ..config import EXPORT_DIR

    values = values or [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]

    truth_path = Path(EXPORT_DIR) / "synthetic_ground_truth.json"
    if not truth_path.exists():
        raise FileNotFoundError(
            f"no ground truth at {truth_path} - run `pf synth generate` first"
        )
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    planted = {p["listing_id"] for p in truth["planted"]}
    population = {r.listing_id for r in build_dataset(include_synthetic=True, db_path=db_path)}
    true_rate = len(planted) / len(population) if population else 0.0

    sweep = []
    for c in values:
        ids = {f.listing_id for f in isolation_forest(
            contamination=c, include_synthetic=True, db_path=db_path)}
        res = _score(f"if@{c:.2f}", ids, planted, population)
        sweep.append({
            "contamination": c,
            "n_flagged": res.n_flagged,
            "precision": res.precision,
            "recall": res.recall,
            "f1": res.f1,
        })

    best = max(sweep, key=lambda s: s["f1"])
    worst = min(sweep, key=lambda s: s["f1"])

    return {
        "true_anomaly_rate": round(true_rate, 4),
        "sweep": sweep,
        "f1_range": [worst["f1"], best["f1"]],
        "best_at_contamination": best["contamination"],
        "finding": (
            f"F1 ranges from {worst['f1']:.2f} to {best['f1']:.2f} across the sweep, "
            f"peaking at contamination={best['contamination']:.2f} - almost exactly the "
            f"true anomaly rate of {true_rate:.2f}. The model does not find that rate; "
            "it is told it. On live data the true rate is unknown, so this is a "
            "hyperparameter the analyst must guess, and the guess dominates the result. "
            "That is the case for keeping the rule-based detector as the primary "
            "instrument and using Isolation Forest to widen the review queue."
        ),
    }
