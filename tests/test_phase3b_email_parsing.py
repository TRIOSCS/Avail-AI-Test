"""test_phase3b_email_parsing.py — Tests for Phase 3B: Email & Freeform Parsing in
offers tab.

Verifies: parse form loading, email parsing, freeform offer parsing,
save-parsed-offers flow, editable cards with confidence badges.

Called by: pytest
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Offer, Requirement, Requisition, User, VendorCard

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def req_with_parts(db_session: Session, test_user: User) -> Requisition:
    """A requisition with requirements for parsing context."""
    req = Requisition(
        name="REQ-PARSE-001",
        customer_name="Test Corp",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    for mpn in ["LM317T", "STM32F407"]:
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


# ── Form Loading ──────────────────────────────────────────────────────


class TestParseFormLoading:
    """Tests for loading parse forms via GET."""

    def test_parse_email_form_loads(self, client: TestClient, req_with_parts: Requisition):
        resp = client.get(f"/v2/partials/requisitions/{req_with_parts.id}/parse-email-form")
        assert resp.status_code == 200
        assert "Parse Vendor Email" in resp.text
        assert "email_body" in resp.text

    def test_paste_offer_form_loads(self, client: TestClient, req_with_parts: Requisition):
        resp = client.get(f"/v2/partials/requisitions/{req_with_parts.id}/paste-offer-form")
        assert resp.status_code == 200
        assert "Paste Vendor Offer" in resp.text
        assert "raw_text" in resp.text

    def test_parse_email_form_404(self, client: TestClient):
        resp = client.get("/v2/partials/requisitions/99999/parse-email-form")
        assert resp.status_code == 404

    def test_paste_offer_form_404(self, client: TestClient):
        resp = client.get("/v2/partials/requisitions/99999/paste-offer-form")
        assert resp.status_code == 404

    def test_offers_tab_has_parse_buttons(self, client: TestClient, req_with_parts: Requisition):
        """Offers tab should include Parse Email and Paste Offer buttons."""
        resp = client.get(f"/v2/partials/requisitions/{req_with_parts.id}/tab/offers")
        assert resp.status_code == 200
        assert "Parse Email" in resp.text
        assert "Paste Offer" in resp.text


# ── Email Parsing ─────────────────────────────────────────────────────


class TestParseEmail:
    """Tests for the parse-email POST endpoint."""

    def test_empty_body_returns_warning(self, client: TestClient, req_with_parts: Requisition):
        resp = client.post(
            f"/v2/partials/requisitions/{req_with_parts.id}/parse-email",
            data={"email_body": "", "email_subject": "", "vendor_name": ""},
        )
        assert resp.status_code == 200
        assert "paste the email body" in resp.text.lower()

    @patch("app.services.ai_email_parser.parse_email", new_callable=AsyncMock)
    def test_parsed_quotes_shown_as_cards(self, mock_parse, client: TestClient, req_with_parts: Requisition):
        mock_parse.return_value = {
            "quotes": [
                {
                    "part_number": "LM317T",
                    "manufacturer": "TI",
                    "quantity_available": 5000,
                    "unit_price": 0.45,
                    "confidence": 0.85,
                    "lead_time_text": "2-3 weeks",
                },
            ],
            "overall_confidence": 0.85,
            "email_type": "quote",
        }
        resp = client.post(
            f"/v2/partials/requisitions/{req_with_parts.id}/parse-email",
            data={"email_body": "Here is our quote for LM317T...", "vendor_name": "Arrow"},
        )
        assert resp.status_code == 200
        assert "LM317T" in resp.text
        assert "85%" in resp.text
        assert "Save" in resp.text

    @patch("app.services.ai_email_parser.parse_email", new_callable=AsyncMock)
    def test_confidence_badges_color_coded(self, mock_parse, client: TestClient, req_with_parts: Requisition):
        mock_parse.return_value = {
            "quotes": [
                {"part_number": "A", "confidence": 0.9},
                {"part_number": "B", "confidence": 0.6},
                {"part_number": "C", "confidence": 0.3},
            ],
            "overall_confidence": 0.6,
            "email_type": "quote",
        }
        resp = client.post(
            f"/v2/partials/requisitions/{req_with_parts.id}/parse-email",
            data={"email_body": "Quote details..."},
        )
        assert resp.status_code == 200
        # High confidence = emerald, medium = amber, low = rose
        assert "bg-emerald-50" in resp.text
        assert "bg-amber-50" in resp.text
        assert "bg-rose-50" in resp.text

    @patch("app.services.ai_email_parser.parse_email", new_callable=AsyncMock)
    def test_parse_error_handled(self, mock_parse, client: TestClient, req_with_parts: Requisition):
        mock_parse.side_effect = RuntimeError("API timeout")
        resp = client.post(
            f"/v2/partials/requisitions/{req_with_parts.id}/parse-email",
            data={"email_body": "Some email text"},
        )
        assert resp.status_code == 200
        assert "Parse failed" in resp.text

    @patch("app.services.ai_email_parser.parse_email", new_callable=AsyncMock)
    def test_no_quotes_shows_empty_state(self, mock_parse, client: TestClient, req_with_parts: Requisition):
        mock_parse.return_value = {
            "quotes": [],
            "overall_confidence": 0,
            "email_type": "unclear",
        }
        resp = client.post(
            f"/v2/partials/requisitions/{req_with_parts.id}/parse-email",
            data={"email_body": "Thanks for your email"},
        )
        assert resp.status_code == 200
        assert "No offers could be parsed" in resp.text


# ── Freeform Offer Parsing ────────────────────────────────────────────


class TestParseOffer:
    """Tests for the parse-offer POST endpoint."""

    def test_empty_text_returns_warning(self, client: TestClient, req_with_parts: Requisition):
        resp = client.post(
            f"/v2/partials/requisitions/{req_with_parts.id}/parse-offer",
            data={"raw_text": ""},
        )
        assert resp.status_code == 200
        assert "paste vendor text" in resp.text.lower()

    @patch("app.services.freeform_parser_service.parse_freeform_offer", new_callable=AsyncMock)
    def test_parsed_offers_shown(self, mock_parse, client: TestClient, req_with_parts: Requisition):
        mock_parse.return_value = {
            "offers": [
                {
                    "vendor_name": "Arrow",
                    "mpn": "LM317T",
                    "qty_available": 2000,
                    "unit_price": 0.50,
                },
            ],
        }
        resp = client.post(
            f"/v2/partials/requisitions/{req_with_parts.id}/parse-offer",
            data={"raw_text": "Arrow has LM317T 2000pcs at $0.50"},
        )
        assert resp.status_code == 200
        assert "LM317T" in resp.text
        assert "Arrow" in resp.text

    @patch("app.services.freeform_parser_service.parse_freeform_offer", new_callable=AsyncMock)
    def test_parse_offer_error_handled(self, mock_parse, client: TestClient, req_with_parts: Requisition):
        mock_parse.side_effect = RuntimeError("Service down")
        resp = client.post(
            f"/v2/partials/requisitions/{req_with_parts.id}/parse-offer",
            data={"raw_text": "Some text"},
        )
        assert resp.status_code == 200
        assert "Parse failed" in resp.text


# ── Save Parsed Offers ───────────────────────────────────────────────


class TestSaveParsedOffers:
    """Tests for saving edited parsed offers to the requisition."""

    def test_save_creates_offer_records(self, client: TestClient, db_session: Session, req_with_parts: Requisition):
        resp = client.post(
            f"/v2/partials/requisitions/{req_with_parts.id}/save-parsed-offers",
            data={
                "vendor_name": "Arrow",
                "offers[0].mpn": "LM317T",
                "offers[0].manufacturer": "TI",
                "offers[0].qty_available": "5000",
                "offers[0].unit_price": "0.45",
                "offers[0].lead_time": "2-3 weeks",
                "offers[0].condition": "new",
            },
        )
        assert resp.status_code == 200
        assert "1 offer" in resp.text
        assert "saved" in resp.text.lower()

        offer = db_session.query(Offer).filter(Offer.requisition_id == req_with_parts.id).first()
        assert offer is not None
        assert offer.mpn == "LM317T"
        assert float(offer.unit_price) == 0.45
        assert offer.vendor_name is not None

    def test_save_multiple_offers(self, client: TestClient, db_session: Session, req_with_parts: Requisition):
        resp = client.post(
            f"/v2/partials/requisitions/{req_with_parts.id}/save-parsed-offers",
            data={
                "vendor_name": "Arrow",
                "offers[0].mpn": "LM317T",
                "offers[0].qty_available": "5000",
                "offers[0].unit_price": "0.45",
                "offers[1].mpn": "STM32F407",
                "offers[1].qty_available": "1000",
                "offers[1].unit_price": "3.20",
            },
        )
        assert resp.status_code == 200
        assert "2 offers saved" in resp.text

        count = db_session.query(Offer).filter(Offer.requisition_id == req_with_parts.id).count()
        assert count == 2

    def test_save_matches_requirement_by_mpn(
        self, client: TestClient, db_session: Session, req_with_parts: Requisition
    ):
        """Saved offers should be linked to matching requirements."""
        client.post(
            f"/v2/partials/requisitions/{req_with_parts.id}/save-parsed-offers",
            data={
                "vendor_name": "Arrow",
                "offers[0].mpn": "LM317T",
                "offers[0].unit_price": "0.45",
            },
        )
        offer = db_session.query(Offer).filter(Offer.requisition_id == req_with_parts.id).first()
        assert offer is not None
        assert offer.requirement_id is not None

        req_obj = db_session.query(Requirement).filter(Requirement.id == offer.requirement_id).first()
        assert req_obj.primary_mpn == "LM317T"

    def test_save_creates_vendor_card_if_missing(
        self, client: TestClient, db_session: Session, req_with_parts: Requisition
    ):
        """Should create a VendorCard if vendor doesn't exist."""
        client.post(
            f"/v2/partials/requisitions/{req_with_parts.id}/save-parsed-offers",
            data={
                "vendor_name": "NewVendorXYZ",
                "offers[0].mpn": "LM317T",
                "offers[0].unit_price": "0.50",
            },
        )
        from app.vendor_utils import normalize_vendor_name

        norm = normalize_vendor_name("NewVendorXYZ")
        card = db_session.query(VendorCard).filter(VendorCard.normalized_name == norm).first()
        assert card is not None

    def test_save_empty_offers_returns_warning(self, client: TestClient, req_with_parts: Requisition):
        resp = client.post(
            f"/v2/partials/requisitions/{req_with_parts.id}/save-parsed-offers",
            data={"vendor_name": "Arrow"},
        )
        assert resp.status_code == 200
        assert "No offers to save" in resp.text

    def test_save_404_for_missing_requisition(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/99999/save-parsed-offers",
            data={"offers[0].mpn": "LM317T"},
        )
        assert resp.status_code == 404

    def test_save_with_freeform_vendor_names(
        self, client: TestClient, db_session: Session, req_with_parts: Requisition
    ):
        """Freeform offers have per-offer vendor names instead of a global one."""
        resp = client.post(
            f"/v2/partials/requisitions/{req_with_parts.id}/save-parsed-offers",
            data={
                "offers[0].vendor_name": "Arrow",
                "offers[0].mpn": "LM317T",
                "offers[0].unit_price": "0.45",
                "offers[1].vendor_name": "DigiKey",
                "offers[1].mpn": "STM32F407",
                "offers[1].unit_price": "3.20",
            },
        )
        assert resp.status_code == 200
        assert "2 offers saved" in resp.text

        offers = db_session.query(Offer).filter(Offer.requisition_id == req_with_parts.id).all()
        vendors = {o.vendor_name for o in offers}
        assert len(vendors) == 2
