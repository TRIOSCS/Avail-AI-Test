"""
test_schemas_v13_features.py — Tests for v1.3 feature schemas.

Validates BuyerProfileUpsert, PhoneCallLog, StrategicToggle,
and RoutingPairRequest schemas.

Called by: pytest
Depends on: app.schemas.v13_features
"""

import pytest
from pydantic import ValidationError

from app.schemas.v13_features import (
    BuyerProfileUpsert,
    PhoneCallLog,
    RoutingPairRequest,
    StrategicToggle,
)


# ── BuyerProfileUpsert ──────────────────────────────────────────────

class TestBuyerProfileUpsert:
    def test_all_optional(self):
        p = BuyerProfileUpsert()
        assert p.primary_commodity is None
        assert p.brand_specialties is None

    def test_full_profile(self):
        p = BuyerProfileUpsert(
            primary_commodity="Semiconductors",
            secondary_commodity="Passives",
            primary_geography="Asia",
            brand_specialties=["TI", "Analog Devices"],
            brand_material_types=["IC"],
            brand_usage_types=["Military"],
        )
        assert p.primary_commodity == "Semiconductors"
        assert p.brand_specialties == ["TI", "Analog Devices"]

    def test_exclude_unset_only_sends_provided(self):
        p = BuyerProfileUpsert(primary_commodity="Connectors")
        dump = p.model_dump(exclude_unset=True)
        assert dump == {"primary_commodity": "Connectors"}

    def test_string_brand_specialties_passed_through(self):
        """Service layer handles str→list conversion, schema just passes it."""
        p = BuyerProfileUpsert(brand_specialties="TI,Analog Devices")
        assert p.brand_specialties == "TI,Analog Devices"


# ── PhoneCallLog ────────────────────────────────────────────────────

class TestPhoneCallLog:
    def test_defaults(self):
        p = PhoneCallLog()
        assert p.phone == ""
        assert p.direction == "outbound"
        assert p.duration_seconds is None

    def test_inbound_call(self):
        p = PhoneCallLog(
            phone="+1-555-1234",
            direction="inbound",
            duration_seconds=120,
            contact_name="Jane Doe",
        )
        assert p.direction == "inbound"
        assert p.duration_seconds == 120

    def test_invalid_direction_rejected(self):
        with pytest.raises(ValidationError, match="direction"):
            PhoneCallLog(direction="missed")


# ── StrategicToggle ─────────────────────────────────────────────────

class TestStrategicToggle:
    def test_default_none_means_flip(self):
        s = StrategicToggle()
        assert s.is_strategic is None

    def test_explicit_true(self):
        s = StrategicToggle(is_strategic=True)
        assert s.is_strategic is True

    def test_empty_body_accepted(self):
        """Endpoint uses default param, so empty body = StrategicToggle()."""
        s = StrategicToggle()
        assert s.is_strategic is None


# ── RoutingPairRequest ──────────────────────────────────────────────

class TestRoutingPairRequest:
    def test_valid(self):
        r = RoutingPairRequest(requirement_id=42, vendor_card_id=7)
        assert r.requirement_id == 42
        assert r.vendor_card_id == 7

    def test_missing_fields_rejected(self):
        with pytest.raises(ValidationError):
            RoutingPairRequest()

    def test_zero_rejected(self):
        with pytest.raises(ValidationError, match="positive"):
            RoutingPairRequest(requirement_id=0, vendor_card_id=5)

    def test_negative_rejected(self):
        with pytest.raises(ValidationError, match="positive"):
            RoutingPairRequest(requirement_id=-1, vendor_card_id=5)
