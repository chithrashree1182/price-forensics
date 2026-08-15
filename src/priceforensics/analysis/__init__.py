"""Analysis modules.

Each module answers one question and stores its output with the exact parameters
used, so results are reproducible and the thresholds are auditable:

    mrp_audit     — do sellers agree on a product's MRP? (needs 3+ retailers)
    mrp_credibility — do a retailer's MRPs behave like real ones? (needs 1 day)
    inflation     — was the "before" price raised before the sale? (needs weeks)
    dark_patterns — are scarcity claims real? (needs ~2 weeks)
    validation    — how often is the detector right? (needs a human)
    features      — price series -> fixed-length feature vectors
    ml_detector   — Isolation Forest + changepoint, benchmarked against the rules
"""

from . import (dark_patterns, features, inflation, ml_detector, mrp_audit,
               mrp_credibility, validation)

__all__ = ["mrp_audit", "inflation", "dark_patterns", "validation",
           "features", "ml_detector", "mrp_credibility"]
