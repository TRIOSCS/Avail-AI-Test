"""tests/test_htmx_views_nightly31.py — Coverage boost for htmx_views.py missing lines.

Targets the following previously uncovered lines in app/routers/htmx_views.py:
  - 202       v2_page: 403 for non-admin accessing trouble-tickets
  - 211-216   v2_page: module access gate redirect/deny
  - 233       v2_page: trouble-tickets partial URL for admin
  - 241       v2_page: my-day partial URL
  - 247-248   v2_page: search/results path with ?q=
  - 297       v2_page: customers/{id}?tab= partial URL injection
  - 388-396   requisition_activity_digest body
  - 408-420   customer_activity_digest body
  - 436,438-466  requisitions_bulk_action (try/except, invalid action, assign success)
  - 505       requisition_inline_edit_cell not-found branch
  - 530       requisition_inline_save not-found branch
  - 545-546   requisition_inline_save status ValueError handler
  - 556       requisition_inline_save owner 403 for non-manager

Called by: pytest autodiscovery
Depends on: conftest.py fixtures (client, db_session, test_user, admin_user, manager_user,
            test_company), app.routers.htmx_views
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import RequisitionStatus
from app.models import Company, Requisition, User

HX = {"HX-Request": "true"}


# ── Local helpers ─────────────────────────────────────────────────────────────


def _req(db: Session, user: User, **kw) -> Requisition:
    defaults = dict(
        name="N31-REQ",
        customer_name="N31 Corp",
        status=RequisitionStatus.OPEN,
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    obj = Requisition(**defaults)
    db.add(obj)
    db.flush()
    return obj


@pytest.fixture()
def _manager_client(db_session: Session, manager_user: User) -> TestClient:
    """TestClient authenticated as manager_user for bulk-assign tests."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    def _db():
        yield db_session

    def _user():
        return manager_user

    async def _token():
        return "mock-token"

    overridden = [get_db, require_user, require_admin, require_buyer, require_fresh_token]
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = _user
    app.dependency_overrides[require_admin] = _user
    app.dependency_overrides[require_buyer] = _user
    app.dependency_overrides[require_fresh_token] = _token

    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in overridden:
            app.dependency_overrides.pop(dep, None)


# ── v2_page: trouble-tickets gate (lines 201-202, 233) ───────────────────────


class TestV2PageTroubleTickets:
    """Line 202: HTTPException(403) for non-admin; line 233: partial URL set."""

    def test_non_admin_trouble_tickets_gets_403(self, client: TestClient, test_user: User):
        """Line 202: buyer role is rejected before page shell renders."""
        with patch("app.routers.htmx_views.get_user", return_value=test_user):
            resp = client.get("/v2/trouble-tickets")
        assert resp.status_code == 403

    def test_admin_trouble_tickets_gets_200(self, client: TestClient, admin_user: User):
        """Line 233: admin passes line 201 check; partial_url set to trouble-tickets/workspace."""
        with patch("app.routers.htmx_views.get_user", return_value=admin_user):
            resp = client.get("/v2/trouble-tickets")
        assert resp.status_code == 200


# ── v2_page: module access redirect / deny-all (lines 211-216) ───────────────


class TestV2PageModuleGate:
    """Lines 211-216: access gate redirects to first allowed module or returns 403."""

    def test_denied_module_redirects_to_first_allowed(self, client: TestClient, db_session: Session):
        """Lines 211-215: buyer with requisitions denied redirects (302) to next module."""
        user = User(
            email="limited31@trioscs.com",
            name="Limited31",
            role="buyer",
            azure_id="azure-limited31",
            access_overrides={"requisitions": False},
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        with patch("app.routers.htmx_views.get_user", return_value=user):
            resp = client.get("/v2/requisitions", follow_redirects=False)
        # Buyer still has other module access → redirect to first allowed
        assert resp.status_code == 302

    def test_agent_user_denied_all_modules_gets_403(self, client: TestClient, db_session: Session):
        """Lines 211, 213-214, 216-219: agent has no module defaults → 403 HTML."""
        agent = User(
            email="agent31@trioscs.com",
            name="Agent31",
            role="agent",
            azure_id="azure-agent31",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(agent)
        db_session.commit()
        db_session.refresh(agent)

        with patch("app.routers.htmx_views.get_user", return_value=agent):
            resp = client.get("/v2/requisitions")
        assert resp.status_code == 403
        assert b"don" in resp.content  # "You don't have access..."


# ── v2_page: my-day partial URL (line 241) ───────────────────────────────────


class TestV2PageMyDay:
    """Line 241: partial_url = /v2/partials/my-day."""

    def test_my_day_full_page_returns_200(self, client: TestClient, test_user: User):
        """Line 241: my-day sets partial_url; buyer has MY_DAY access by default."""
        with patch("app.routers.htmx_views.get_user", return_value=test_user):
            resp = client.get("/v2/my-day")
        assert resp.status_code == 200


# ── v2_page: search partial URL (current file lines 235-239) ─────────────────


class TestV2PageSearch:
    """v2_page search branch: partial_url set from ?mpn= param."""

    def test_search_page_with_mpn_param(self, client: TestClient, test_user: User):
        """Lines 238-239: /v2/search?mpn=LM317T builds partial_url with mpn param."""
        with patch("app.routers.htmx_views.get_user", return_value=test_user):
            resp = client.get("/v2/search?mpn=LM317T")
        assert resp.status_code == 200

    def test_search_results_partial_returns_200(self, client: TestClient):
        """Lines 352-356: /v2/partials/search/results uses require_user (overridden)."""
        from unittest.mock import patch as _patch

        with _patch(
            "app.services.global_search_service.fast_search",
            return_value={"best_match": None, "groups": {}, "total_count": 0},
        ):
            resp = client.get("/v2/partials/search/results?q=LM317T")
        assert resp.status_code == 200


# ── v2_page: customers/{id}?tab= partial URL injection (line 297) ─────────────


class TestV2PageCustomerDetailTab:
    """Line 297: tab param threaded into partial_url for customer detail deep-links."""

    def test_customer_detail_with_tab_returns_200(
        self, client: TestClient, db_session: Session, test_user: User, test_company: Company
    ):
        """Line 297: /v2/customers/{id}?tab=quotes appends tab to partial_url."""
        with patch("app.routers.htmx_views.get_user", return_value=test_user):
            resp = client.get(f"/v2/customers/{test_company.id}?tab=quotes")
        assert resp.status_code == 200


# ── Requisition activity digest (lines 388-396) ───────────────────────────────


class TestRequisitionActivityDigest:
    """Lines 388-396: GET /v2/partials/requisitions/{req_id}/activity-digest."""

    def test_digest_returns_200(self, client: TestClient, db_session: Session, test_user: User):
        """Lines 391-396: builds digest with mocked service, renders template."""
        req = _req(db_session, test_user)
        db_session.commit()

        mock_digest = {"state": "insufficient"}
        with patch(
            "app.services.activity_digest_service.get_or_build_digest",
            new=AsyncMock(return_value=mock_digest),
        ):
            resp = client.get(f"/v2/partials/requisitions/{req.id}/activity-digest", headers=HX)
        assert resp.status_code == 200

    def test_digest_unknown_req_returns_404(self, client: TestClient):
        """get_requisition_or_404 raises 404 for unknown req_id."""
        resp = client.get("/v2/partials/requisitions/999999/activity-digest", headers=HX)
        assert resp.status_code == 404


# ── Customer activity digest (lines 408-420) ──────────────────────────────────


class TestCustomerActivityDigest:
    """Lines 408-420: GET /v2/partials/customers/{company_id}/activity-digest."""

    def test_digest_returns_200(self, client: TestClient, test_company: Company):
        """Lines 410-420: company found, digest built with mocked service."""
        mock_digest = {"state": "insufficient"}
        with patch(
            "app.services.activity_digest_service.get_or_build_digest",
            new=AsyncMock(return_value=mock_digest),
        ):
            resp = client.get(f"/v2/partials/customers/{test_company.id}/activity-digest", headers=HX)
        assert resp.status_code == 200

    def test_digest_unknown_company_returns_404(self, client: TestClient):
        """Line 411: HTTPException(404) when company_id not in DB."""
        resp = client.get("/v2/partials/customers/999999/activity-digest", headers=HX)
        assert resp.status_code == 404


# ── Bulk requisition action (lines 436, 438-466) ──────────────────────────────


class TestRequisitionsBulkAction:
    """POST /v2/partials/requisitions/bulk/{action} — try/except, validation, assign."""

    def test_invalid_id_format_returns_400(self, client: TestClient):
        """Lines 436, 438-439: non-integer ids_str hits ValueError → 400."""
        resp = client.post(
            "/v2/partials/requisitions/bulk/assign",
            data={"ids": "abc,def"},
        )
        assert resp.status_code == 400

    def test_invalid_action_returns_400(self, client: TestClient, db_session: Session, test_user: User):
        """Line 446: action='delete' not in valid_actions → HTTPException 400."""
        req = _req(db_session, test_user)
        db_session.commit()
        resp = client.post(
            "/v2/partials/requisitions/bulk/delete",
            data={"ids": str(req.id)},
        )
        assert resp.status_code == 400

    def test_assign_by_manager_returns_200(
        self,
        _manager_client: TestClient,
        db_session: Session,
        test_user: User,
        manager_user: User,
    ):
        """Lines 452-466: manager assigns owner; requisitions_list_partial mocked."""
        req = _req(db_session, manager_user)
        db_session.commit()

        with patch(
            "app.routers.htmx_views.requisitions_list_partial",
            new=AsyncMock(return_value=HTMLResponse("<div>ok</div>")),
        ):
            resp = _manager_client.post(
                "/v2/partials/requisitions/bulk/assign",
                data={"ids": str(req.id), "owner_id": str(test_user.id)},
            )
        assert resp.status_code == 200


# ── Inline edit cell – invalid-field and not-found branches ──────────────────


class TestRequisitionInlineEditCell:
    """Lines 484, 488: invalid-field 400 and not-found 404 in inline edit cell."""

    def test_invalid_field_returns_400(self, client: TestClient):
        """Line 484: field not in valid_fields → HTMLResponse 400."""
        resp = client.get(
            "/v2/partials/requisitions/1/edit/bogusfield",
            headers=HX,
        )
        assert resp.status_code == 400

    def test_not_found_returns_404_html(self, client: TestClient):
        """Line 488: patch get_req_for_user → None → HTMLResponse 404."""
        with patch("app.routers.htmx_views.get_req_for_user", return_value=None):
            resp = client.get(
                "/v2/partials/requisitions/1/edit/name",
                headers=HX,
            )
        assert resp.status_code == 404


# ── Inline save – not-found and error branches (lines 530, 545-546, 556) ──────


class TestRequisitionInlineSave:
    """Lines 530, 545-546, 556: not-found, ValueError, and owner-403 paths."""

    def test_not_found_returns_404_html(self, client: TestClient):
        """Line 530: patch get_req_for_user → None → HTMLResponse 404."""
        with patch("app.routers.htmx_views.get_req_for_user", return_value=None):
            resp = client.patch(
                "/v2/partials/requisitions/1/inline",
                data={"field": "name", "value": "New Name"},
            )
        assert resp.status_code == 404

    def test_status_value_error_is_caught(self, client: TestClient, db_session: Session, test_user: User):
        """Lines 545-546: invalid status transition raises ValueError → caught, msg set."""
        req = _req(db_session, test_user)
        db_session.commit()

        with patch(
            "app.services.requisition_state.transition",
            side_effect=ValueError("Cannot transition"),
        ):
            resp = client.patch(
                f"/v2/partials/requisitions/{req.id}/inline",
                data={"field": "status", "value": "closed"},
            )
        # ValueError is caught; endpoint returns the row partial (200)
        assert resp.status_code == 200

    def test_owner_field_by_buyer_returns_403(self, client: TestClient, db_session: Session, test_user: User):
        """Line 556: buyer (non-manager) trying to change owner → HTTPException 403."""
        req = _req(db_session, test_user)
        db_session.commit()

        resp = client.patch(
            f"/v2/partials/requisitions/{req.id}/inline",
            data={"field": "owner", "value": str(test_user.id)},
        )
        assert resp.status_code == 403
