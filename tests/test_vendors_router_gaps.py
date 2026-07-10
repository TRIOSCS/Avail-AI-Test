"""tests/test_vendors_router_gaps.py — Targeted coverage for missing lines in vendors
router.

Covers lines: 231-264, 577, 595-602, 682-696, 701-706, 709-713, 846-874,
              897-918, 999, 1076-1092, 1116, 1149, 1197-1213

Called by: pytest
Depends on: app/routers/htmx/vendors.py, conftest.py fixtures
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User, VendorCard, VendorContact

# ── Admin client fixture ─────────────────────────────────────────────────────


@pytest.fixture()
def admin_user(db_session: Session) -> User:
    """Admin-role user (may already exist from conftest — redefined locally to avoid
    dep)."""
    user = User(
        email="vendor_gaps_admin@trioscs.com",
        name="Gaps Admin",
        role="admin",
        azure_id="test-azure-gaps-admin",
        created_at=datetime.now(UTC),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def admin_client(db_session: Session, admin_user: User) -> TestClient:
    """TestClient with all auth deps overridden to admin_user."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    overridden = [get_db, require_user, require_admin, require_buyer, require_fresh_token]
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: admin_user
    app.dependency_overrides[require_admin] = lambda: admin_user
    app.dependency_overrides[require_buyer] = lambda: admin_user
    app.dependency_overrides[require_fresh_token] = lambda: "token"
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in overridden:
            app.dependency_overrides.pop(dep, None)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _mock_form_request(fields: dict):
    """Return a minimal async-form request mock."""
    form = MagicMock()
    form.get = lambda key, default="": fields.get(key, default)

    req = MagicMock()
    req.headers = {}
    req.form = AsyncMock(return_value=form)
    return req


# ── Lines 231-264: create_vendor_partial_early success ───────────────────────


class TestCreateVendorSuccess:
    """POST /v2/partials/vendors/create — success path."""

    def test_create_vendor_no_existing_creates_card(self, client, db_session):
        """Lines 235-264: display_name provided, no duplicate, card created."""
        # find_vendor_card_by_name is lazily imported inside the function body,
        # so patch at the source module where it lives.
        with patch("app.utils.vendor_helpers.find_vendor_card_by_name", return_value=None):
            with patch(
                "app.routers.htmx.vendors.vendor_detail_partial",
                new=AsyncMock(return_value=HTMLResponse("<div>vendor detail</div>")),
            ):
                resp = client.post(
                    "/v2/partials/vendors/create",
                    data={"display_name": "BrandNew Supplier Inc"},
                )
        assert resp.status_code == 200

        # Card should exist in DB
        card = db_session.query(VendorCard).filter_by(display_name="BrandNew Supplier Inc").first()
        assert card is not None
        assert card.source == "manual"
        assert card.is_new_vendor is True

    def test_create_vendor_with_emails_and_phones(self, client, db_session):
        """Lines 240-264: emails and phones are parsed from form data."""
        with patch("app.utils.vendor_helpers.find_vendor_card_by_name", return_value=None):
            with patch(
                "app.routers.htmx.vendors.vendor_detail_partial",
                new=AsyncMock(return_value=HTMLResponse("<div>ok</div>")),
            ):
                resp = client.post(
                    "/v2/partials/vendors/create",
                    data={
                        "display_name": "Vendor With Contacts",
                        "emails": "sales@vendor.com, info@vendor.com",
                        "phones": "555-1234, 555-5678",
                        "website": "https://vendor.com",
                    },
                )
        assert resp.status_code == 200

        card = db_session.query(VendorCard).filter_by(display_name="Vendor With Contacts").first()
        assert card is not None
        assert "sales@vendor.com" in card.emails
        assert "info@vendor.com" in card.emails

    def test_create_vendor_missing_display_name_returns_400(self, client):
        """Lines 232-233: blank display_name → 400."""
        resp = client.post("/v2/partials/vendors/create", data={"display_name": ""})
        assert resp.status_code == 400

    def test_create_vendor_duplicate_returns_409(self, client, test_vendor_card):
        """Lines 237-238: existing vendor → 409."""
        # Patch at source module since the import is lazy (inside function body).
        with patch(
            "app.utils.vendor_helpers.find_vendor_card_by_name",
            return_value=test_vendor_card,
        ):
            resp = client.post(
                "/v2/partials/vendors/create",
                data={"display_name": test_vendor_card.display_name},
            )
        assert resp.status_code == 409


# ── Line 577: vendor_tab "Other" activity bucket ─────────────────────────────


class TestVendorTabActivityOther:
    """vendor_tab with tab=activity covers the 'Other' bucket (line 577)."""

    async def test_activity_tab_other_bucket(self, db_session: Session, test_vendor_card: VendorCard):
        """Line 577: activity type not in calls/emails/meetings/notes lands in 'Other'."""
        # ActivityLog with a type that doesn't fall into known buckets
        from app.models.intelligence import ActivityLog
        from app.routers.htmx.vendors import vendor_tab

        activity = ActivityLog(
            activity_type="rfq_sent",  # not in _CALLS/_EMAILS/_MEETINGS/_NOTES
            channel="email",  # NOT NULL in schema
            vendor_card_id=test_vendor_card.id,
            user_id=None,
            created_at=datetime.now(UTC),
        )
        db_session.add(activity)
        db_session.commit()

        req = MagicMock()
        req.headers = {}
        user = db_session.query(User).first() or User(email="tmp@x.com", name="X", role="buyer", azure_id="x-az")

        with patch("app.routers.htmx._shared_tabs.template_response") as mock_tpl:
            mock_tpl.return_value = HTMLResponse("<html/>")
            result = await vendor_tab(
                request=req,
                vendor_id=test_vendor_card.id,
                tab="activity",
                user=user,
                db=db_session,
            )

        assert result.status_code == 200
        # Verify sections were built — template_response was called with "sections"
        call_kwargs = mock_tpl.call_args[0][1]  # ctx dict
        assert "Other" in call_kwargs["sections"]
        assert len(call_kwargs["sections"]["Other"]) == 1


# ── Lines 595-602: vendor_tab "tasks" branch ─────────────────────────────────


class TestVendorTabTasks:
    """vendor_tab with tab=tasks (lines 595-602)."""

    async def test_tasks_tab_renders(self, db_session: Session, test_vendor_card: VendorCard):
        """Lines 595-602: tasks tab fetches open tasks and renders partial."""
        from app.routers.htmx.vendors import vendor_tab

        req = MagicMock()
        req.headers = {}
        user = MagicMock()

        with (
            patch("app.routers.htmx._shared_tabs.template_response") as mock_tpl,
            patch(
                "app.services.task_service.get_open_tasks_for_vendor_card",
                return_value=[],
            ),
        ):
            mock_tpl.return_value = HTMLResponse("<html/>")
            result = await vendor_tab(
                request=req,
                vendor_id=test_vendor_card.id,
                tab="tasks",
                user=user,
                db=db_session,
            )

        assert result.status_code == 200
        called_template = mock_tpl.call_args[0][0]
        assert "vendor_tasks" in called_template or "task" in called_template.lower()


# ── Lines 682-713: vendor edit with display_name / emails / phones ────────────


class TestVendorEdit:
    """POST /v2/partials/vendors/{id}/edit — field update paths."""

    def test_edit_display_name_updates_vendor(self, client, db_session, test_vendor_card):
        """Lines 682-696: display_name provided in form → vendor name updated."""
        with patch(
            "app.routers.htmx.vendors.vendor_detail_partial",
            new=AsyncMock(return_value=HTMLResponse("<div>updated</div>")),
        ):
            resp = client.post(
                f"/v2/partials/vendors/{test_vendor_card.id}/edit",
                data={"display_name": "Updated Vendor Name"},
            )
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert test_vendor_card.display_name == "Updated Vendor Name"

    def test_edit_blank_display_name_returns_400(self, client, test_vendor_card):
        """Lines 685-686: display_name present but blank → 400."""
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/edit",
            data={"display_name": "   "},
        )
        assert resp.status_code == 400

    def test_edit_emails_valid_updates_emails(self, client, db_session, test_vendor_card):
        """Lines 695-703: valid emails in form → vendor.emails updated."""
        with patch(
            "app.routers.htmx.vendors.vendor_detail_partial",
            new=AsyncMock(return_value=HTMLResponse("<div>ok</div>")),
        ):
            resp = client.post(
                f"/v2/partials/vendors/{test_vendor_card.id}/edit",
                data={"emails": "new@vendor.com, other@vendor.com"},
            )
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert "new@vendor.com" in test_vendor_card.emails

    def test_edit_invalid_email_returns_400(self, client, test_vendor_card):
        """Lines 700-702: email without '@' → 400."""
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/edit",
            data={"emails": "notanemail"},
        )
        assert resp.status_code == 400

    def test_edit_phones_updates_phones(self, client, db_session, test_vendor_card):
        """Lines 705-707: phones_raw present → vendor.phones updated."""
        with patch(
            "app.routers.htmx.vendors.vendor_detail_partial",
            new=AsyncMock(return_value=HTMLResponse("<div>ok</div>")),
        ):
            resp = client.post(
                f"/v2/partials/vendors/{test_vendor_card.id}/edit",
                data={"phones": "555-9999"},
            )
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert "555-9999" in test_vendor_card.phones


# ── Lines 846-874: vendor_contact_add success ────────────────────────────────


class TestVendorContactAdd:
    """POST /v2/partials/vendors/{id}/contacts — success path."""

    def test_add_contact_creates_contact(self, client, db_session, test_vendor_card):
        """Lines 846-874: valid email + full_name → contact created, row rendered."""
        with patch(
            "app.routers.htmx.vendors.template_response",
        ) as mock_tpl:
            mock_tpl.return_value = HTMLResponse("<tr>contact row</tr>")
            resp = client.post(
                f"/v2/partials/vendors/{test_vendor_card.id}/contacts",
                data={
                    "email": "newcontact@vendor.com",
                    "full_name": "Jane Doe",
                    "title": "VP Sales",
                    "phone": "555-0001",
                },
            )
        assert resp.status_code == 200

        vc = (
            db_session.query(VendorContact)
            .filter_by(vendor_card_id=test_vendor_card.id, email="newcontact@vendor.com")
            .first()
        )
        assert vc is not None
        assert vc.full_name == "Jane Doe"
        assert vc.contact_type == "individual"

    def test_add_contact_no_email_returns_400(self, client, test_vendor_card):
        """Line 847-848: missing email → 400."""
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/contacts",
            data={"email": ""},
        )
        assert resp.status_code == 400

    def test_add_contact_duplicate_email_returns_409(self, client, test_vendor_card, test_vendor_contact):
        """Lines 854-856: duplicate email → 409."""
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/contacts",
            data={"email": test_vendor_contact.email},
        )
        assert resp.status_code == 409


# ── Lines 897-918: vendor_contact_edit success ───────────────────────────────


class TestVendorContactEdit:
    """PUT /v2/partials/vendors/{id}/contacts/{cid} — field update paths."""

    def test_edit_contact_updates_full_name(self, client, db_session, test_vendor_card, test_vendor_contact):
        """Lines 897-918: full_name, title, phone submitted → contact updated."""
        with patch("app.routers.htmx._shared_tabs.template_response") as mock_tpl:
            mock_tpl.return_value = HTMLResponse("<tr>row</tr>")
            resp = client.put(
                f"/v2/partials/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}",
                data={
                    "full_name": "Updated Name",
                    "title": "Director",
                    "phone": "555-7777",
                },
            )
        assert resp.status_code == 200
        db_session.refresh(test_vendor_contact)
        assert test_vendor_contact.full_name == "Updated Name"
        assert test_vendor_contact.title == "Director"
        assert test_vendor_contact.phone == "555-7777"

    def test_edit_contact_email_change(self, client, db_session, test_vendor_card, test_vendor_contact):
        """Lines 907-911: email change (no collision) → email updated."""
        with patch("app.routers.htmx._shared_tabs.template_response") as mock_tpl:
            mock_tpl.return_value = HTMLResponse("<tr>row</tr>")
            resp = client.put(
                f"/v2/partials/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}",
                data={"email": "changed@arrow.com"},
            )
        assert resp.status_code == 200
        db_session.refresh(test_vendor_contact)
        assert test_vendor_contact.email == "changed@arrow.com"

    def test_edit_contact_not_found_returns_404(self, client, test_vendor_card):
        """Lines 892-894: contact_id not found → 404."""
        resp = client.put(
            f"/v2/partials/vendors/{test_vendor_card.id}/contacts/999999",
            data={"full_name": "Ghost"},
        )
        assert resp.status_code == 404


# ── Line 999: vendor_claim error path ────────────────────────────────────────


class TestVendorClaimError:
    """POST /v2/partials/vendors/{id}/claim — error path (line 999)."""

    def test_claim_failure_returns_400(self, client, test_vendor_card):
        """Line 999: claim_vendor returns error string → 400.

        claim_vendor is lazily imported inside vendor_claim, so patch at the
        source module (app.services.strategic_vendor_service).
        """
        with patch(
            "app.services.strategic_vendor_service.claim_vendor",
            return_value=(None, "Already claimed by another user"),
        ):
            resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/claim")
        assert resp.status_code == 400


# ── Lines 1076-1092: vendor_add_custom_field success ─────────────────────────


class TestVendorAddCustomField:
    """POST /v2/partials/vendors/{id}/custom-fields — success path."""

    def test_add_custom_field_stores_and_renders(self, client, db_session, test_vendor_card):
        """Lines 1076-1092: label+value submitted → custom_fields updated."""
        with patch("app.routers.htmx._shared_tabs.template_response") as mock_tpl:
            mock_tpl.return_value = HTMLResponse("<div>custom fields</div>")
            resp = client.post(
                f"/v2/partials/vendors/{test_vendor_card.id}/custom-fields",
                data={"label": "Contract Tier", "value": "Gold"},
            )
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert test_vendor_card.custom_fields is not None
        assert test_vendor_card.custom_fields.get("Contract Tier") == "Gold"

    def test_add_custom_field_missing_label_returns_400(self, client, test_vendor_card):
        """Lines 1078-1079: blank label → 400."""
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/custom-fields",
            data={"label": "", "value": "Gold"},
        )
        assert resp.status_code == 400

    def test_add_custom_field_vendor_not_found_returns_404(self, client):
        """Lines 1072-1073: vendor doesn't exist → 404."""
        resp = client.post(
            "/v2/partials/vendors/999999/custom-fields",
            data={"label": "Tier", "value": "Gold"},
        )
        assert resp.status_code == 404


# ── Line 1116: vendor_delete_custom_field vendor not found ───────────────────


class TestVendorDeleteCustomField:
    """DELETE /v2/partials/vendors/{id}/custom-fields/{label} — 404 path."""

    def test_delete_custom_field_vendor_not_found(self, client):
        """Line 1116: vendor doesn't exist → 404."""
        resp = client.delete("/v2/partials/vendors/999999/custom-fields/SomeLabel")
        assert resp.status_code == 404

    def test_delete_custom_field_success(self, client, db_session, test_vendor_card):
        """Lines 1118-1125: existing field removed, partial re-rendered."""
        # Seed a custom field first
        test_vendor_card.custom_fields = {"RemoveMe": "value"}
        db_session.commit()

        with patch("app.routers.htmx._shared_tabs.template_response") as mock_tpl:
            mock_tpl.return_value = HTMLResponse("<div>fields</div>")
            resp = client.delete(f"/v2/partials/vendors/{test_vendor_card.id}/custom-fields/RemoveMe")
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert "RemoveMe" not in (test_vendor_card.custom_fields or {})


# ── Lines 1197-1213: add_vendor_review success ───────────────────────────────


class TestAddVendorReview:
    """POST /v2/partials/vendors/{id}/reviews — success path."""

    def test_add_review_creates_review_record(self, client, db_session, test_vendor_card):
        """Lines 1197-1213: valid rating + comment → VendorReview created, reviews returned."""
        from app.models import VendorReview

        with patch("app.routers.htmx._shared_tabs.template_response") as mock_tpl:
            mock_tpl.return_value = HTMLResponse("<div>reviews</div>")
            resp = client.post(
                f"/v2/partials/vendors/{test_vendor_card.id}/reviews",
                data={"rating": "4", "comment": "Great response time"},
            )
        assert resp.status_code == 200

        review = db_session.query(VendorReview).filter_by(vendor_card_id=test_vendor_card.id).first()
        assert review is not None
        assert review.rating == 4
        assert review.comment == "Great response time"

    def test_add_review_invalid_rating_defaults_to_3(self, client, db_session, test_vendor_card):
        """Lines 1197-1200: non-integer rating → defaults to 3."""
        from app.models import VendorReview

        with patch("app.routers.htmx._shared_tabs.template_response") as mock_tpl:
            mock_tpl.return_value = HTMLResponse("<div>reviews</div>")
            resp = client.post(
                f"/v2/partials/vendors/{test_vendor_card.id}/reviews",
                data={"rating": "not-a-number", "comment": ""},
            )
        assert resp.status_code == 200

        review = db_session.query(VendorReview).filter_by(vendor_card_id=test_vendor_card.id).first()
        assert review is not None
        assert review.rating == 3

    def test_add_review_vendor_not_found_returns_404(self, client):
        """vendor_id not found → 404 before review is created."""
        resp = client.post(
            "/v2/partials/vendors/999999/reviews",
            data={"rating": "5", "comment": "Excellent"},
        )
        assert resp.status_code == 404
