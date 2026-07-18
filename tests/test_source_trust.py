"""tests/test_source_trust.py — Tests for app/source_trust.py.

Covers: source_reliability_base(), evidence_tier_bonus(), and the T1..T7 trust
ordering (T6 manual buyer entry ranked above T3 marketplace scrape).
"""

import pytest

from app.source_trust import (
    EVIDENCE_TIER_BONUS,
    SOURCE_RELIABILITY_BASE,
    SOURCE_RELIABILITY_DEFAULT,
    VENDOR_RELIABILITY_KNOWN_NO_SCORE,
    VENDOR_RELIABILITY_UNKNOWN,
    evidence_tier_bonus,
    source_reliability_base,
)


class TestSourceReliabilityBase:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("digikey", 90.0),
            ("DIGIKEY", 90.0),  # case-insensitive
            ("brokerbin", 80.0),  # T2 API connector: above scraped marketplaces, below authorized APIs
            ("sourcengine", 80.0),
            ("netcomponents", 72.0),
            ("ai", 40.0),
            ("unknown_connector", SOURCE_RELIABILITY_DEFAULT),
            (None, SOURCE_RELIABILITY_DEFAULT),
            ("", SOURCE_RELIABILITY_DEFAULT),
        ],
    )
    def test_source_reliability_base(self, source, expected):
        assert source_reliability_base(source) == expected


class TestEvidenceTierBonus:
    @pytest.mark.parametrize(
        ("tier", "expected"),
        [
            ("T1", 8.0),
            ("t1", 8.0),  # case-insensitive
            ("T2", 5.0),
            ("T6", 3.0),
            ("T3", 2.0),
            ("T4", 0.0),
            ("T5", -5.0),
            ("T7", -15.0),
            (None, 0.0),
            ("unknown_tier", 0.0),
        ],
    )
    def test_evidence_tier_bonus(self, tier, expected):
        assert evidence_tier_bonus(tier) == expected


class TestTrustOrdering:
    """The explicit ordering requirement: T1 > T2 > T6 > T3 > T4/T5 > T7."""

    def test_full_ordering(self):
        assert EVIDENCE_TIER_BONUS["T1"] > EVIDENCE_TIER_BONUS["T2"]
        assert EVIDENCE_TIER_BONUS["T2"] > EVIDENCE_TIER_BONUS["T6"]
        assert EVIDENCE_TIER_BONUS["T6"] > EVIDENCE_TIER_BONUS["T3"]
        assert EVIDENCE_TIER_BONUS["T3"] > EVIDENCE_TIER_BONUS["T4"]
        assert EVIDENCE_TIER_BONUS["T4"] >= EVIDENCE_TIER_BONUS["T5"]
        assert EVIDENCE_TIER_BONUS["T5"] > EVIDENCE_TIER_BONUS["T7"]

    def test_manual_beats_scrape_at_equal_base_reliability(self):
        """When the underlying source_type gives the same base reliability (both fall to
        the default bucket), a T6 manual entry must score higher than a T3 marketplace
        scrape — the bug this module fixes."""
        manual = source_reliability_base("manual") + evidence_tier_bonus("T6")
        scrape = source_reliability_base("ebay") + evidence_tier_bonus("T3")
        assert manual > scrape


class TestVendorReliabilityFallbacks:
    def test_unknown_lower_than_known_no_score(self):
        assert VENDOR_RELIABILITY_UNKNOWN < VENDOR_RELIABILITY_KNOWN_NO_SCORE

    def test_values_in_range(self):
        assert 0.0 <= VENDOR_RELIABILITY_UNKNOWN <= 100.0
        assert 0.0 <= VENDOR_RELIABILITY_KNOWN_NO_SCORE <= 100.0


class TestSourceReliabilityBaseTable:
    def test_all_values_in_range(self):
        for value in SOURCE_RELIABILITY_BASE.values():
            assert 0.0 <= value <= 100.0
