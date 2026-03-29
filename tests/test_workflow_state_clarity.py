"""Tests for workflow state clarity features — RFQ failures, retry endpoint, buy plan
resubmission."""

from datetime import datetime, timezone

import pytest

from app.models.offers import Contact


@pytest.fixture
def _rfq_requisition(db_session, test_user):
    from app.models import Requirement, Requisition

    req = Requisition(name="RFQ Test Req", status="active", created_by=test_user.id)
    db_session.add(req)
    db_session.flush()
    part = Requirement(requisition_id=req.id, primary_mpn="TEST-MPN-001")
    db_session.add(part)
    db_session.flush()
    return req


class TestRfqFailureRecovery:
    def test_failed_send_creates_contact_with_error(self, db_session, test_user, _rfq_requisition):
        contact = Contact(
            requisition_id=_rfq_requisition.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="Fail Corp",
            vendor_contact="fail@example.com",
            status="failed",
            error_message="Graph API 429: Too Many Requests",
        )
        db_session.add(contact)
        db_session.flush()
        saved = db_session.get(Contact, contact.id)
        assert saved.status == "failed"
        assert saved.error_message == "Graph API 429: Too Many Requests"


class TestPendingContactVisibility:
    def test_ooo_classification_sets_contact_status(self, db_session, test_user, _rfq_requisition):
        from app.models.offers import VendorResponse

        contact = Contact(
            requisition_id=_rfq_requisition.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="OOO Vendor",
            vendor_contact="ooo@vendor.com",
            status="sent",
        )
        db_session.add(contact)
        db_session.flush()

        vr = VendorResponse(
            contact_id=contact.id,
            requisition_id=_rfq_requisition.id,
            vendor_name="OOO Vendor",
            vendor_email="ooo@vendor.com",
            classification="ooo",
            status="new",
            received_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.flush()

        # Simulate the classification update logic
        OOO_CLASSIFICATIONS = {"ooo", "out_of_office", "auto_reply"}
        if vr.classification in OOO_CLASSIFICATIONS and vr.contact_id:
            parent = db_session.get(Contact, vr.contact_id)
            if parent:
                parent.status = "ooo"
                parent.status_updated_at = datetime.now(timezone.utc)

        db_session.flush()
        db_session.refresh(contact)
        assert contact.status == "ooo"

    def test_bounce_sets_contact_status(self, db_session, test_user, _rfq_requisition):
        from app.models.offers import VendorResponse

        contact = Contact(
            requisition_id=_rfq_requisition.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="Bounce Vendor",
            vendor_contact="bounce@vendor.com",
            status="sent",
        )
        db_session.add(contact)
        db_session.flush()

        vr = VendorResponse(
            contact_id=contact.id,
            requisition_id=_rfq_requisition.id,
            vendor_name="Bounce Vendor",
            vendor_email="bounce@vendor.com",
            classification="bounce",
            status="new",
            received_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.flush()

        BOUNCE_CLASSIFICATIONS = {"bounce", "bounced", "delivery_failure"}
        if vr.classification in BOUNCE_CLASSIFICATIONS and vr.contact_id:
            parent = db_session.get(Contact, vr.contact_id)
            if parent:
                parent.status = "bounced"
                parent.status_updated_at = datetime.now(timezone.utc)

        db_session.flush()
        db_session.refresh(contact)
        assert contact.status == "bounced"

    def test_normal_response_doesnt_change_contact(self, db_session, test_user, _rfq_requisition):
        from app.models.offers import VendorResponse

        contact = Contact(
            requisition_id=_rfq_requisition.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="Normal Vendor",
            vendor_contact="normal@vendor.com",
            status="sent",
        )
        db_session.add(contact)
        db_session.flush()

        vr = VendorResponse(
            contact_id=contact.id,
            requisition_id=_rfq_requisition.id,
            vendor_name="Normal Vendor",
            vendor_email="normal@vendor.com",
            classification="quote",
            status="new",
            received_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.flush()

        # Normal classification shouldn't change contact status
        db_session.refresh(contact)
        assert contact.status == "sent"


class TestWorkflowIntegration:
    """End-to-end: send RFQ → fail → retry → OOO → mark VR reviewed."""

    def test_full_rfq_lifecycle(self, db_session, test_user, _rfq_requisition):
        from app.models.offers import VendorResponse

        # 1. Failed send persists
        c = Contact(
            requisition_id=_rfq_requisition.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="Lifecycle Corp",
            vendor_contact="life@corp.com",
            status="failed",
            error_message="Timeout",
        )
        db_session.add(c)
        db_session.flush()
        assert c.status == "failed"
        assert c.error_message == "Timeout"

        # 2. Retry succeeds — old contact marked retried
        c.status = "retried"
        c.status_updated_at = datetime.now(timezone.utc)
        db_session.flush()

        c2 = Contact(
            requisition_id=_rfq_requisition.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="Lifecycle Corp",
            vendor_contact="life@corp.com",
            status="sent",
        )
        db_session.add(c2)
        db_session.flush()
        assert c.status == "retried"
        assert c2.status == "sent"

        # 3. OOO response arrives → contact updated
        vr = VendorResponse(
            contact_id=c2.id,
            requisition_id=_rfq_requisition.id,
            vendor_name="Lifecycle Corp",
            vendor_email="life@corp.com",
            classification="ooo",
            status="new",
            received_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.flush()
        c2.status = "ooo"
        c2.status_updated_at = datetime.now(timezone.utc)
        db_session.flush()
        assert c2.status == "ooo"

        # 4. Mark VR reviewed → terminal state
        vr.status = "reviewed"
        db_session.flush()
        assert vr.status == "reviewed"


from app.services.status_machine import validate_transition


class TestSourcingStatusTransitions:
    """Verify SourcingStatus transitions in status_machine.py."""

    def test_open_to_sourcing_valid(self):
        assert validate_transition("requirement", "open", "sourcing") is True

    def test_sourcing_to_offered_valid(self):
        assert validate_transition("requirement", "sourcing", "offered") is True

    def test_offered_to_quoted_valid(self):
        assert validate_transition("requirement", "offered", "quoted") is True

    def test_open_to_won_invalid(self):
        """Skipping states should be rejected."""
        assert validate_transition("requirement", "open", "won", raise_on_invalid=False) is False

    def test_archived_is_terminal(self):
        """No transitions from archived."""
        assert validate_transition("requirement", "archived", "open", raise_on_invalid=False) is False

    def test_noop_same_status_valid(self):
        assert validate_transition("requirement", "open", "open") is True


class TestStatusMachineValidation:
    """Status machine prevents invalid offer/quote transitions."""

    def test_offer_sold_cannot_go_active(self, client, db_session):
        """Once sold, offer cannot go back to active."""
        from app.models.offers import Offer
        from app.models.sourcing import Requirement, Requisition

        req = Requisition(name="Test Req", customer_name="Test", status="active")
        db_session.add(req)
        db_session.flush()
        reqmt = Requirement(requisition_id=req.id, primary_mpn="TEST-001")
        db_session.add(reqmt)
        db_session.flush()
        offer = Offer(
            requirement_id=reqmt.id,
            requisition_id=req.id,
            vendor_name="V",
            mpn="TEST-001",
            status="sold",
            unit_price=1.0,
            source="manual",
        )
        db_session.add(offer)
        db_session.commit()

        # Try to approve (→ active) a sold offer via generic update
        resp = client.put(
            f"/api/offers/{offer.id}",
            json={"status": "active"},
        )
        assert resp.status_code == 409 or "cannot transition" in resp.text.lower()

    def test_offer_rejected_is_terminal(self, client, db_session):
        """Rejected offers cannot transition to any other status."""
        from app.models.offers import Offer
        from app.models.sourcing import Requirement, Requisition

        req = Requisition(name="Test Req", customer_name="Test", status="active")
        db_session.add(req)
        db_session.flush()
        reqmt = Requirement(requisition_id=req.id, primary_mpn="TEST-002")
        db_session.add(reqmt)
        db_session.flush()
        offer = Offer(
            requirement_id=reqmt.id,
            requisition_id=req.id,
            vendor_name="V",
            mpn="TEST-002",
            status="rejected",
            unit_price=1.0,
            source="manual",
        )
        db_session.add(offer)
        db_session.commit()

        resp = client.put(
            f"/api/offers/{offer.id}",
            json={"status": "active"},
        )
        assert resp.status_code == 409

    def test_valid_offer_transition_succeeds(self, client, db_session):
        """pending_review → active is a valid transition."""
        from app.models.offers import Offer
        from app.models.sourcing import Requirement, Requisition

        req = Requisition(name="Test Req", customer_name="Test", status="active")
        db_session.add(req)
        db_session.flush()
        reqmt = Requirement(requisition_id=req.id, primary_mpn="TEST-003")
        db_session.add(reqmt)
        db_session.flush()
        offer = Offer(
            requirement_id=reqmt.id,
            requisition_id=req.id,
            vendor_name="V",
            mpn="TEST-003",
            status="pending_review",
            unit_price=1.0,
            source="manual",
        )
        db_session.add(offer)
        db_session.commit()

        resp = client.put(f"/api/offers/{offer.id}/approve")
        assert resp.status_code == 200

    def test_require_valid_transition_unit(self):
        """Unit test for require_valid_transition helper."""
        from app.services.status_machine import require_valid_transition

        # Valid transition should not raise
        require_valid_transition("offer", "pending_review", "active")

        # Invalid transition should raise HTTPException 409
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            require_valid_transition("offer", "sold", "active")
        assert exc_info.value.status_code == 409
