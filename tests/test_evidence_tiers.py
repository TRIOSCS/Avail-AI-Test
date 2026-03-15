"""test_evidence_tiers.py — Tests for app/evidence_tiers.py.

Covers:
- tier_for_sighting: maps source_type + is_authorized → T1–T7
- tier_for_parsed_offer: maps confidence → T4 or T5
"""

from app.evidence_tiers import tier_for_parsed_offer, tier_for_sighting


class TestTierForSighting:
    def test_authorized_always_t1(self):
        assert tier_for_sighting("digikey", True) == "T1"
        assert tier_for_sighting("brokerbin", True) == "T1"
        assert tier_for_sighting(None, True) == "T1"

    def test_authorized_api_sources_t2(self):
        for src in ("digikey", "mouser", "element14", "nexar", "octopart", "brokerbin", "sourcengine"):
            assert tier_for_sighting(src, False) == "T2", f"{src} should be T2"

    def test_marketplace_sources_t3(self):
        for src in ("ebay", "oemsecrets", "ics", "ics_scrape"):
            assert tier_for_sighting(src, False) == "T3", f"{src} should be T3"

    def test_email_sources_t5(self):
        for src in ("email_parse", "email_auto_import", "email"):
            assert tier_for_sighting(src, False) == "T5", f"{src} should be T5"

    def test_manual_t6(self):
        assert tier_for_sighting("manual", False) == "T6"
        assert tier_for_sighting("", False) == "T6"

    def test_history_sources_t7(self):
        for src in ("material_history", "stock_list", "excess_list"):
            assert tier_for_sighting(src, False) == "T7", f"{src} should be T7"

    def test_unknown_source_defaults_t3(self):
        assert tier_for_sighting("some_new_connector", False) == "T3"

    def test_none_source_not_authorized_t6(self):
        assert tier_for_sighting(None, False) == "T6"


class TestTierForParsedOffer:
    def test_high_confidence_t5(self):
        assert tier_for_parsed_offer(0.9) == "T5"
        assert tier_for_parsed_offer(0.8) == "T5"
        assert tier_for_parsed_offer(1.0) == "T5"

    def test_medium_confidence_t4(self):
        assert tier_for_parsed_offer(0.7) == "T4"
        assert tier_for_parsed_offer(0.5) == "T4"
        assert tier_for_parsed_offer(0.0) == "T4"

    def test_none_confidence_t4(self):
        assert tier_for_parsed_offer(None) == "T4"
