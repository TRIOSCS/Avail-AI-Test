"""test_phase3b_email_parsing.py — Tests for the save-parsed-offers flow.

Verifies: the surviving save-parsed-offers route (the Add-offer paste box's save
step) creates offers, matches requirements by MPN, and creates vendor cards.
(The parse-email / paste-offer door tests died with those doors in W3, spec §5.1
— the paste box's parse flow is covered in tests/test_offer_doors.py.)

Called by: pytest
Depends on: conftest.py fixtures, app.routers.htmx.offers.crud
"""

from datetime import UTC, datetime

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
        created_at=datetime.now(UTC),
    )
    db_session.add(req)
    db_session.flush()

    for mpn in ["LM317T", "STM32F407"]:
        r = Requirement(
            requisition_id=req.id,
            primary_mpn=mpn,
            target_qty=1000,
            created_at=datetime.now(UTC),
        )
        db_session.add(r)

    db_session.commit()
    db_session.refresh(req)
    return req


# (TestParseFormLoading / TestParseEmail / TestParseOffer left with the two-doors
# collapse, spec §5.1 — the surviving Add-offer paste box's parse flow is covered
# in tests/test_offer_doors.py.)


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
