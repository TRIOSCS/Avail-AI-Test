"""tests/test_sighting_ingest.py — Tests for app/services/sighting_ingest.py.

Covers sighting_from_row: ORM-object construction from market-result dicts.

Called by: pytest
Depends on: app.services.sighting_ingest, app.models.sourcing.Sighting
"""

import os

os.environ["TESTING"] = "1"

from app.services.sighting_ingest import sighting_from_row


class TestSightingFromRow:
    def test_basic_mapping(self):
        item = {
            "vendor_name": "Arrow",
            "mpn_matched": "LM317T",
            "manufacturer": "TI",
            "qty_available": 500,
            "unit_price": 0.45,
            "currency": "USD",
            "source_type": "broker",
            "is_authorized": True,
            "confidence": 90,
            "score": 85,
        }
        sighting = sighting_from_row(1, item)
        assert sighting.requirement_id == 1
        assert sighting.vendor_name == "Arrow"
        assert sighting.mpn_matched == "LM317T"
        assert sighting.manufacturer == "TI"
        assert sighting.qty_available == 500
        assert sighting.unit_price == 0.45
        assert sighting.currency == "USD"
        assert sighting.source_type == "broker"
        assert sighting.is_authorized is True
        assert sighting.confidence == 90
        assert sighting.score == 85

    def test_defaults_applied_when_keys_missing(self):
        sighting = sighting_from_row(42, {})
        assert sighting.requirement_id == 42
        assert sighting.vendor_name == "Unknown"
        assert sighting.currency == "USD"
        assert sighting.is_authorized is False
        assert sighting.confidence == 0
        assert sighting.score == 0
        assert sighting.mpn_matched is None
        assert sighting.manufacturer is None

    def test_mpn_falls_back_to_mpn_key(self):
        """When mpn_matched is absent, falls back to 'mpn' key."""
        item = {"mpn": "ABC123"}
        sighting = sighting_from_row(1, item)
        assert sighting.mpn_matched == "ABC123"

    def test_mpn_matched_takes_priority_over_mpn(self):
        item = {"mpn_matched": "MATCHED", "mpn": "FALLBACK"}
        sighting = sighting_from_row(1, item)
        assert sighting.mpn_matched == "MATCHED"

    def test_optional_fields_populated(self):
        item = {
            "evidence_tier": "T1",
            "moq": 100,
            "lead_time": "8 weeks",
            "condition": "new",
            "date_code": "2312",
            "packaging": "tape/reel",
            "vendor_email": "sales@vendor.com",
            "vendor_phone": "+14155551234",
        }
        sighting = sighting_from_row(1, item)
        assert sighting.evidence_tier == "T1"
        assert sighting.moq == 100
        assert sighting.lead_time == "8 weeks"
        assert sighting.condition == "new"
        assert sighting.date_code == "2312"
        assert sighting.packaging == "tape/reel"
        assert sighting.vendor_email == "sales@vendor.com"
        assert sighting.vendor_phone == "+14155551234"

    def test_raw_data_built_from_url_fields(self):
        item = {
            "vendor_url": "https://arrow.com/part",
            "click_url": "https://arrow.com/click",
            "octopart_url": "https://octopart.com/part",
            "vendor_sku": "ARROW-LM317",
        }
        sighting = sighting_from_row(1, item)
        assert sighting.raw_data["vendor_url"] == "https://arrow.com/part"
        assert sighting.raw_data["click_url"] == "https://arrow.com/click"
        assert sighting.raw_data["octopart_url"] == "https://octopart.com/part"
        assert sighting.raw_data["vendor_sku"] == "ARROW-LM317"

    def test_raw_data_none_when_url_fields_absent(self):
        sighting = sighting_from_row(1, {})
        assert sighting.raw_data == {
            "vendor_url": None,
            "click_url": None,
            "octopart_url": None,
            "vendor_sku": None,
        }

    def test_different_requirement_ids(self):
        s1 = sighting_from_row(10, {"vendor_name": "V1"})
        s2 = sighting_from_row(20, {"vendor_name": "V2"})
        assert s1.requirement_id == 10
        assert s2.requirement_id == 20
        assert s1.vendor_name == "V1"
        assert s2.vendor_name == "V2"

    def test_non_usd_currency_preserved(self):
        sighting = sighting_from_row(1, {"currency": "EUR"})
        assert sighting.currency == "EUR"
