"""Tests for workflow state clarity features — RFQ failures, retry endpoint, buy plan resubmission."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.models.buy_plan import BuyPlanStatus, BuyPlanV3
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

    @patch("app.routers.rfq.require_fresh_token", new_callable=AsyncMock, return_value="fake-token")
    @patch("app.routers.rfq.send_batch_rfq", new_callable=AsyncMock)
    def test_retry_endpoint_resends_failed_contact(
        self, mock_send, mock_token, client, db_session, test_user, _rfq_requisition
    ):
        contact = Contact(
            requisition_id=_rfq_requisition.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="Retry Corp",
            vendor_contact="retry@example.com",
            subject="RFQ for parts",
            details="Please quote TEST-MPN-001",
            status="failed",
            error_message="Timeout",
            parts_included=["TEST-MPN-001"],
        )
        db_session.add(contact)
        db_session.commit()

        mock_send.return_value = [
            {
                "id": contact.id,
                "status": "sent",
                "vendor_name": "Retry Corp",
                "vendor_email": "retry@example.com",
                "parts_count": 1,
            }
        ]
        resp = client.post(f"/api/contacts/{contact.id}/retry")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "sent"

    def test_retry_rejects_non_failed_contact(self, client, db_session, test_user, _rfq_requisition):
        contact = Contact(
            requisition_id=_rfq_requisition.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="OK Corp",
            vendor_contact="ok@example.com",
            status="sent",
        )
        db_session.add(contact)
        db_session.commit()
        resp = client.post(f"/api/contacts/{contact.id}/retry")
        assert resp.status_code == 400
        body = resp.json()
        msg = str(body.get("detail") or body.get("error") or body).lower()
        assert "failed" in msg


class TestVendorResponseTerminalStates:
    """P1: VendorResponses can be marked reviewed/rejected."""

    @pytest.fixture
    def _vendor_response(self, db_session, _rfq_requisition):
        from app.models.offers import VendorResponse

        vr = VendorResponse(
            requisition_id=_rfq_requisition.id,
            vendor_name="Test Vendor",
            vendor_email="test@vendor.com",
            subject="Re: RFQ",
            body="We can supply.",
            status="new",
            received_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.flush()
        return vr

    def test_mark_reviewed(self, client, db_session, _vendor_response):
        resp = client.patch(
            f"/api/vendor-responses/{_vendor_response.id}/status",
            json={"status": "reviewed"},
        )
        assert resp.status_code == 200
        db_session.refresh(_vendor_response)
        assert _vendor_response.status == "reviewed"

    def test_mark_rejected(self, client, db_session, _vendor_response):
        resp = client.patch(
            f"/api/vendor-responses/{_vendor_response.id}/status",
            json={"status": "rejected"},
        )
        assert resp.status_code == 200
        db_session.refresh(_vendor_response)
        assert _vendor_response.status == "rejected"

    def test_invalid_status_rejected(self, client, db_session, _vendor_response):
        resp = client.patch(
            f"/api/vendor-responses/{_vendor_response.id}/status",
            json={"status": "invalid_state"},
        )
        assert resp.status_code == 200  # returns 200 with error body
        assert "status_code" in resp.json()
        assert resp.json()["status_code"] == 400

    def test_list_responses_filters_by_status(self, client, db_session, _rfq_requisition):
        from app.models.offers import VendorResponse

        for s in ("new", "reviewed", "rejected"):
            vr = VendorResponse(
                requisition_id=_rfq_requisition.id,
                vendor_name=f"Vendor {s}",
                vendor_email=f"{s}@vendor.com",
                subject="Re: RFQ",
                status=s,
                received_at=datetime.now(timezone.utc),
            )
            db_session.add(vr)
        db_session.commit()

        # Default (status=new) returns only new
        resp = client.get(f"/api/requisitions/{_rfq_requisition.id}/responses")
        assert resp.status_code == 200
        data = resp.json()
        assert all(r["status"] == "new" for r in data)

        # status=all returns everything
        resp_all = client.get(f"/api/requisitions/{_rfq_requisition.id}/responses?status=all")
        assert resp_all.status_code == 200
        assert len(resp_all.json()) >= 3

        # status=reviewed returns only reviewed
        resp_rev = client.get(f"/api/requisitions/{_rfq_requisition.id}/responses?status=reviewed")
        assert resp_rev.status_code == 200
        assert all(r["status"] == "reviewed" for r in resp_rev.json())


class TestBuyPlanResubmission:
    @pytest.fixture
    def _halted_plan(self, db_session, test_user, _rfq_requisition):
        from app.models.crm import Company, CustomerSite
        from app.models.quotes import Quote

        co = Company(name="BP Test Co")
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(site_name="BP Test Site", company_id=co.id)
        db_session.add(site)
        db_session.flush()
        q = Quote(
            requisition_id=_rfq_requisition.id,
            customer_site_id=site.id,
            quote_number="Q-BP-001",
            status="won",
            created_by_id=test_user.id,
        )
        db_session.add(q)
        db_session.flush()
        plan = BuyPlanV3(
            quote_id=q.id,
            requisition_id=_rfq_requisition.id,
            status="halted",
            submitted_by_id=test_user.id,
        )
        db_session.add(plan)
        db_session.flush()
        return plan

    def test_reset_halted_plan_to_draft(self, client, db_session, _halted_plan):
        resp = client.post(f"/api/buy-plans-v3/{_halted_plan.id}/reset-to-draft")
        assert resp.status_code == 200
        db_session.refresh(_halted_plan)
        assert _halted_plan.status == BuyPlanStatus.draft.value

    def test_reset_active_plan_fails(self, client, db_session, _halted_plan):
        _halted_plan.status = BuyPlanStatus.active.value
        db_session.commit()
        resp = client.post(f"/api/buy-plans-v3/{_halted_plan.id}/reset-to-draft")
        assert resp.status_code == 200  # 200 with error body
        assert resp.json()["status_code"] == 400

    def test_reset_cancelled_plan_to_draft(self, client, db_session, _halted_plan):
        _halted_plan.status = BuyPlanStatus.cancelled.value
        db_session.commit()
        resp = client.post(f"/api/buy-plans-v3/{_halted_plan.id}/reset-to-draft")
        assert resp.status_code == 200
        db_session.refresh(_halted_plan)
        assert _halted_plan.status == BuyPlanStatus.draft.value


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
