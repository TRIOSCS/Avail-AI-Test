"""Tests for workflow state clarity features — RFQ failures, retry endpoint."""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from app.models.offers import Contact


@pytest.fixture
def _rfq_requisition(db_session, test_user):
    from app.models import Requisition, Requirement

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
        assert resp.status_code == 200  # Returns 200 with error body per project convention
        assert "failed" in resp.json()["error"].lower()
