"""test_phase3c_rfq_draft.py — Tests for RFQ email cleanup in compose flow.

Verifies: AI cleanup endpoint, compose form layout, error handling.

Called by: pytest
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Requirement, Requisition, Sighting, User, VendorCard

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def rfq_requisition(db_session: Session, test_user: User) -> Requisition:
    """A requisition with parts for RFQ drafting."""
    req = Requisition(
        name="REQ-RFQ-001",
        customer_name="Acme Corp",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    for mpn in ["LM317T", "STM32F407", "TPS54331"]:
        r = Requirement(
            requisition_id=req.id,
            primary_mpn=mpn,
            target_qty=1000,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(r)

    db_session.commit()
    db_session.refresh(req)
    return req


# ── AI Email Cleanup ─────────────────────────────────────────────────


class TestAiCleanupEmail:
    """Tests for the AI-powered email cleanup endpoint."""

    @patch("app.utils.claude_client.claude_text", new_callable=AsyncMock)
    def test_cleanup_returns_cleaned_text(self, mock_claude, client: TestClient, rfq_requisition: Requisition):
        """Should return JS that updates the textarea with cleaned text."""
        mock_claude.return_value = "Dear Arrow team,\n\nWe are looking for LM317T in quantity."
        resp = client.post(
            f"/v2/partials/requisitions/{rfq_requisition.id}/ai-cleanup-email",
            data={"body": "hey arrow, we need lm317t parts pls"},
        )
        assert resp.status_code == 200
        assert "rfq-body-textarea" in resp.text
        assert "cleaned up" in resp.text.lower()

    @patch("app.utils.claude_client.claude_text", new_callable=AsyncMock)
    def test_cleanup_handles_error_gracefully(self, mock_claude, client: TestClient, rfq_requisition: Requisition):
        """AI errors should return the original text unchanged."""
        mock_claude.side_effect = RuntimeError("API timeout")
        resp = client.post(
            f"/v2/partials/requisitions/{rfq_requisition.id}/ai-cleanup-email",
            data={"body": "hey we need parts"},
        )
        assert resp.status_code == 200
        assert "hey we need parts" in resp.text

    def test_cleanup_empty_body_shows_warning(self, client: TestClient, rfq_requisition: Requisition):
        """Empty body should show a warning message."""
        resp = client.post(
            f"/v2/partials/requisitions/{rfq_requisition.id}/ai-cleanup-email",
            data={"body": ""},
        )
        assert resp.status_code == 200
        assert "Write your email first" in resp.text

    def test_cleanup_404_for_missing_requisition(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/99999/ai-cleanup-email",
            data={"body": "test email"},
        )
        assert resp.status_code == 404


# ── RFQ Compose Form Integration ─────────────────────────────────────


class TestRfqComposeIntegration:
    """Tests that the RFQ compose form includes cleanup section."""

    def test_compose_form_has_parts_summary(self, client: TestClient, rfq_requisition: Requisition):
        resp = client.get(f"/v2/partials/requisitions/{rfq_requisition.id}/rfq-compose")
        assert resp.status_code == 200
        assert "Parts to Quote" in resp.text

    def test_compose_form_has_cleanup_button(
        self,
        client: TestClient,
        db_session: Session,
        rfq_requisition: Requisition,
        test_vendor_card: VendorCard,
    ):
        """When vendors exist, compose form should have body textarea and Clean Up
        button."""
        reqs = db_session.query(Requirement).filter(Requirement.requisition_id == rfq_requisition.id).all()
        if reqs:
            s = Sighting(
                requirement_id=reqs[0].id,
                vendor_name="Arrow Electronics",
                vendor_name_normalized="arrow electronics",
                mpn_matched=reqs[0].primary_mpn,
                source_type="brokerbin",
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(s)
            db_session.commit()

        resp = client.get(f"/v2/partials/requisitions/{rfq_requisition.id}/rfq-compose")
        assert resp.status_code == 200
        assert "rfq-body-textarea" in resp.text
        assert "Clean Up" in resp.text
