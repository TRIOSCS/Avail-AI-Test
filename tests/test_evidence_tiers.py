"""test_evidence_tiers.py — Tests for app/evidence_tiers.py.

Covers:
- tier_for_sighting: maps source_type + is_authorized → T1–T7
- tier_for_parsed_offer: maps confidence → T4 or T5
"""

import pytest

from app.evidence_tiers import tier_for_parsed_offer, tier_for_sighting


class TestTierForSighting:
    @pytest.mark.parametrize(
        ("source", "authorized", "expected"),
        [
            # Authorized always wins → T1, regardless of source.
            ("digikey", True, "T1"),
            ("brokerbin", True, "T1"),
            (None, True, "T1"),
            # Authorized API sources (not authorized flag) → T2.
            ("digikey", False, "T2"),
            ("mouser", False, "T2"),
            ("element14", False, "T2"),
            ("nexar", False, "T2"),
            ("octopart", False, "T2"),
            ("brokerbin", False, "T2"),
            ("sourcengine", False, "T2"),
            # Marketplace sources → T3.
            ("ebay", False, "T3"),
            ("oemsecrets", False, "T3"),
            ("ics", False, "T3"),
            ("ics_scrape", False, "T3"),
            # Email sources → T5.
            ("email_parse", False, "T5"),
            ("email_auto_import", False, "T5"),
            ("email", False, "T5"),
            # Manual / empty → T6.
            ("manual", False, "T6"),
            ("", False, "T6"),
            # History sources → T7.
            ("material_history", False, "T7"),
            ("stock_list", False, "T7"),
            ("excess_list", False, "T7"),
            # Unknown source defaults to T3.
            ("some_new_connector", False, "T3"),
            # None source, not authorized → T6.
            (None, False, "T6"),
        ],
    )
    def test_tier_for_sighting(self, source, authorized, expected):
        assert tier_for_sighting(source, authorized) == expected


class TestTierForParsedOffer:
    @pytest.mark.parametrize(
        ("confidence", "expected"),
        [
            # High confidence (>= 0.8) → T5.
            (0.9, "T5"),
            (0.8, "T5"),
            (1.0, "T5"),
            # Medium / low confidence → T4.
            (0.7, "T4"),
            (0.5, "T4"),
            (0.0, "T4"),
            # None confidence → T4.
            (None, "T4"),
        ],
    )
    def test_tier_for_parsed_offer(self, confidence, expected):
        assert tier_for_parsed_offer(confidence) == expected
