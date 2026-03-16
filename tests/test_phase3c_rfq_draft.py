"""test_phase3c_rfq_draft.py — Tests for Phase 3C: Smart RFQ Draft in compose flow.

Verifies: AI draft generation endpoint, draft result template rendering,
RFQ compose form includes draft section, error handling.

Called by: pytest
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Requirement, Requisition, User, VendorCard


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


# ── AI Draft Generation ──────────────────────────────────────────────


class TestAiDraftRfq:
    """Tests for the AI-powered RFQ draft generation endpoint."""

    @patch("app.services.ai_service.draft_rfq", new_callable=AsyncMock)
    def test_draft_generates_body(
        self, mock_draft, client: TestClient, rfq_requisition: Requisition
    ):
        """Should return draft HTML with the generated body."""
        mock_draft.return_value = "Dear Arrow team,\n\nWe are looking for LM317T..."
        resp = client.post(
            f"/v2/partials/requisitions/{rfq_requisition.id}/ai-draft-rfq",
            data={
                "vendor_names": "Arrow Electronics",
                "parts_summary": "LM317T, STM32F407",
            },
        )
        assert resp.status_code == 200
        assert "Draft generated" in resp.text
        assert "Arrow Electronics" in resp.text

    @patch("app.services.ai_service.draft_rfq", new_callable=AsyncMock)
    def test_draft_passes_parts_to_service(
        self, mock_draft, client: TestClient, rfq_requisition: Requisition
    ):
        """Should pass parsed parts list to the AI service."""
        mock_draft.return_value = "Hello"
        client.post(
            f"/v2/partials/requisitions/{rfq_requisition.id}/ai-draft-rfq",
            data={
                "vendor_names": "DigiKey",
                "parts_summary": "LM317T, STM32F407",
            },
        )
        mock_draft.assert_called_once()
        call_kwargs = mock_draft.call_args
        assert "LM317T" in call_kwargs.kwargs.get("parts", call_kwargs[1].get("parts", []))

    @patch("app.services.ai_service.draft_rfq", new_callable=AsyncMock)
    def test_draft_handles_error_gracefully(
        self, mock_draft, client: TestClient, rfq_requisition: Requisition
    ):
        """AI service errors should show fallback message."""
        mock_draft.side_effect = RuntimeError("API timeout")
        resp = client.post(
            f"/v2/partials/requisitions/{rfq_requisition.id}/ai-draft-rfq",
            data={"vendor_names": "Arrow", "parts_summary": "LM317T"},
        )
        assert resp.status_code == 200
        assert "Could not generate" in resp.text

    @patch("app.services.ai_service.draft_rfq", new_callable=AsyncMock)
    def test_draft_handles_none_response(
        self, mock_draft, client: TestClient, rfq_requisition: Requisition
    ):
        """When AI returns None, should show fallback."""
        mock_draft.return_value = None
        resp = client.post(
            f"/v2/partials/requisitions/{rfq_requisition.id}/ai-draft-rfq",
            data={"vendor_names": "Arrow", "parts_summary": "LM317T"},
        )
        assert resp.status_code == 200
        assert "Could not generate" in resp.text

    def test_draft_404_for_missing_requisition(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/99999/ai-draft-rfq",
            data={"vendor_names": "Arrow", "parts_summary": "LM317T"},
        )
        assert resp.status_code == 404

    @patch("app.services.ai_service.draft_rfq", new_callable=AsyncMock)
    def test_draft_includes_js_to_populate_textarea(
        self, mock_draft, client: TestClient, rfq_requisition: Requisition
    ):
        """The draft result should include script to fill the textarea."""
        mock_draft.return_value = "Dear vendor, we need parts."
        resp = client.post(
            f"/v2/partials/requisitions/{rfq_requisition.id}/ai-draft-rfq",
            data={"vendor_names": "Arrow", "parts_summary": "LM317T"},
        )
        assert resp.status_code == 200
        assert "rfq-body-textarea" in resp.text
        assert "Dear vendor, we need parts." in resp.text


# ── RFQ Compose Form Integration ─────────────────────────────────────


class TestRfqComposeIntegration:
    """Tests that the RFQ compose form includes AI draft section."""

    def test_compose_form_has_draft_button(
        self,
        client: TestClient,
        db_session: Session,
        rfq_requisition: Requisition,
    ):
        """RFQ compose page should include the AI draft section."""
        resp = client.get(f"/v2/partials/requisitions/{rfq_requisition.id}/rfq-compose")
        assert resp.status_code == 200
        # The form should render; even if no vendors, the parts summary should be there
        assert "Parts to Quote" in resp.text

    def test_compose_form_has_body_textarea(
        self,
        client: TestClient,
        db_session: Session,
        rfq_requisition: Requisition,
        test_vendor_card: VendorCard,
    ):
        """When vendors exist, compose form should have body textarea and draft button."""
        # Create a sighting linking a requirement to the vendor
        from app.models import Sighting

        reqs = db_session.query(Requirement).filter(
            Requirement.requisition_id == rfq_requisition.id
        ).all()
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
        assert "Generate Draft" in resp.text
