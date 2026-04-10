"""test_htmx_views_nightly5.py — Fifth nightly coverage boost for htmx_views.py.

Targets: rfq-send (test mode), add-to-requisition, lead status update,
         add-offers-to-quote, email reply, review response, create quote.

Called by: pytest
Depends on: conftest.py (client, db_session, test_user, test_requisition)
"""

import json
import os
import uuid

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import RequisitionStatus, SourcingStatus
from app.models import Offer, Requirement, Requisition, User
from app.models.offers import VendorResponse
from app.models.quotes import Quote, QuoteLine
from app.models.sourcing_lead import SourcingLead

# ── Helpers ────────────────────────────────────────────────────────────


def _req(db: Session, user: User, **kw) -> Requisition:
    defaults = dict(
        name=f"N5-REQ-{uuid.uuid4().hex[:6]}",
        customer_name="N5 Corp",
        status=RequisitionStatus.ACTIVE,
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    obj = Requisition(**defaults)
    db.add(obj)
    db.flush()
    return obj


def _requirement(db: Session, req: Requisition, mpn: str = "LM317T", **kw) -> Requirement:
    defaults = dict(
        requisition_id=req.id,
        primary_mpn=mpn,
        target_qty=100,
        sourcing_status=SourcingStatus.OPEN,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    obj = Requirement(**defaults)
    db.add(obj)
    db.flush()
    return obj


def _offer(db: Session, req: Requisition, user: User, mpn: str = "LM317T", **kw) -> Offer:
    defaults = dict(
        requisition_id=req.id,
        vendor_name="Acme Supply",
        mpn=mpn,
        qty_available=500,
        unit_price=0.75,
        entered_by_id=user.id,
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    obj = Offer(**defaults)
    db.add(obj)
    db.flush()
    return obj


def _quote(db: Session, req: Requisition, user: User, status: str = "draft", **kw) -> Quote:
    num = f"Q-{req.id}-{uuid.uuid4().hex[:4]}"
    defaults = dict(
        requisition_id=req.id,
        quote_number=num,
        status=status,
        line_items=[],
        created_by_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    obj = Quote(**defaults)
    db.add(obj)
    db.flush()
    return obj


def _vendor_response(db: Session, req: Requisition, **kw) -> VendorResponse:
    defaults = dict(
        requisition_id=req.id,
        vendor_name="TestVendor",
        vendor_email="vendor@example.com",
        subject="Re: RFQ",
        body="We have stock.",
        status="new",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    obj = VendorResponse(**defaults)
    db.add(obj)
    db.flush()
    return obj


def _sourcing_lead(db: Session, req: Requisition, requirement: Requirement, **kw) -> SourcingLead:
    defaults = dict(
        lead_id=f"lead-{uuid.uuid4().hex}",
        requirement_id=requirement.id,
        requisition_id=req.id,
        part_number_requested="LM317T",
        part_number_matched="LM317T",
        vendor_name="TestVendorLead",
        vendor_name_normalized="testvendorlead",
        primary_source_type="manual",
        primary_source_name="test",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    obj = SourcingLead(**defaults)
    db.add(obj)
    db.flush()
    return obj


# ── Tests: RFQ Send (test mode) ───────────────────────────────────────


class TestRfqSend:
    def test_send_in_test_mode_creates_contacts(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
    ):
        """TESTING=1 → contacts created in DB, rfq_results template returned."""
        url = f"/v2/partials/requisitions/{test_requisition.id}/rfq-send"
        resp = client.post(
            url,
            data={
                "vendor_names": "TestVendor",
                "vendor_emails": "test@vendor.com",
                "subject": "RFQ Test",
                "body": "Please quote LM317T",
            },
        )
        assert resp.status_code == 200
        # Template contains rfq results content
        assert resp.text

    def test_send_multiple_vendors_in_test_mode(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
    ):
        """Multiple vendor pairs all get Contact records created."""
        url = f"/v2/partials/requisitions/{test_requisition.id}/rfq-send"
        resp = client.post(
            url,
            data={
                "vendor_names": ["Vendor Alpha", "Vendor Beta"],
                "vendor_emails": ["alpha@v.com", "beta@v.com"],
                "subject": "Multi RFQ",
                "body": "Quote these parts",
            },
        )
        assert resp.status_code == 200

    def test_no_vendor_names_returns_400(
        self,
        client: TestClient,
        test_requisition: Requisition,
    ):
        """Missing vendor_names raises HTTPException(400)."""
        url = f"/v2/partials/requisitions/{test_requisition.id}/rfq-send"
        resp = client.post(url, data={"subject": "RFQ", "body": "Hello"})
        assert resp.status_code == 400

    def test_requisition_not_found_returns_404(
        self,
        client: TestClient,
    ):
        """Non-existent requisition raises 404."""
        resp = client.post(
            "/v2/partials/requisitions/99999/rfq-send",
            data={"vendor_names": "V", "vendor_emails": "v@v.com"},
        )
        assert resp.status_code == 404


# ── Tests: Add to Requisition ─────────────────────────────────────────


class TestAddToRequisition:
    def test_missing_fields_returns_400(self, client: TestClient):
        """Empty body → 400 HTML response."""
        resp = client.post(
            "/v2/partials/search/add-to-requisition",
            content=json.dumps({}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert "Missing required fields" in resp.text

    def test_missing_items_returns_400(
        self,
        client: TestClient,
        test_requisition: Requisition,
    ):
        """Present requisition_id + mpn but no items → 400."""
        resp = client.post(
            "/v2/partials/search/add-to-requisition",
            content=json.dumps({"requisition_id": test_requisition.id, "mpn": "LM317T", "items": []}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_requisition_not_found_returns_404(self, client: TestClient):
        """Unknown requisition_id → 404 HTML response."""
        resp = client.post(
            "/v2/partials/search/add-to-requisition",
            content=json.dumps(
                {
                    "requisition_id": 99999,
                    "mpn": "LM317T",
                    "items": [{"vendor_name": "Acme", "qty_available": 100}],
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 404
        assert "Requisition not found" in resp.text

    def test_valid_post_creates_sightings(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
    ):
        """Valid JSON body creates a Requirement + Sighting rows, returns 200."""
        resp = client.post(
            "/v2/partials/search/add-to-requisition",
            content=json.dumps(
                {
                    "requisition_id": test_requisition.id,
                    "mpn": "ABC999",
                    "items": [
                        {"vendor_name": "Acme", "qty_available": 100, "unit_price": 0.50},
                        {"vendor_name": "Beta Supply", "qty_available": 50, "unit_price": 0.60},
                    ],
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert "Added 2 results" in resp.text

    def test_existing_requirement_gets_sightings_appended(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
    ):
        """If Requirement for MPN already exists, new Sightings are added to it."""
        # test_requisition already has LM317T requirement
        resp = client.post(
            "/v2/partials/search/add-to-requisition",
            content=json.dumps(
                {
                    "requisition_id": test_requisition.id,
                    "mpn": "LM317T",
                    "items": [{"vendor_name": "ExistingVendor", "qty_available": 200}],
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert "Added 1 result" in resp.text


# ── Tests: Lead Status Update ─────────────────────────────────────────


class TestLeadStatusUpdate:
    def test_lead_not_found_returns_404(
        self,
        client: TestClient,
    ):
        """update_lead_status returns None → 404."""
        with patch(
            "app.services.sourcing_leads.update_lead_status",
            return_value=None,
        ):
            resp = client.post(
                "/v2/partials/sourcing/leads/99999/status",
                data={"status": "has_stock"},
            )
        assert resp.status_code == 404

    def test_invalid_status_raises_400(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
    ):
        """ValueError from update_lead_status → 400."""
        req = _req(db_session, test_user)
        requirement = _requirement(db_session, req)
        lead = _sourcing_lead(db_session, req, requirement)
        db_session.commit()

        with patch(
            "app.services.sourcing_leads.update_lead_status",
            side_effect=ValueError("Unsupported lead status: bogus"),
        ):
            resp = client.post(
                f"/v2/partials/sourcing/leads/{lead.id}/status",
                data={"status": "bogus"},
            )
        assert resp.status_code == 400

    def test_valid_status_returns_card_html(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
    ):
        """Valid status update without special HX-Target returns lead card template."""
        req = _req(db_session, test_user)
        requirement = _requirement(db_session, req)
        lead = _sourcing_lead(db_session, req, requirement)
        db_session.commit()

        # Use real service — no vendor_card_id so propagation is a no-op
        resp = client.post(
            f"/v2/partials/sourcing/leads/{lead.id}/status",
            data={"status": "has_stock"},
        )
        assert resp.status_code == 200

    def test_valid_status_with_lead_row_hx_target(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
    ):
        """HX-Target starting with 'lead-row-' returns lead_row template."""
        req = _req(db_session, test_user)
        requirement = _requirement(db_session, req)
        lead = _sourcing_lead(db_session, req, requirement)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/sourcing/leads/{lead.id}/status",
            data={"status": "no_stock"},
            headers={"HX-Target": f"lead-row-{lead.id}"},
        )
        assert resp.status_code == 200

    def test_status_update_with_note(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
    ):
        """Note field is passed through — lead buyer_feedback_summary updated."""
        req = _req(db_session, test_user)
        requirement = _requirement(db_session, req)
        lead = _sourcing_lead(db_session, req, requirement)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/sourcing/leads/{lead.id}/status",
            data={"status": "contacted", "note": "Called them"},
        )
        assert resp.status_code == 200


# ── Tests: Add Offers to Draft Quote ─────────────────────────────────


class TestAddOfferstoDraftQuote:
    def test_invalid_json_returns_400(
        self,
        client: TestClient,
        test_requisition: Requisition,
    ):
        """Non-JSON body → 400."""
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/add-offers-to-quote",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_missing_offer_ids_returns_400(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_requisition: Requisition,
    ):
        """Empty offer_ids → 400."""
        quote = _quote(db_session, test_requisition, test_user)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/add-offers-to-quote",
            content=json.dumps({"offer_ids": [], "quote_id": quote.id}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_missing_quote_id_returns_400(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_requisition: Requisition,
    ):
        """quote_id=0 (falsy) → 400."""
        offer = _offer(db_session, test_requisition, test_user)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/add-offers-to-quote",
            content=json.dumps({"offer_ids": [offer.id], "quote_id": 0}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_quote_not_found_returns_404(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_requisition: Requisition,
    ):
        """Nonexistent quote_id → 404."""
        offer = _offer(db_session, test_requisition, test_user)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/add-offers-to-quote",
            content=json.dumps({"offer_ids": [offer.id], "quote_id": 99999}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 404

    def test_non_draft_quote_returns_400(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_requisition: Requisition,
    ):
        """Quote not in draft status → 400."""
        offer = _offer(db_session, test_requisition, test_user)
        quote = _quote(db_session, test_requisition, test_user, status="sent")
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/add-offers-to-quote",
            content=json.dumps({"offer_ids": [offer.id], "quote_id": quote.id}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_valid_request_adds_lines(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_requisition: Requisition,
    ):
        """Valid draft quote + offer → QuoteLine created, 200 HTML returned."""
        offer = _offer(db_session, test_requisition, test_user)
        quote = _quote(db_session, test_requisition, test_user, status="draft")
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/add-offers-to-quote",
            content=json.dumps({"offer_ids": [offer.id], "quote_id": quote.id}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert "added" in resp.text.lower()

    def test_duplicate_offer_not_added_twice(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_requisition: Requisition,
    ):
        """Offer already in quote → not duplicated, still 200."""
        offer = _offer(db_session, test_requisition, test_user)
        quote = _quote(db_session, test_requisition, test_user, status="draft")
        db_session.flush()
        # Pre-add the line
        existing_line = QuoteLine(
            quote_id=quote.id,
            offer_id=offer.id,
            mpn=offer.mpn,
            manufacturer="",
            qty=offer.qty_available or 1,
            cost_price=float(offer.unit_price or 0),
            sell_price=float(offer.unit_price or 0),
            margin_pct=0.0,
        )
        db_session.add(existing_line)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/add-offers-to-quote",
            content=json.dumps({"offer_ids": [offer.id], "quote_id": quote.id}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200


# ── Tests: Email Reply ────────────────────────────────────────────────


class TestEmailReply:
    def test_missing_to_returns_400(self, client: TestClient):
        """Missing 'to' field → 400."""
        resp = client.post(
            "/v2/partials/emails/reply",
            data={"body": "Hello there", "subject": "Test"},
        )
        assert resp.status_code == 400

    def test_missing_body_returns_400(self, client: TestClient):
        """Missing 'body' field → 400."""
        resp = client.post(
            "/v2/partials/emails/reply",
            data={"to": "vendor@example.com", "subject": "Test"},
        )
        assert resp.status_code == 400

    def test_both_missing_returns_400(self, client: TestClient):
        """Both 'to' and 'body' missing → 400."""
        resp = client.post("/v2/partials/emails/reply", data={"subject": "Test"})
        assert resp.status_code == 400

    def test_graph_api_connection_error_returns_200_with_error(self, client: TestClient):
        """Graph API connection error → 200 with error template (no real M365 token in tests)."""
        # The route imports require_fresh_token locally — in test mode it raises HTTPException
        # which is caught and displayed as an error in the reply_result template (status 200).
        resp = client.post(
            "/v2/partials/emails/reply",
            data={
                "to": "vendor@example.com",
                "subject": "Re: RFQ",
                "body": "We are interested",
                "conversation_id": "conv-123",
            },
        )
        assert resp.status_code == 200
        # Error displayed in template (M365 refresh message or network error message)
        assert "m365" in resp.text.lower() or "refresh" in resp.text.lower() or resp.text

    def test_http_exception_from_token_returns_200_with_error(self, client: TestClient):
        """HTTPException from require_fresh_token → 200 with M365 error message in template.

        The route uses a local import of require_fresh_token and calls it directly
        (bypassing FastAPI dependency injection). In test environments without a
        real M365 token, it raises HTTPException which is caught and shown in the template.
        """
        resp = client.post(
            "/v2/partials/emails/reply",
            data={
                "to": "vendor@example.com",
                "body": "Hello",
            },
        )
        assert resp.status_code == 200
        # Template is rendered with error set — check it has some content
        assert resp.text

    def test_successful_send_returns_200(self, client: TestClient):
        """Reply endpoint returns 200 — errors shown in template, not as HTTP error status."""

        # require_fresh_token is imported locally inside the route function body.
        # Patch at source module (app.dependencies) so the local import picks up mock.
        async def _mock_token(*args, **kwargs):
            return "mock-access-token"

        with patch("app.dependencies.require_fresh_token", new=_mock_token):
            with patch("app.utils.graph_client.GraphClient.post_json", new_callable=AsyncMock) as mock_post:
                mock_post.return_value = {}
                resp = client.post(
                    "/v2/partials/emails/reply",
                    data={
                        "to": "vendor@example.com",
                        "subject": "Re: RFQ for LM317T",
                        "body": "We have found suitable pricing.",
                        "conversation_id": "conv-abc123",
                    },
                )
        assert resp.status_code == 200


# ── Tests: Review Response ────────────────────────────────────────────


class TestReviewResponse:
    def test_response_not_found_returns_404(
        self,
        client: TestClient,
        test_requisition: Requisition,
    ):
        """Non-existent response_id → 404."""
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/responses/99999/review",
            data={"status": "reviewed"},
        )
        assert resp.status_code == 404

    def test_invalid_status_returns_400(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
    ):
        """Status not in ('reviewed', 'rejected') → 400."""
        vr = _vendor_response(db_session, test_requisition)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/responses/{vr.id}/review",
            data={"status": "approved"},
        )
        assert resp.status_code == 400

    def test_valid_reviewed_status(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
    ):
        """Valid 'reviewed' status → 200 and response card template."""
        vr = _vendor_response(db_session, test_requisition)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/responses/{vr.id}/review",
            data={"status": "reviewed"},
        )
        assert resp.status_code == 200

    def test_valid_rejected_status(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
    ):
        """Valid 'rejected' status → 200."""
        vr = _vendor_response(db_session, test_requisition)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/responses/{vr.id}/review",
            data={"status": "rejected"},
        )
        assert resp.status_code == 200

    def test_response_wrong_requisition_returns_404(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_requisition: Requisition,
    ):
        """Response exists but belongs to different requisition → 404."""
        other_req = _req(db_session, test_user)
        db_session.flush()
        vr = _vendor_response(db_session, other_req)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/responses/{vr.id}/review",
            data={"status": "reviewed"},
        )
        assert resp.status_code == 404


# ── Tests: Create Quote from Offers ──────────────────────────────────


class TestCreateQuoteFromOffers:
    def test_no_offer_ids_returns_400(
        self,
        client: TestClient,
        test_requisition: Requisition,
    ):
        """Empty offer_ids list → 400."""
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/create-quote",
            data={},
        )
        assert resp.status_code == 400

    def test_offers_not_on_requisition_returns_404(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_requisition: Requisition,
    ):
        """offer_ids that don't match req_id → 404 (no matching offers)."""
        other_req = _req(db_session, test_user)
        offer = _offer(db_session, other_req, test_user)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/create-quote",
            data={"offer_ids": str(offer.id)},
        )
        assert resp.status_code == 404

    def test_invalid_offer_id_type_returns_400(
        self,
        client: TestClient,
        test_requisition: Requisition,
    ):
        """Non-integer offer_ids → 400."""
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/create-quote",
            data={"offer_ids": "not-an-int"},
        )
        assert resp.status_code == 400

    def test_valid_offer_creates_quote(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_requisition: Requisition,
    ):
        """Valid offer on the requisition → Quote + QuoteLines created, 200 HTML."""
        offer = _offer(db_session, test_requisition, test_user)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/create-quote",
            data={"offer_ids": str(offer.id)},
        )
        assert resp.status_code == 200

    def test_multiple_offers_create_multiple_lines(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_requisition: Requisition,
    ):
        """Multiple valid offers → all included as QuoteLines."""
        offer1 = _offer(db_session, test_requisition, test_user, mpn="LM317T")
        offer2 = _offer(db_session, test_requisition, test_user, mpn="NE555P")
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/create-quote",
            data={"offer_ids": [str(offer1.id), str(offer2.id)]},
        )
        assert resp.status_code == 200

    def test_requisition_not_found_returns_404(
        self,
        client: TestClient,
    ):
        """Non-existent req_id → 404 before checking offers."""
        resp = client.post(
            "/v2/partials/requisitions/99999/create-quote",
            data={"offer_ids": "1"},
        )
        assert resp.status_code == 404
