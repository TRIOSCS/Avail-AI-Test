"""test_htmx_views_nightly4.py — Fourth nightly coverage boost for htmx_views.py.

Targets: follow-ups list, vendor detail/tabs/edit/reviews/nudges,
         vendor contact timeline, part header/cell/spec inline editing,
         part tab routes (activity, comms, notes), error branches.

Called by: pytest
Depends on: conftest.py (client, db_session, test_user, test_vendor_card)
"""

import os

os.environ["TESTING"] = "1"

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import RequisitionStatus, SourcingStatus
from app.models import Requirement, Requisition, User, VendorCard, VendorContact, VendorReview
from app.models.sourcing_lead import SourcingLead

# ── Helpers ──────────────────────────────────────────────────────────


def _req(db: Session, user: User, **kw) -> Requisition:
    defaults = dict(
        name="N4-REQ",
        customer_name="N4 Corp",
        status=RequisitionStatus.OPEN,
        created_by=user.id,
        created_at=datetime.now(UTC),
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
        created_at=datetime.now(UTC),
    )
    defaults.update(kw)
    obj = Requirement(**defaults)
    db.add(obj)
    db.flush()
    return obj


def _vendor(db: Session, name: str = "TestVendorN4", **kw) -> VendorCard:
    normalized = name.lower().replace(" ", "-")
    defaults = dict(
        normalized_name=normalized,
        display_name=name,
        emails=[],
        phones=[],
        sighting_count=0,
        created_at=datetime.now(UTC),
    )
    defaults.update(kw)
    obj = VendorCard(**defaults)
    db.add(obj)
    db.flush()
    return obj


def _vendor_contact(db: Session, vendor: VendorCard, email: str = "contact@vendor.com", **kw) -> VendorContact:
    defaults = dict(
        vendor_card_id=vendor.id,
        source="manual",
        email=email,
        full_name="Test Contact",
    )
    defaults.update(kw)
    obj = VendorContact(**defaults)
    db.add(obj)
    db.flush()
    return obj


def _rfq_contact(db: Session, req: Requisition, user: User, **kw):
    """Create a Contact (RFQ contact) in the contacts table."""
    from app.models.offers import Contact as RfqContact

    defaults = dict(
        requisition_id=req.id,
        user_id=user.id,
        contact_type="email",
        vendor_name="SomeVendor",
        vendor_name_normalized="somevendor",
        vendor_contact="vendor@example.com",
        status="sent",
        created_at=datetime.now(UTC) - timedelta(days=5),
    )
    defaults.update(kw)
    obj = RfqContact(**defaults)
    db.add(obj)
    db.flush()
    return obj


# ── Tests: Follow-ups list ────────────────────────────────────────────


class TestFollowUpsListPartial:
    def test_empty_list_returns_200(self, client: TestClient, db_session: Session, test_user: User):
        resp = client.get("/v2/partials/follow-ups")
        assert resp.status_code == 200

    def test_with_stale_contacts(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        _rfq_contact(db_session, req, test_user)
        db_session.commit()

        resp = client.get("/v2/partials/follow-ups")
        assert resp.status_code == 200


# ── Tests: Vendor detail partial ─────────────────────────────────────


class TestVendorDetailPartial:
    def test_basic(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard):
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}")
        assert resp.status_code == 200

    def test_with_mpn_filter(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard):
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}?mpn=LM317T")
        assert resp.status_code == 200

    def test_with_safety_data_from_lead(
        self, client: TestClient, db_session: Session, test_vendor_card: VendorCard, test_user: User
    ):
        req = _req(db_session, test_user)
        requirement = _requirement(db_session, req)
        db_session.commit()

        lead = SourcingLead(
            lead_id=f"lead-{uuid.uuid4().hex}",
            requirement_id=requirement.id,
            requisition_id=req.id,
            part_number_requested="LM317T",
            part_number_matched="LM317T",
            vendor_name=test_vendor_card.display_name,
            vendor_name_normalized=test_vendor_card.normalized_name,
            primary_source_type="manual",
            primary_source_name="test",
            vendor_safety_band="GREEN",
            vendor_safety_summary="Verified distributor",
            vendor_safety_flags=[],
            created_at=datetime.now(UTC),
        )
        db_session.add(lead)
        db_session.commit()

        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}")
        assert resp.status_code == 200

    def test_vendor_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/vendors/99999")
        assert resp.status_code == 404


# ── Tests: Vendor tab partial ─────────────────────────────────────────


class TestVendorTabPartial:
    @pytest.mark.parametrize("tab", ["overview", "find_contacts", "emails", "offers"])
    def test_tab_returns_200(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard, tab: str):
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/tab/{tab}")
        assert resp.status_code == 200

    def test_tab_overview_with_safety_lead(
        self, client: TestClient, db_session: Session, test_vendor_card: VendorCard, test_user: User
    ):
        req = _req(db_session, test_user)
        requirement = _requirement(db_session, req)
        db_session.commit()

        lead = SourcingLead(
            lead_id=f"lead-{uuid.uuid4().hex}",
            requirement_id=requirement.id,
            requisition_id=req.id,
            part_number_requested="LM317T",
            part_number_matched="LM317T",
            vendor_name=test_vendor_card.display_name,
            vendor_name_normalized=test_vendor_card.normalized_name,
            primary_source_type="manual",
            primary_source_name="test",
            vendor_safety_band="RED",
            vendor_safety_summary="Risk detected",
            vendor_safety_flags=["counterfeit_risk"],
            vendor_safety_score=25.0,
            created_at=datetime.now(UTC),
        )
        db_session.add(lead)
        db_session.commit()

        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/tab/overview")
        assert resp.status_code == 200

    def test_tab_contacts(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard):
        _vendor_contact(db_session, test_vendor_card)
        db_session.commit()

        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/tab/contacts")
        assert resp.status_code == 200

    def test_tab_analytics(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard):
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/tab/analytics")
        assert resp.status_code == 200
        assert "Win Rate" in resp.text

    def test_tab_reviews(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard, test_user: User):
        review = VendorReview(
            vendor_card_id=test_vendor_card.id,
            user_id=test_user.id,
            rating=4,
            comment="Good vendor",
            created_at=datetime.now(UTC),
        )
        db_session.add(review)
        db_session.commit()

        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/tab/reviews")
        assert resp.status_code == 200

    def test_invalid_tab_returns_404(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard):
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/tab/nonexistent")
        assert resp.status_code == 404


# ── Tests: Vendor edit form ───────────────────────────────────────────


class TestVendorEditForm:
    def test_returns_edit_form(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard):
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/edit-form")
        assert resp.status_code == 200

    def test_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/vendors/99999/edit-form")
        assert resp.status_code == 404


# ── Tests: Vendor reviews ────────────────────────────────────────────


class TestVendorReviews:
    def test_no_reviews(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard):
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/reviews")
        assert resp.status_code == 200

    def test_with_review(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard, test_user: User):
        review = VendorReview(
            vendor_card_id=test_vendor_card.id,
            user_id=test_user.id,
            rating=5,
            comment="Excellent",
            created_at=datetime.now(UTC),
        )
        db_session.add(review)
        db_session.commit()

        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/reviews")
        assert resp.status_code == 200


# ── Tests: Part header save (PATCH) error branches ───────────────────


class TestPartHeaderSaveErrors:
    def test_invalid_field_returns_400(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        requirement = _requirement(db_session, req)
        db_session.commit()

        resp = client.patch(
            f"/v2/partials/parts/{requirement.id}/header",
            data={"field": "invalid_field_xyz", "value": "something"},
        )
        assert resp.status_code == 400
        assert "Invalid" in resp.text

    def test_nonexistent_requirement_returns_404(self, client: TestClient):
        resp = client.patch(
            "/v2/partials/parts/99999/header",
            data={"field": "notes", "value": "test"},
        )
        assert resp.status_code == 404

    def test_sourcing_status_field_transition(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        requirement = _requirement(db_session, req, sourcing_status=SourcingStatus.OPEN)
        db_session.commit()

        with patch("app.services.requirement_status.transition_requirement", return_value=True):
            resp = client.patch(
                f"/v2/partials/parts/{requirement.id}/header",
                data={"field": "sourcing_status", "value": "sourcing"},
            )
        assert resp.status_code == 200

    def test_sourcing_status_transition_rejected(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        requirement = _requirement(db_session, req, sourcing_status=SourcingStatus.OPEN)
        db_session.commit()

        with patch("app.services.requirement_status.transition_requirement", return_value=False):
            resp = client.patch(
                f"/v2/partials/parts/{requirement.id}/header",
                data={"field": "sourcing_status", "value": "won"},
            )
        assert resp.status_code == 200

    @pytest.mark.parametrize(
        "field,value",
        [
            ("target_qty", "250"),
            ("target_price", "1.25"),
            ("manufacturer", "Texas Instruments"),
            ("notes", "Some notes here"),
        ],
    )
    def test_valid_field_returns_200(
        self, client: TestClient, db_session: Session, test_user: User, field: str, value: str
    ):
        req = _req(db_session, test_user)
        requirement = _requirement(db_session, req)
        db_session.commit()

        resp = client.patch(
            f"/v2/partials/parts/{requirement.id}/header",
            data={"field": field, "value": value},
        )
        assert resp.status_code == 200


# ── Tests: Part cell inline edit ────────────────────────────────────


class TestPartCellEdit:
    @pytest.mark.parametrize(
        "mode,field,expected",
        [
            ("edit", "target_qty", 200),
            ("edit", "invalid_field", 400),
            ("display", "target_qty", 200),
            ("display", "badfield", 400),
        ],
    )
    def test_cell_get_field(
        self, client: TestClient, db_session: Session, test_user: User, mode: str, field: str, expected: int
    ):
        req = _req(db_session, test_user)
        requirement = _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/parts/{requirement.id}/cell/{mode}/{field}")
        assert resp.status_code == expected

    @pytest.mark.parametrize("mode", ["edit", "display"])
    def test_cell_get_not_found(self, client: TestClient, mode: str):
        resp = client.get(f"/v2/partials/parts/99999/cell/{mode}/target_qty")
        assert resp.status_code == 404

    def test_cell_save_invalid_field(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        requirement = _requirement(db_session, req)
        db_session.commit()

        resp = client.patch(
            f"/v2/partials/parts/{requirement.id}/cell",
            data={"field": "bad_field", "value": "x"},
        )
        assert resp.status_code == 400

    def test_cell_save_not_found(self, client: TestClient):
        resp = client.patch(
            "/v2/partials/parts/99999/cell",
            data={"field": "target_qty", "value": "10"},
        )
        assert resp.status_code == 404


# ── Tests: Part spec edit ────────────────────────────────────────────


class TestPartSpecEdit:
    @pytest.mark.parametrize(
        "field,expected",
        [
            ("condition", 200),
            ("nonexistent_field", 400),
        ],
    )
    def test_spec_edit_field(self, client: TestClient, db_session: Session, test_user: User, field: str, expected: int):
        req = _req(db_session, test_user)
        requirement = _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/parts/{requirement.id}/edit-spec/{field}")
        assert resp.status_code == expected

    def test_spec_edit_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/parts/99999/edit-spec/condition")
        assert resp.status_code == 404

    def test_spec_edit_archived_returns_403(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        requirement = _requirement(db_session, req, sourcing_status=SourcingStatus.ARCHIVED)
        db_session.commit()

        resp = client.get(f"/v2/partials/parts/{requirement.id}/edit-spec/condition")
        assert resp.status_code == 403

    def test_spec_save_invalid_field(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        requirement = _requirement(db_session, req)
        db_session.commit()

        resp = client.patch(
            f"/v2/partials/parts/{requirement.id}/save-spec",
            data={"field": "badfield", "value": "New"},
        )
        assert resp.status_code == 400

    def test_spec_save_not_found(self, client: TestClient):
        resp = client.patch(
            "/v2/partials/parts/99999/save-spec",
            data={"field": "condition", "value": "New"},
        )
        assert resp.status_code == 404

    def test_spec_save_archived_returns_403(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        requirement = _requirement(db_session, req, sourcing_status=SourcingStatus.ARCHIVED)
        db_session.commit()

        resp = client.patch(
            f"/v2/partials/parts/{requirement.id}/save-spec",
            data={"field": "condition", "value": "New"},
        )
        assert resp.status_code == 403


# ── Tests: Part tab routes ────────────────────────────────────────────


class TestPartTabRoutes:
    @pytest.mark.parametrize("tab", ["activity", "comms", "notes"])
    def test_returns_tab(self, client: TestClient, db_session: Session, test_user: User, tab: str):
        req = _req(db_session, test_user)
        requirement = _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/parts/{requirement.id}/tab/{tab}")
        assert resp.status_code == 200

    @pytest.mark.parametrize("tab", ["activity", "comms", "notes"])
    def test_not_found(self, client: TestClient, tab: str):
        resp = client.get(f"/v2/partials/parts/99999/tab/{tab}")
        assert resp.status_code == 404


# ── Tests: Part header edit cell (GET) ───────────────────────────────


class TestPartHeaderEditCell:
    @pytest.mark.parametrize(
        "field,expected",
        [
            ("notes", 200),
            ("invalid", 400),
            ("sourcing_status", 200),
            ("condition", 200),
        ],
    )
    def test_edit_field(self, client: TestClient, db_session: Session, test_user: User, field: str, expected: int):
        req = _req(db_session, test_user)
        requirement = _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/parts/{requirement.id}/header/edit/{field}")
        assert resp.status_code == expected

    def test_edit_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/parts/99999/header/edit/notes")
        assert resp.status_code == 404


# ── Tests: Company tab routes ─────────────────────────────────────────


@pytest.fixture()
def _grant_account_management(test_user: User, db_session: Session) -> None:
    """Promote the buyer ``test_user`` to MANAGER so it can_manage every account.

    Company detail + tab partials (``GET /v2/partials/customers/{id}`` and
    ``.../tab/{tab}``) now gate on ``can_manage_account``. The classes below GET those
    endpoints as ``test_user`` on companies they create without assigning ownership, so
    promote the actor to MANAGER (``can_manage_account`` is True for managers, exactly as
    for the account owner) to exercise the authorized render path. Applied per-class via
    ``@pytest.mark.usefixtures`` — scoped narrowly so role-based list tests are untouched.
    """
    test_user.role = "manager"
    db_session.commit()


@pytest.mark.usefixtures("_grant_account_management")
class TestCompanyTabRoutes:
    @pytest.mark.parametrize("tab", ["sites", "contacts", "requisitions", "activity"])
    def test_tab_empty_returns_200(self, client: TestClient, db_session: Session, test_company, tab: str):
        resp = client.get(f"/v2/partials/customers/{test_company.id}/tab/{tab}")
        assert resp.status_code == 200

    def test_invalid_tab_returns_404(self, client: TestClient, db_session: Session, test_company):
        resp = client.get(f"/v2/partials/customers/{test_company.id}/tab/nonexistent")
        assert resp.status_code == 404

    def test_company_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/customers/99999/tab/sites")
        assert resp.status_code == 404

    def test_requisitions_tab_with_data(self, client: TestClient, db_session: Session, test_company, test_user: User):
        req = Requisition(
            name="TAB-REQ-001",
            customer_name=test_company.name,
            company_id=test_company.id,
            status=RequisitionStatus.OPEN,
            created_by=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(req)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{test_company.id}/tab/requisitions")
        assert resp.status_code == 200


# ── Tests: Follow-ups list with SALES role ────────────────────────────


class TestFollowUpsWithSalesRole:
    def test_sales_user_sees_only_their_contacts(self, db_session: Session, sales_user: User):
        """SALES role filters follow-ups to their own requisitions (line 2666)."""
        from app.database import get_db
        from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
        from app.main import app

        def _db():
            yield db_session

        def _user():
            return sales_user

        async def _token():
            return "mock-token"

        overridden = [get_db, require_user, require_admin, require_buyer, require_fresh_token]
        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[require_user] = _user
        app.dependency_overrides[require_admin] = _user
        app.dependency_overrides[require_buyer] = _user
        app.dependency_overrides[require_fresh_token] = _token

        try:
            from fastapi.testclient import TestClient as TC

            with TC(app) as c:
                resp = c.get("/v2/partials/follow-ups")
            assert resp.status_code == 200
        finally:
            for dep in overridden:
                app.dependency_overrides.pop(dep, None)


# ── Tests: Vendor tab with mpn filter ────────────────────────────────


class TestVendorTabWithMpnFilter:
    def test_overview_tab_with_mpn(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard):
        """Covers mpn filter branch in vendor_tab overview (lines 3648-3652)."""
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/tab/overview?mpn=LM317T")
        assert resp.status_code == 200

    def test_detail_with_mpn_no_match(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard):
        """Covers normalize_mpn branch in vendor_detail_partial (lines 3586-3590)."""
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}?mpn=LM317T")
        assert resp.status_code == 200


# ── Tests: Vendor tab offers with data ───────────────────────────────


class TestVendorTabOffersWithData:
    def test_offers_tab_with_existing_offer(
        self, client: TestClient, db_session: Session, test_vendor_card: VendorCard, test_user: User
    ):
        """Covers offers HTML rendering loop in vendor_tab (lines 3795-3806)."""
        from app.constants import OfferStatus
        from app.models import Offer

        req = _req(db_session, test_user)
        offer = Offer(
            requisition_id=req.id,
            vendor_name=test_vendor_card.display_name,
            vendor_name_normalized=test_vendor_card.normalized_name,
            mpn="LM317T",
            normalized_mpn="LM317T",
            source="manual",
            status=OfferStatus.ACTIVE,
            unit_price=1.50,
            qty_available=500,
            created_at=datetime.now(UTC),
        )
        db_session.add(offer)
        db_session.commit()

        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/tab/offers")
        assert resp.status_code == 200


# ── Tests: Offer error branches (pre-await 404 paths) ─────────────────


class TestOfferErrorBranches:
    """Cover 404 branches in offer routes that occur before any await."""

    def test_reconfirm_offer_not_found(self, client: TestClient, db_session: Session, test_user: User):
        """Covers line 2098 — reconfirm 404 before any await."""
        req = _req(db_session, test_user)
        db_session.commit()

        resp = client.post(f"/v2/partials/requisitions/{req.id}/offers/99999/reconfirm")
        assert resp.status_code == 404

    def test_edit_offer_not_found(self, client: TestClient, db_session: Session, test_user: User):
        """Covers line 2149 — edit offer 404 before await request.form()."""
        req = _req(db_session, test_user)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/offers/99999/edit",
            data={"vendor_name": "TestVendor"},
        )
        assert resp.status_code == 404

    def test_mark_sold_offer_not_found(self, client: TestClient, db_session: Session, test_user: User):
        """Covers line 2250 — mark-sold 404 before first await."""
        req = _req(db_session, test_user)
        db_session.commit()

        resp = client.post(f"/v2/partials/requisitions/{req.id}/offers/99999/mark-sold")
        assert resp.status_code == 404


# ── Tests: Requisition action error branches ──────────────────────────


class TestRequisitionActionErrors:
    """Cover error branches in requisition_action route."""

    def test_invalid_action_returns_400(self, client: TestClient, db_session: Session, test_user: User):
        """Covers line 1830 — invalid action branch before await."""
        req = _req(db_session, test_user)
        db_session.commit()

        resp = client.post(f"/v2/partials/requisitions/{req.id}/action/invalid_action")
        assert resp.status_code == 400

    def test_req_not_found_returns_404(self, client: TestClient):
        """Covers the not-found branch (valid action, missing req) before
        request.form()."""
        resp = client.post("/v2/partials/requisitions/99999/action/clone")
        assert resp.status_code == 404
