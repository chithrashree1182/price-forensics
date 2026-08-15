"""Tests for product normalisation and price parsing.

These are the highest-value tests in the project. If normalisation is wrong,
two different products get compared and the MRP-contradiction finding is
garbage — and it fails silently, producing plausible-looking numbers. Every
title below was taken from a real listing.
"""

from __future__ import annotations

import pytest

from priceforensics.normalize import (
    extract_brand,
    extract_variant,
    parse_int,
    parse_price,
    parse_title,
)


class TestParsePrice:
    @pytest.mark.parametrize("raw,expected", [
        ("₹1,29,999", 129999.0),          # Indian digit grouping
        ("₹1,29,999.00", 129999.0),
        ("Rs. 45,990", 45990.0),
        ("Rs 4,499", 4499.0),
        ("45990", 45990.0),
        ("₹ 1,299", 1299.0),
        ("MRP ₹59,999", 59999.0),
        ("M.R.P.: ₹79,900", 79900.0),
        ("from ₹12,999", 12999.0),
        ("₹1,299.50", 1299.50),
    ])
    def test_valid_prices(self, raw, expected):
        assert parse_price(raw) == expected

    @pytest.mark.parametrize("raw", [
        None, "", "Currently unavailable", "Coming soon", "₹", "N/A", "--",
    ])
    def test_unparseable_returns_none(self, raw):
        assert parse_price(raw) is None

    @pytest.mark.parametrize("raw", ["₹0", "0.5", "₹99,99,99,999"])
    def test_out_of_range_rejected(self, raw):
        """Implausible values are parse errors, not prices.

        Letting a ₹0 through would silently create a 100% discount in the
        analysis, which is exactly the kind of bug that produces an exciting
        and completely false headline.
        """
        assert parse_price(raw) is None


class TestExtractVariant:
    @pytest.mark.parametrize("title,expected", [
        ("Galaxy S24 5G (Onyx Black, 128 GB) (8 GB RAM)", "8gb-128gb"),
        ("Redmi Note 13 (6GB RAM, 128GB Storage)", "6gb-128gb"),
        ("iPhone 15 128GB", "128gb"),
        ("Vivobook 15 (16GB/1TB)", "16gb-1tb"),
        ("IdeaPad Slim 5 16GB RAM 512GB SSD", "16gb-512gb"),
    ])
    def test_variant_extraction(self, title, expected):
        assert extract_variant(title) == expected

    def test_no_capacity_returns_none(self):
        assert extract_variant("Sony WH-1000XM5 Wireless Headphones") is None

    def test_smaller_capacity_treated_as_ram(self):
        """Two bare capacities: the smaller is RAM, the larger is storage."""
        assert extract_variant("Phone 8 256") is None      # no unit -> no guess
        assert extract_variant("Phone 8GB 256GB") == "8gb-256gb"


class TestExtractBrand:
    @pytest.mark.parametrize("text,expected", [
        ("samsung galaxy s24", "samsung"),
        ("oneplus nord ce4", "oneplus"),
        ("apple iphone 15", "apple"),
        ("iphone 15 pro", "apple"),           # iPhone implies Apple
        ("boat rockerz 550", "boat"),
    ])
    def test_known_brands(self, text, expected):
        assert extract_brand(text) == expected

    def test_unknown_brand(self):
        assert extract_brand("generic wireless earbuds") is None

    def test_longer_brand_wins_at_same_position(self):
        """'oneplus' must not be shadowed by a shorter partial match."""
        assert extract_brand("oneplus 12r 5g") == "oneplus"

    def test_earliest_brand_wins_over_feature_copy(self):
        """Regression: a real listing scraped on 2026-08-15.

        "Sony ULT WEAR Headphones WH-ULT900N With Massive Bass, Noise
        Cancelling" contains both 'sony' and 'noise' (both real brands). The
        old longest-match rule picked Noise - the wrong company - which
        corrupts the match key and silently breaks cross-seller comparison.
        """
        title = ("sony ult wear headphones wh-ult900n with massive bass, "
                 "noise cancelling")
        assert extract_brand(title) == "sony"

    @pytest.mark.parametrize("title,expected", [
        ("boat rockerz 550 with noise isolation", "boat"),
        ("realme buds air 5 pro active noise cancellation", "realme"),
        ("apple airpods pro 2 with active noise cancellation", "apple"),
        ("jbl tune 770nc adaptive noise cancelling headphones", "jbl"),
    ])
    def test_noise_in_feature_copy_never_wins(self, title, expected):
        assert extract_brand(title) == expected


class TestCrossSellerMatching:
    """The core requirement: differently-titled listings of one SKU must agree."""

    def test_same_phone_three_retailers(self):
        titles = [
            "SAMSUNG Galaxy S24 5G (Onyx Black, 128 GB)  (8 GB RAM)",
            "Samsung Galaxy S24 5G (8GB RAM, 128GB, Onyx Black)",
            "Samsung Galaxy S24 5G AI Smartphone (Marble Grey, 8GB, 128GB Storage)",
        ]
        keys = {parse_title(t, "mobiles").match_key for t in titles}
        assert len(keys) == 1, f"expected one match key, got {keys}"

    def test_colour_does_not_split_products(self):
        """Colour variants share an MRP, so they must not fragment the group."""
        a = parse_title("Galaxy M35 (Onyx Black, 128 GB) (6 GB RAM)", "mobiles")
        b = parse_title("Galaxy M35 (Moonlight Silver, 128 GB) (6 GB RAM)", "mobiles")
        assert a.match_key == b.match_key

    def test_storage_does_split_products(self):
        """Different storage genuinely prices differently — must not merge."""
        a = parse_title("iPhone 15 (Blue, 128 GB)", "mobiles")
        b = parse_title("iPhone 15 (Blue, 256 GB)", "mobiles")
        assert a.match_key != b.match_key

    def test_confidence_flag(self):
        confident = parse_title("Samsung Galaxy S24 5G (128 GB)", "mobiles")
        assert confident.is_confident

        vague = parse_title("Wireless Earbuds with Charging Case", "headphones")
        assert not vague.is_confident


class TestParseInt:
    @pytest.mark.parametrize("raw,expected", [
        ("1,234 ratings", 1234),
        ("57% off", 57),
        ("only 3 left", 3),
        (None, None),
        ("no digits here", None),
    ])
    def test_parse_int(self, raw, expected):
        assert parse_int(raw) == expected
