"""
test_schemas_v13_features.py — Tests for v1.3 feature schemas.

Validates PhoneCallLog and StrategicToggle schemas.

Called by: pytest
Depends on: app.schemas.v13_features
"""

import pytest
from pydantic import ValidationError

from app.schemas.v13_features import (
    PhoneCallLog,
    StrategicToggle,
)

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


# ── ActivityAttributeRequest ─────────────────────────────────────────

class TestActivityAttributeRequest:
    def test_valid(self):
        from app.schemas.v13_features import ActivityAttributeRequest
        a = ActivityAttributeRequest(entity_type="company", entity_id=5)
        assert a.entity_type == "company"
        assert a.entity_id == 5

    def test_vendor_type(self):
        from app.schemas.v13_features import ActivityAttributeRequest
        a = ActivityAttributeRequest(entity_type="vendor", entity_id=10)
        assert a.entity_type == "vendor"

    def test_zero_entity_id_raises(self):
        from app.schemas.v13_features import ActivityAttributeRequest
        with pytest.raises(ValidationError, match="entity_id must be positive"):
            ActivityAttributeRequest(entity_type="company", entity_id=0)

    def test_negative_entity_id_raises(self):
        from app.schemas.v13_features import ActivityAttributeRequest
        with pytest.raises(ValidationError, match="entity_id must be positive"):
            ActivityAttributeRequest(entity_type="company", entity_id=-1)


# ── Other v13 schemas ────────────────────────────────────────────────

class TestOtherV13Schemas:
    def test_company_call_log(self):
        from app.schemas.v13_features import CompanyCallLog
        c = CompanyCallLog()
        assert c.direction == "outbound"

    def test_company_note_log(self):
        from app.schemas.v13_features import CompanyNoteLog
        n = CompanyNoteLog(notes="Test note")
        assert n.notes == "Test note"

    def test_vendor_call_log(self):
        from app.schemas.v13_features import VendorCallLog
        v = VendorCallLog()
        assert v.direction == "outbound"

    def test_vendor_note_log(self):
        from app.schemas.v13_features import VendorNoteLog
        v = VendorNoteLog(notes="Test note")
        assert v.notes == "Test note"

    def test_graph_webhook_payload(self):
        from app.schemas.v13_features import GraphWebhookPayload
        g = GraphWebhookPayload()
        assert g.value == []

    def test_graph_webhook_extra_allowed(self):
        from app.schemas.v13_features import GraphWebhookPayload
        g = GraphWebhookPayload(extra_field="test")
        assert g.extra_field == "test"

    def test_email_click_log(self):
        from app.schemas.v13_features import EmailClickLog
        e = EmailClickLog(email="test@test.com")
        assert e.contact_name is None
