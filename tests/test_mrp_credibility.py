"""Tests for MRP credibility profiling.

The measure makes a claim about a retailer's whole catalogue, so the tests pin
both directions: a genuine catalogue must come out clean, and a menu of stock
values must come out flagged. Getting the first wrong would be worse than
getting the second wrong — a false accusation is more damaging than a miss.
"""

from __future__ import annotations

import pytest

from priceforensics.analysis.mrp_credibility import (
    RetailerMRPProfile,
    _is_round,
    compare,
)


def profile(site, mrps, discounts=None, **kw):
    from collections import Counter
    import statistics

    counts = Counter(mrps)
    n = len(mrps)
    discounts = discounts or [50.0] * n
    top5 = sum(c for _, c in counts.most_common(5))
    return RetailerMRPProfile(
        site_key=site,
        n_products=n,
        n_distinct_mrp=len(counts),
        diversity_ratio=round(len(counts) / n, 4),
        top5_concentration=round(top5 / n, 4),
        round_number_rate=round(sum(1 for m in mrps if _is_round(m)) / n, 4),
        median_discount_pct=statistics.median(discounts),
        discount_range=(min(discounts), max(discounts)),
        most_repeated=[(v, c) for v, c in counts.most_common(6) if c > 1],
        **kw,
    )


class TestIsRound:
    @pytest.mark.parametrize("v", [999, 1499, 1999, 23999, 99])
    def test_psychological_endings(self, v):
        assert _is_round(v)

    @pytest.mark.parametrize("v", [89600, 215900, 23456, 1234, 2000, 1000])
    def test_other_endings(self, v):
        """Regression: an earlier definition also counted anything ending in
        00, which flagged genuine MRPs like Rs 89,600 and Rs 215,900. That made
        the measure near-useless — Reliance scored 0.77 against Snapdeal's 1.00.
        """
        assert not _is_round(v)

    def test_excluded_from_verdict(self):
        """The measure is context only. A catalogue of diverse MRPs that all
        happen to end in 99 must still read as credible."""
        p = profile("all99", [x * 1000 + 999 for x in range(1, 31)])
        assert p.round_number_rate == 1.0
        assert p.verdict == "consistent with product-specific pricing"


class TestVerdict:
    def test_stock_menu_is_flagged(self):
        """Observed Snapdeal shape: many products, a handful of round values."""
        mrps = [999] * 14 + [1499] * 13 + [1999] * 4 + [2999] * 3 + [1299] * 4
        p = profile("marketplace", mrps)
        assert p.diversity_ratio < 0.35
        assert p.top5_concentration > 0.5
        assert p.verdict == "inconsistent with product-specific pricing"

    def test_genuine_catalogue_is_clean(self):
        """Observed Reliance shape: nearly one distinct MRP per product.

        This is the important direction. A measure that flags a real catalogue
        would produce a false accusation, which is far worse than a miss.
        """
        mrps = [89600, 215900, 23999, 59999, 32999, 49900, 54900, 119600,
                99600, 79900, 26999, 18999, 20999, 27999, 33999, 78999,
                82999, 71999, 34990, 4499, 12999, 3999]
        p = profile("first_party", mrps)
        assert p.diversity_ratio > 0.9
        assert p.verdict == "consistent with product-specific pricing"

    def test_small_sample_is_not_judged(self):
        """Under 20 products, the distribution argument has no force."""
        p = profile("tiny", [999] * 5)
        assert p.verdict == "insufficient"

    def test_borderline_is_not_over_claimed(self):
        """Moderate concentration gets a softer verdict, not the strong one."""
        mrps = [999] * 6 + [1499] * 5 + list(range(2000, 2011))
        p = profile("mixed", mrps)
        assert p.verdict in ("unusually concentrated",
                            "consistent with product-specific pricing")
        assert p.verdict != "inconsistent with product-specific pricing"


class TestCompare:
    def test_orders_by_diversity_and_reports_gap(self):
        low = profile("marketplace", [999] * 20 + [1499] * 20, [80.0] * 40)
        high = profile("first_party", list(range(10000, 10040)), [10.0] * 40)
        result = compare([low, high])
        assert result["least_credible"] == "marketplace"
        assert result["most_credible"] == "first_party"
        assert result["discount_gap_pp"] == pytest.approx(70.0, abs=0.1)
        assert "reference prices" in result["finding"]

    def test_single_retailer_declines_to_compare(self):
        result = compare([profile("only", [999] * 30)])
        assert result["n_retailers"] == 1
        assert "at least two retailers" in result["note"]

    def test_no_retailers(self):
        assert compare([])["n_retailers"] == 0
