"""Tests for price-series feature engineering.

Features feed an unsupervised model, so there are no labels to catch a mistake
downstream — a wrong feature just produces confident nonsense. These tests pin
the properties the model depends on, especially **scale invariance**: without
it, Isolation Forest would mostly learn that laptops cost more than earphones.
"""

from __future__ import annotations

import pytest

from priceforensics.analysis.features import FEATURE_NAMES, extract


def feats(prices):
    return extract([float(p) for p in prices])


class TestScaleInvariance:
    """The single most important property. Shape must matter, level must not."""

    @pytest.mark.parametrize("multiplier", [0.01, 10, 1000])
    def test_features_survive_rescaling(self, multiplier):
        cheap = [1000] * 10 + [1300] * 7 + [950] * 5
        pricey = [p * multiplier for p in cheap]

        a, b = feats(cheap), feats(pricey)
        # n_observations is a count, not a shape feature, so it is exempt.
        for name in FEATURE_NAMES:
            if name == "n_observations":
                continue
            assert a[name] == pytest.approx(b[name], rel=1e-6), (
                f"{name} changed under rescaling by {multiplier}"
            )


class TestRiseThenFall:
    """The signature feature for a manufactured discount."""

    def test_inflation_pattern_scores_high(self):
        f = feats([1000] * 10 + [1300] * 7 + [950] * 5)
        assert f["max_rise_21d_pct"] > 25
        assert f["max_drop_after_rise_pct"] > 25
        assert f["rise_then_fall_score"] > 25

    def test_flat_series_scores_zero(self):
        f = feats([1000] * 25)
        assert f["rise_then_fall_score"] == 0
        assert f["coef_variation"] == 0

    def test_monotonic_decline_scores_low(self):
        """Electronics depreciate. That must not look like manipulation."""
        f = feats([1000 - i * 15 for i in range(25)])
        assert f["rise_then_fall_score"] == 0
        assert f["trend_slope_norm"] < 0

    def test_honest_discount_scores_low(self):
        """A straight cut with no prior rise."""
        f = feats([1000] * 18 + [800] * 6)
        assert f["rise_then_fall_score"] == 0

    def test_rise_without_fall_scores_low(self):
        """A genuine price increase that is never reversed."""
        f = feats([1000] * 12 + [1300] * 12)
        assert f["rise_then_fall_score"] == 0


class TestStructuralFeatures:
    def test_price_levels_normalised(self):
        flat = feats([1000] * 20)
        noisy = feats([1000 + (i % 7) * 50 for i in range(20)])
        assert flat["n_price_levels_norm"] < noisy["n_price_levels_norm"]

    def test_volatility_ordering(self):
        stable = feats([1000, 1005, 998, 1002] * 5)
        volatile = feats([1000, 1400, 700, 1250] * 5)
        assert volatile["coef_variation"] > stable["coef_variation"]

    def test_fraction_at_extremes(self):
        f = feats([1000] * 5 + [1500] * 15)
        assert f["frac_days_at_max"] == pytest.approx(0.75, abs=0.01)
        assert f["frac_days_at_min"] == pytest.approx(0.25, abs=0.01)

    def test_autocorrelation_distinguishes_sticky_from_noisy(self):
        sticky = feats([1000] * 10 + [1200] * 10)
        alternating = feats([1000, 1200] * 10)
        assert sticky["lag1_autocorr"] > alternating["lag1_autocorr"]


class TestRobustness:
    @pytest.mark.parametrize("series", [[], [100], [100, 200]])
    def test_short_series_returns_zeros_not_errors(self, series):
        f = feats(series)
        assert set(f) == set(FEATURE_NAMES)
        assert all(v == 0.0 for v in f.values())

    def test_all_features_present_and_finite(self):
        import math
        f = feats([1000] * 10 + [1300] * 7 + [950] * 5)
        assert set(f) == set(FEATURE_NAMES)
        assert all(math.isfinite(v) for v in f.values())

    def test_zero_prices_do_not_divide_by_zero(self):
        """Bad parses can produce zeros; the extractor must not explode."""
        f = feats([0, 0, 1000, 1000, 1200])
        assert all(v == v for v in f.values())   # no NaN
