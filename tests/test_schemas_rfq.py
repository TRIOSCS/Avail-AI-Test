"""
test_schemas_rfq.py — Tests for app/schemas/rfq.py

Validates PhoneCallLog, BatchRfqSend, RfqPrepare, FollowUpEmail
schemas including required fields, defaults, and blank rejection.

Called by: pytest
Depends on: app/schemas/rfq.py
"""

import pytest
from pydantic import ValidationError

from app.schemas.rfq import BatchRfqSend, FollowUpEmail, PhoneCallLog, RfqPrepare

# ── PhoneCallLog ─────────────────────────────────────────────────────

class TestPhoneCallLog:
    def test_valid_minimal(self):
        p = PhoneCallLog(requisition_id=1, vendor_name="Acme", vendor_phone="555-1234")
        assert p.requisition_id == 1
        assert p.parts == []

    def test_valid_with_parts(self):
        p = PhoneCallLog(requisition_id=1, vendor_name="Acme", vendor_phone="555-1234",
                         parts=["LM358", "NE555"])
        assert len(p.parts) == 2

    def test_missing_vendor_name_raises(self):
        with pytest.raises(ValidationError):
            PhoneCallLog(requisition_id=1, vendor_phone="555-1234")

    def test_blank_vendor_name_raises(self):
        with pytest.raises(ValidationError, match="must not be blank"):
            PhoneCallLog(requisition_id=1, vendor_name="  ", vendor_phone="555-1234")

    def test_blank_vendor_phone_raises(self):
        with pytest.raises(ValidationError, match="must not be blank"):
            PhoneCallLog(requisition_id=1, vendor_name="Acme", vendor_phone="")

    def test_strips_whitespace(self):
        p = PhoneCallLog(requisition_id=1, vendor_name="  Acme Corp  ", vendor_phone=" 555 ")
        assert p.vendor_name == "Acme Corp"
        assert p.vendor_phone == "555"


# ── BatchRfqSend ─────────────────────────────────────────────────────

class TestBatchRfqSend:
    def test_empty_groups_default(self):
        b = BatchRfqSend()
        assert b.groups == []

    def test_valid_groups(self):
        b = BatchRfqSend(groups=[
            {"vendor_name": "Acme", "vendor_email": "a@acme.com", "parts": ["LM358"]},
        ])
        assert len(b.groups) == 1
        assert b.groups[0].vendor_name == "Acme"

    def test_invalid_group_missing_email(self):
        with pytest.raises(ValidationError):
            BatchRfqSend(groups=[{"vendor_name": "Acme"}])


# ── RfqPrepare ───────────────────────────────────────────────────────

class TestRfqPrepare:
    def test_empty_vendors_default(self):
        r = RfqPrepare()
        assert r.vendors == []

    def test_with_vendors(self):
        r = RfqPrepare(vendors=[{"vendor_name": "Acme"}, {"vendor_name": "Globex"}])
        assert len(r.vendors) == 2


# ── FollowUpEmail ────────────────────────────────────────────────────

class TestFollowUpEmail:
    def test_empty_body_default(self):
        f = FollowUpEmail()
        assert f.body == ""

    def test_with_body(self):
        f = FollowUpEmail(body="Following up on our RFQ")
        assert "Following up" in f.body
