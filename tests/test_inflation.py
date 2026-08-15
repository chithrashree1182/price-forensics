"""Tests for inflation detection.

Built around hand-constructed price series where the correct answer is known by
construction. The negative cases matter more than the positive ones: a detector
that flags everything would look impressive and be worthless, so most of these
tests assert that ordinary retail behaviour is *not* flagged.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from priceforensics.analysis.inflation import (
    PricePoint,
    _analyse_drop,
    _confidence,
    _find_price_drops,
)

START = date(2026, 6, 1)


def series(prices: list[float], start: date = START) -> list[PricePoint]:
    """Build a daily price series from a list of prices."""
    return [PricePoint(obs_date=start + timedelta(days=i), price=p)
            for i, p in enumerate(prices)]


def analyse(prices: list[float], drop_idx: int | None = None, **kw):
    points = series(prices)
    if drop_idx is None:
        drops = _find_price_drops(points, kw.get("min_rise_pct", 10.0))
        if not drops:
            return None
        drop_idx = drops[-1]
    return _analyse_drop(
        points, drop_idx,
        lookback_days=kw.get("lookback_days", 21),
        min_rise_pct=kw.get("min_rise_pct", 10.0),
        min_hold_days=kw.get("min_hold_days", 3),
    )


class TestFindPriceDrops:
    def test_detects_sharp_drop(self):
        points = series([1000] * 5 + [800] * 3)
        assert _find_price_drops(points, min_drop_pct=10.0) == [5]

    def test_ignores_minor_wobble(self):
        points = series([1000, 995, 1002, 998, 1001])
        assert _find_price_drops(points, min_drop_pct=10.0) == []

    def test_finds_multiple_drops(self):
        points = series([1000] * 3 + [850] * 3 + [700] * 3)
        assert _find_price_drops(points, min_drop_pct=10.0) == [3, 6]


class TestManufacturedDiscount:
    """The pattern the project exists to detect."""

    def test_classic_pre_sale_inflation(self):
        # ₹10,000 for 10 days -> raised to ₹13,000 for 7 days -> "sale" at ₹9,500
        result = analyse([10_000] * 10 + [13_000] * 7 + [9_500])
        assert result is not None
        assert result["baseline"] == pytest.approx(10_000)
        assert result["peak"] == pytest.approx(13_000)
        assert result["rise_pct"] == pytest.approx(30.0)
        # Advertised as 27% off ₹13,000; actually 5% off the real price.
        assert result["claimed"] == pytest.approx(26.92, abs=0.1)
        assert result["real"] == pytest.approx(5.0, abs=0.1)
        assert result["overstatement"] == pytest.approx(21.92, abs=0.2)

    def test_overstatement_is_claimed_minus_real(self):
        result = analyse([5_000] * 8 + [6_000] * 5 + [4_800])
        assert result["overstatement"] == pytest.approx(
            result["claimed"] - result["real"], abs=0.01
        )

    def test_tolerates_missing_days(self):
        """Scrapers miss days. A gap must not break detection."""
        points = [PricePoint(START + timedelta(days=i), 10_000) for i in range(0, 10, 2)]
        points += [PricePoint(START + timedelta(days=i), 13_000) for i in range(10, 18, 2)]
        points += [PricePoint(START + timedelta(days=18), 9_500)]
        result = _analyse_drop(points, len(points) - 1, lookback_days=21,
                               min_rise_pct=10.0, min_hold_days=3)
        assert result is not None
        assert result["baseline"] == pytest.approx(10_000)


class TestNegativeCases:
    """Ordinary retail behaviour that must NOT be flagged."""

    def test_honest_discount_not_flagged(self):
        """A straight cut with no prior rise is a real sale."""
        assert analyse([10_000] * 15 + [8_000]) is None

    def test_brief_price_spike_not_flagged(self):
        """A one-day blip is a glitch or a stock-out, not a campaign."""
        assert analyse([10_000] * 12 + [13_000] + [9_500], min_hold_days=3) is None

    def test_small_rise_below_threshold_not_flagged(self):
        """A 5% rise is within normal repricing noise."""
        assert analyse([10_000] * 10 + [10_500] * 5 + [9_500], min_rise_pct=10.0) is None

    def test_no_visible_baseline_not_flagged(self):
        """If the elevated price runs to the start of the window we cannot tell
        inflation from a product that was simply always priced there."""
        assert analyse([13_000] * 15 + [9_500]) is None

    def test_insufficient_history_not_flagged(self):
        assert analyse([13_000, 13_000, 9_500]) is None

    def test_gradual_decline_not_flagged(self):
        """Electronics depreciate steadily; that is not a manufactured discount."""
        prices = [10_000 - i * 40 for i in range(20)]
        assert analyse(prices) is None


class TestConfidence:
    def _result(self, **over):
        base = {"rise_pct": 30.0, "hold_days": 8, "overstatement": 20.0,
                "sale": 9_500, "baseline": 10_000}
        return {**base, **over}

    def test_strong_case_is_high(self):
        assert _confidence(self._result(), n_obs=40) == "high"

    def test_weak_case_is_low(self):
        weak = self._result(rise_pct=11.0, hold_days=3, overstatement=5.0,
                            sale=11_000, baseline=10_000)
        assert _confidence(weak, n_obs=10) == "low"

    def test_confidence_is_ordered(self):
        strong = _confidence(self._result(), n_obs=40)
        weak = _confidence(
            self._result(rise_pct=11.0, hold_days=3, overstatement=4.0,
                         sale=11_500, baseline=10_000),
            n_obs=10,
        )
        assert (strong, weak) == ("high", "low")
