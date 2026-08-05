"""test_htmx_views_deep.py — Deep coverage tests for app/routers/htmx_views.py.

Targets uncovered routes: buy-plans, sourcing, materials, quotes, prospecting,
settings, proactive, and v2_page path variants.

Called by: pytest
Depends on: conftest.py (client, db_session, test_user, test_requisition)
"""

import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import (
    BuyPlanStatus,
    QuoteStatus,
    RequisitionStatus,
    SourcingStatus,
)
from app.models import (
    BuyPlan,
    Company,
    Quote,
    Requirement,
    Requisition,
    User,
    VendorCard,
)

# ── Helpers ───────────────────────────────────────────────────────────────


def _requisition(db: Session, user: User, **kw) -> Requisition:
    r = Requisition(
        name="REQ-DEEP",
        customer_name="Deep Corp",
        status=RequisitionStatus.OPEN,
        created_by=user.id,
        created_at=datetime.now(UTC),
        **kw,
    )
    db.add(r)
    db.flush()
    return r


def _requirement(db: Session, req: Requisition, mpn: str = "LM317T", **kw) -> Requirement:
    r = Requirement(
        requisition_id=req.id,
        primary_mpn=mpn,
        target_qty=100,
        sourcing_status=SourcingStatus.OPEN,
        created_at=datetime.now(UTC),
        **kw,
    )
    db.add(r)
    db.flush()
    return r


def _quote(db: Session, req: Requisition, user: User, **kw) -> Quote:
    q = Quote(
        requisition_id=req.id,
        quote_number=f"Q-DEEP-{req.id}",
        status=QuoteStatus.DRAFT,
        created_by_id=user.id,
        created_at=datetime.now(UTC),
        **kw,
    )
    db.add(q)
    db.flush()
    return q


def _buy_plan(db: Session, quote: Quote, user: User, **kw) -> BuyPlan:
    bp = BuyPlan(
        quote_id=quote.id,
        requisition_id=quote.requisition_id,
        status=BuyPlanStatus.PENDING,
        submitted_by_id=user.id,
        total_cost=100.0,
        created_at=datetime.now(UTC),
        **kw,
    )
    db.add(bp)
    db.flush()
    return bp


def _vendor_card(db: Session, **kw) -> VendorCard:
    vc = VendorCard(
        normalized_name="deep vendor",
        display_name="Deep Vendor",
        emails=[],
        phones=[],
        created_at=datetime.now(UTC),
        **kw,
    )
    db.add(vc)
    db.flush()
    return vc


def _company(db: Session, **kw) -> Company:
    co = Company(
        name="Deep Co",
        is_active=True,
        created_at=datetime.now(UTC),
        **kw,
    )
    db.add(co)
    db.flush()
    return co


# ══════════════════════════════════════════════════════════════════════════
# v2_page path variant coverage (lines 181-262)
# ══════════════════════════════════════════════════════════════════════════


class TestV2PagePathVariants:
    """Test v2_page routes — must mock get_user since v2_page uses session, not
    require_user."""

    def _get(self, client: TestClient, path: str, test_user: User) -> int:
        with patch("app.routers.htmx_views.get_user", return_value=test_user):
            resp = client.get(path)
        return resp.status_code

    @pytest.mark.parametrize(
        "path",
        [
            "/v2/buy-plans",
            "/v2/resell",
            "/v2/quotes",
            "/v2/prospecting",
            "/v2/proactive",
            "/v2/settings",
            "/v2/materials",
            "/v2/follow-ups",
            # NB: /v2/trouble-tickets is admin-only (the management console) — its
            # gating is covered in test_ticket_diagnosis.py::TestAdminGating.
            "/v2/search",
            "/v2/crm",
            "/v2/sightings",
            "/v2/materials/1",
            "/v2/prospecting/1",
            "/v2/resell/1",
            "/v2/prospecting/5",
        ],
    )
    def test_v2_static_paths_ok(self, client: TestClient, test_user: User, path: str):
        assert self._get(client, path, test_user) == 200

    def test_v2_vendors_id(self, client: TestClient, db_session: Session, test_user: User):
        vc = _vendor_card(db_session)
        db_session.commit()
        assert self._get(client, f"/v2/vendors/{vc.id}", test_user) == 200

    def test_v2_customers_id(self, client: TestClient, db_session: Session, test_user: User):
        co = _company(db_session)
        db_session.commit()
        assert self._get(client, f"/v2/customers/{co.id}", test_user) == 200

    def test_v2_buy_plans_id(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        q = _quote(db_session, req, test_user)
        db_session.commit()
        bp = _buy_plan(db_session, q, test_user)
        db_session.commit()
        assert self._get(client, f"/v2/buy-plans/{bp.id}", test_user) == 200

    def test_v2_quotes_id(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        q = _quote(db_session, req, test_user)
        db_session.commit()
        assert self._get(client, f"/v2/quotes/{q.id}", test_user) == 200

    def test_v2_unauthenticated_returns_login(self, unauthenticated_client: TestClient):
        with patch("app.routers.htmx_views.get_user", return_value=None):
            resp = unauthenticated_client.get("/v2/buy-plans")
        assert resp.status_code == 200  # login page is 200


# ══════════════════════════════════════════════════════════════════════════
# Buy-plans list and detail (lines 5863-6240)
# ══════════════════════════════════════════════════════════════════════════


class TestBuyPlansRoutes:
    @pytest.mark.parametrize(
        "path",
        [
            "/v2/partials/buy-plans",  # retired hub shell
            "/v2/partials/buy-plans?lens=deals",  # any old lens value redirects too
        ],
    )
    def test_buy_plans_list_308s_to_workspace(self, client: TestClient, path: str):
        # The hub shell retired (spec §11.1) — 308 onto the workspace shell, which
        # renders its own lazy body container + explicit hx-target after the redirect.
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 308
        assert resp.headers["location"] == "/v2/partials/approvals?tab=buy-plans"
        followed = client.get(path, follow_redirects=True)
        assert followed.status_code == 200
        assert 'id="ap-hub-body"' in followed.text
        assert 'hx-target="#ap-hub-body"' in followed.text

    def test_buy_plan_detail_404(self, client: TestClient):
        resp = client.get("/v2/partials/buy-plans/99999")
        assert resp.status_code == 404

    def test_buy_plan_detail_exists(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        q = _quote(db_session, req, test_user)
        db_session.commit()
        bp = _buy_plan(db_session, q, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/buy-plans/{bp.id}")
        assert resp.status_code == 200

    def test_buy_plan_submit_missing_so(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        q = _quote(db_session, req, test_user)
        db_session.commit()
        bp = _buy_plan(db_session, q, test_user)
        db_session.commit()
        resp = client.post(f"/v2/partials/buy-plans/{bp.id}/submit", data={})
        assert resp.status_code == 400

    @pytest.mark.parametrize("action", ["cancel", "reset"])
    def test_buy_plan_post_missing_404(self, client: TestClient, action: str):
        resp = client.post(f"/v2/partials/buy-plans/99999/{action}", data={})
        assert resp.status_code in (404, 400, 422)


# ══════════════════════════════════════════════════════════════════════════
# Sourcing routes (lines 6343+)
# ══════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════
# Materials routes (lines 6965+)
# ══════════════════════════════════════════════════════════════════════════


class TestMaterialsRoutes:
    @pytest.mark.parametrize(
        "path",
        [
            "/v2/partials/materials",
            "/v2/partials/materials/workspace",
            "/v2/partials/materials/filters/manufacturers",
            "/v2/partials/materials/filters/manufacturers?commodity=resistors",
            "/v2/partials/manufacturers/search",
            "/v2/partials/manufacturers/search?q=Texas",
            "/v2/partials/materials/filters/tree",
            "/v2/partials/materials/filters/sub",
            "/v2/partials/materials/ai-interpret",
            "/v2/partials/materials/faceted",
        ],
    )
    def test_materials_get_ok(self, client: TestClient, path: str):
        assert client.get(path).status_code == 200

    @pytest.mark.parametrize(
        "path",
        [
            "/v2/partials/materials/99999",
            "/v2/partials/materials/99999/insights",
        ],
    )
    def test_materials_get_404(self, client: TestClient, path: str):
        assert client.get(path).status_code == 404

    def test_manufacturer_add_empty(self, client: TestClient):
        resp = client.post("/v2/partials/manufacturers/add", data={})
        assert resp.status_code in (200, 400, 422)


# ══════════════════════════════════════════════════════════════════════════
# Quotes routes (lines 7386+)
# ══════════════════════════════════════════════════════════════════════════


class TestQuotesRoutes:
    @pytest.mark.parametrize(
        "path",
        [
            # /v2/partials/quotes (standalone list) was retired — see test_quotes_relocation.py
            "/v2/partials/quotes/recent-terms",
            "/v2/partials/pricing-history/LM317T",
        ],
    )
    def test_quotes_get_ok(self, client: TestClient, path: str):
        assert client.get(path).status_code == 200

    def test_quote_detail_404(self, client: TestClient):
        resp = client.get("/v2/partials/quotes/99999")
        assert resp.status_code == 404

    def test_quote_detail_exists(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        q = _quote(db_session, req, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/quotes/{q.id}")
        assert resp.status_code == 200

    def test_quote_delete_404(self, client: TestClient):
        resp = client.delete("/v2/partials/quotes/99999")
        assert resp.status_code == 404

    @pytest.mark.parametrize(
        "path",
        [
            "/v2/partials/quotes/99999/reopen",
            "/v2/partials/quotes/99999/preview",
            "/v2/partials/quotes/99999/send",
        ],
    )
    def test_quote_post_404(self, client: TestClient, path: str):
        assert client.post(path, data={}).status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# Prospecting routes (lines 7777+)
# ══════════════════════════════════════════════════════════════════════════


class TestProspectingRoutes:
    @pytest.mark.parametrize(
        "path",
        [
            "/v2/partials/prospecting",
            "/v2/partials/prospecting?q=acme&sort=fit_desc",
            "/v2/partials/prospecting?sort=recent_desc",
            "/v2/partials/prospecting/stats",
        ],
    )
    def test_prospecting_get_ok(self, client: TestClient, path: str):
        assert client.get(path).status_code == 200

    def test_prospecting_add_domain_empty(self, client: TestClient):
        # Empty domain returns an inline error chip (200), not a 400.
        resp = client.post("/v2/partials/prospecting/add-domain", data={})
        assert resp.status_code == 200
        assert "domain" in resp.text.lower()

    def test_prospecting_add_domain_valid(self, client: TestClient):
        with patch("app.services.prospect_claim.add_prospect_manually") as mock_add:
            mock_prospect = MagicMock()
            mock_prospect.id = 42
            mock_add.return_value = mock_prospect
            resp = client.post("/v2/partials/prospecting/add-domain", data={"domain": "example.com"})
        assert resp.status_code == 200

    def test_prospecting_detail_404(self, client: TestClient):
        resp = client.get("/v2/partials/prospecting/99999")
        assert resp.status_code == 404

    def test_prospecting_claim_404(self, client: TestClient):
        # claim_prospect raises LookupError for a missing prospect → 404
        resp = client.post("/v2/partials/prospecting/99999/claim")
        assert resp.status_code == 404

    @pytest.mark.parametrize("action", ["dismiss", "enrich"])
    def test_prospecting_post_404(self, client: TestClient, action: str):
        resp = client.post(f"/v2/partials/prospecting/99999/{action}")
        assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# Settings routes (lines 7983+)
# ══════════════════════════════════════════════════════════════════════════


class TestSettingsRoutes:
    @pytest.mark.parametrize(
        "path",
        [
            "/v2/partials/settings",
            "/v2/partials/settings?tab=profile",
            "/v2/partials/settings/profile",
        ],
    )
    def test_settings_get_ok(self, client: TestClient, path: str):
        assert client.get(path).status_code == 200

    @pytest.mark.parametrize(
        "path",
        [
            "/v2/partials/settings/sources",
            "/v2/partials/settings/api-keys",
        ],
    )
    def test_settings_old_tabs_redirect_to_connectors(self, client: TestClient, path: str):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 302
        assert "/connectors" in resp.headers["location"]

    def test_settings_connectors_non_admin_returns_403(self, client: TestClient):
        # connectors tab is admin-only; buyer role → 403
        resp = client.get("/v2/partials/settings/connectors")
        assert resp.status_code == 403

    def test_settings_system_non_admin(self, client: TestClient):
        # test_user is a 'buyer' — should get 403
        resp = client.get("/v2/partials/settings/system")
        assert resp.status_code == 403

    def test_settings_system_admin(self, db_session: Session, admin_user: User):
        from app.database import get_db
        from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
        from app.main import app

        def _db():
            yield db_session

        def _user():
            return admin_user

        with patch("app.services.admin_service.get_all_config", return_value={}):
            app.dependency_overrides[get_db] = _db
            app.dependency_overrides[require_user] = _user
            app.dependency_overrides[require_admin] = _user
            app.dependency_overrides[require_buyer] = _user
            app.dependency_overrides[require_fresh_token] = lambda: "tok"
            try:
                with TestClient(app) as c:
                    resp = c.get("/v2/partials/settings/system")
                assert resp.status_code == 200
            finally:
                for dep in [get_db, require_user, require_admin, require_buyer, require_fresh_token]:
                    app.dependency_overrides.pop(dep, None)

    def test_toggle_8x8(self, client: TestClient):
        resp = client.post("/api/user/toggle-8x8")
        assert resp.status_code == 200

    def test_settings_data_ops_non_admin(self, client: TestClient):
        resp = client.get("/v2/partials/settings/data-ops")
        assert resp.status_code == 403


# ══════════════════════════════════════════════════════════════════════════
# Proactive routes (lines 8141+)
# ══════════════════════════════════════════════════════════════════════════


class TestProactiveRoutes:
    def test_proactive_list(self, client: TestClient):
        with patch(
            "app.services.proactive_service.get_matches_for_user", return_value={"groups": [], "stats": {"total": 0}}
        ):
            resp = client.get("/v2/partials/proactive")
        assert resp.status_code == 200

    def test_proactive_list_sent_tab(self, client: TestClient):
        with patch(
            "app.services.proactive_service.get_matches_for_user", return_value={"groups": [], "stats": {"total": 0}}
        ):
            with patch("app.services.proactive_service.get_sent_offers", return_value=[]):
                resp = client.get("/v2/partials/proactive?tab=sent")
        assert resp.status_code == 200

    def test_proactive_scorecard(self, client: TestClient):
        with patch(
            "app.services.proactive_service.get_scorecard", return_value={"total_sent": 0, "total_converted": 0}
        ):
            resp = client.get("/v2/partials/proactive/scorecard")
        assert resp.status_code == 200

    def test_proactive_batch_dismiss_empty(self, client: TestClient):
        with patch(
            "app.services.proactive_service.get_matches_for_user", return_value={"groups": [], "stats": {"total": 0}}
        ):
            resp = client.post("/v2/partials/proactive/batch-dismiss", data={})
        assert resp.status_code == 200

    def test_proactive_do_not_offer_missing_params(self, client: TestClient):
        resp = client.post("/v2/partials/proactive/do-not-offer", data={})
        assert resp.status_code == 400

    def test_proactive_do_not_offer_invalid_company(self, client: TestClient):
        resp = client.post("/v2/partials/proactive/do-not-offer", data={"mpn": "LM317T", "company_id": "bad"})
        assert resp.status_code == 400

    def test_proactive_do_not_offer_valid(self, client: TestClient, db_session: Session, test_user: User):
        co = _company(db_session)
        co.account_owner_id = test_user.id  # actor must manage the account (authz gate)
        db_session.commit()
        with patch("app.services.proactive_helpers.is_do_not_offer", return_value=True):
            resp = client.post(
                "/v2/partials/proactive/do-not-offer",
                data={"mpn": "LM317T", "company_id": str(co.id)},
            )
        assert resp.status_code == 200

    def test_proactive_prepare_no_matches(self, client: TestClient):
        resp = client.post("/v2/proactive/prepare/1", data={})
        # Returns redirect 303 when no match_ids
        assert resp.status_code in (200, 303)

    def test_proactive_send_no_matches(self, client: TestClient):
        resp = client.post("/v2/proactive/send", data={})
        assert resp.status_code == 400

    def test_proactive_send_no_contacts(self, client: TestClient):
        resp = client.post("/v2/proactive/send", data={"match_ids": "1"})
        assert resp.status_code == 400

    def test_proactive_convert_404(self, client: TestClient):
        resp = client.post("/v2/partials/proactive/99999/convert")
        assert resp.status_code == 404

    def test_proactive_draft_no_matches(self, client: TestClient):
        resp = client.post("/v2/partials/proactive/draft", data={})
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Admin merge routes (lines 8088+)
# ══════════════════════════════════════════════════════════════════════════


class TestAdminMergeRoutes:
    @pytest.mark.parametrize(
        "path",
        ["/v2/partials/admin/vendor-merge", "/v2/partials/admin/company-merge"],
    )
    def test_merge_non_admin_403(self, client: TestClient, path: str):
        # buyer user → 403
        resp = client.post(path, data={"keep_id": "1", "remove_id": "2"})
        assert resp.status_code == 403


# ══════════════════════════════════════════════════════════════════════════
# Insights routes (lines 5729+)
# ══════════════════════════════════════════════════════════════════════════


class TestInsightsRoutes:
    @pytest.mark.parametrize(
        "path",
        [
            # Returns 200 even for non-existent IDs (renders "no insights" state)
            "/v2/partials/requisitions/99999/insights",
            "/v2/partials/vendors/99999/insights",
            "/v2/partials/customers/99999/insights",
        ],
    )
    def test_insights_any_id_ok(self, client: TestClient, path: str):
        assert client.get(path).status_code == 200

    def test_requisition_insights_exists(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/insights")
        assert resp.status_code == 200

    def test_vendor_insights_exists(self, client: TestClient, db_session: Session):
        vc = _vendor_card(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/vendors/{vc.id}/insights")
        assert resp.status_code == 200

    def test_customer_insights_exists(self, client: TestClient, db_session: Session):
        co = _company(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{co.id}/insights")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Email / thread routes (lines 5556+)
# ══════════════════════════════════════════════════════════════════════════


class TestEmailRoutes:
    def test_email_thread_404(self, client: TestClient):
        resp = client.get("/v2/partials/emails/thread/nonexistent-id")
        assert resp.status_code in (200, 404, 400)


# ══════════════════════════════════════════════════════════════════════════
# Dashboard route (line 5688)
# ══════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════
# Requisition tab routes (lines 1247-1356) — covers all 7 tabs
# ══════════════════════════════════════════════════════════════════════════


class TestRequisitionTabs:
    @pytest.mark.parametrize(
        "tab",
        ["parts", "offers", "quotes", "buy_plans", "tasks", "activity", "responses"],
    )
    def test_tab_ok(self, client: TestClient, db_session: Session, test_user: User, tab: str):
        req = _requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/{tab}")
        assert resp.status_code == 200

    def test_tab_unknown_404(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/unknown")
        assert resp.status_code == 404

    def test_tab_404_req(self, client: TestClient):
        resp = client.get("/v2/partials/requisitions/99999/tab/parts")
        assert resp.status_code == 404


# (TestParseForms left with the two-doors collapse, spec §5.1 — the surviving
# Add-offer paste box is covered in tests/test_offer_doors.py.)


# ══════════════════════════════════════════════════════════════════════════
# Offer routes (lines 1974-2345)
# ══════════════════════════════════════════════════════════════════════════


class TestOfferRoutes:
    def test_add_offer_form(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        db_session.commit()
        # Paste-box gating pinned off (DB credential lookup) — see test_offer_doors.py.
        with patch("app.routers.htmx.offers.crud.claude_configured", return_value=False):
            resp = client.get(f"/v2/partials/requisitions/{req.id}/add-offer-form")
        assert resp.status_code == 200

    def test_add_offer_form_404(self, client: TestClient):
        resp = client.get("/v2/partials/requisitions/99999/add-offer-form")
        assert resp.status_code == 404

    def test_offer_changelog_404(self, client: TestClient):
        resp = client.get("/v2/partials/offers/99999/changelog")
        assert resp.status_code == 404

    def test_offer_edit_form_404(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/offers/99999/edit-form")
        assert resp.status_code == 404

    def test_offer_delete_404(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        db_session.commit()
        resp = client.delete(f"/v2/partials/requisitions/{req.id}/offers/99999")
        assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# Requisition action routes (rfq-compose tests left with the W3 composer-A
# delete — the surviving vendor-modal composer is covered in
# test_sightings_router.py / test_routers_sightings.py)
# ══════════════════════════════════════════════════════════════════════════


class TestRfqRoutes:
    def test_detail_header_send_rfq_opens_vendor_modal(self, client: TestClient, db_session: Session, test_user: User):
        """W3 repoint: the deal header's Send-RFQ button opens the surviving
        sightings vendor-modal composer with every line id in the basket (no
        hx-get to the deleted rfq-compose route)."""
        req = _requisition(db_session, test_user)
        r1 = _requirement(db_session, req, mpn="LM317T")
        r2 = _requirement(db_session, req, mpn="NE555P")
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}")
        assert resp.status_code == 200
        assert f"/v2/partials/sightings/vendor-modal?requirement_ids={r1.id},{r2.id}" in resp.text
        assert "rfq-compose" not in resp.text

    def test_log_activity_404(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/99999/log-activity",
            data={"activity_type": "call", "notes": "test"},
        )
        assert resp.status_code == 404

    def test_action_invalid_returns_400(self, client: TestClient):
        # "close" not in valid_actions → 400
        resp = client.post("/v2/partials/requisitions/99999/action/close")
        assert resp.status_code == 400

    def test_action_valid_but_404(self, client: TestClient):
        # "clone" is a valid action but req 99999 doesn't exist → 404
        resp = client.post("/v2/partials/requisitions/99999/action/clone")
        assert resp.status_code == 404

    def test_log_phone_404(self, client: TestClient):
        resp = client.post("/v2/partials/requisitions/99999/log-phone", data={})
        assert resp.status_code == 404

    def test_poll_inbox_404(self, client: TestClient):
        resp = client.post("/v2/partials/requisitions/99999/poll-inbox")
        assert resp.status_code in (404, 400)

    def test_create_quote_404(self, client: TestClient):
        resp = client.post("/v2/partials/requisitions/99999/create-quote")
        assert resp.status_code in (404, 400)


# ══════════════════════════════════════════════════════════════════════════
# Follow-ups routes (lines 2647+)
# ══════════════════════════════════════════════════════════════════════════


class TestFollowUpRoutes:
    @pytest.mark.parametrize("path", ["/v2/partials/follow-ups", "/v2/partials/follow-ups?q=test"])
    def test_follow_ups_list_ok(self, client: TestClient, path: str):
        assert client.get(path).status_code == 200

    def test_follow_ups_send_batch_empty(self, client: TestClient):
        resp = client.post("/v2/partials/follow-ups/send-batch", data={})
        assert resp.status_code in (200, 400)

    def test_follow_ups_send_404(self, client: TestClient):
        resp = client.post("/v2/partials/follow-ups/99999/send", data={"body": "hi"})
        assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# Search routes (lines 2968-3300)
# ══════════════════════════════════════════════════════════════════════════


class TestSearchRoutes:
    @pytest.mark.parametrize(
        "path",
        [
            "/v2/partials/search",
            # search_id is required; missing results → returns "expired" message
            "/v2/partials/search/filter?search_id=nonexistent",
            "/v2/partials/search/requisition-picker",
            "/v2/partials/search/requisition-picker?q=REQ",
        ],
    )
    def test_search_get_ok(self, client: TestClient, path: str):
        assert client.get(path).status_code == 200

    def test_search_lead_detail_missing(self, client: TestClient):
        resp = client.get("/v2/partials/search/lead-detail?lead_id=99999")
        assert resp.status_code in (200, 404)


# ══════════════════════════════════════════════════════════════════════════
# Vendor routes (lines 3396-4100+)
# ══════════════════════════════════════════════════════════════════════════


class TestVendorDetailRoutes:
    @pytest.mark.parametrize(
        "suffix",
        ["edit-form", "reviews"],
    )
    def test_vendor_get_404(self, client: TestClient, suffix: str):
        resp = client.get(f"/v2/partials/vendors/99999/{suffix}")
        assert resp.status_code == 404

    @pytest.mark.parametrize(
        "suffix",
        [
            "edit-form",
            "reviews",
            "tab/contacts",
            "tab/overview",
            "tab/offers",
            "tab/emails",
        ],
    )
    def test_vendor_get_exists_ok(self, client: TestClient, db_session: Session, suffix: str):
        vc = _vendor_card(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/vendors/{vc.id}/{suffix}")
        assert resp.status_code == 200

    def test_vendor_tab_rfq_invalid(self, client: TestClient, db_session: Session):
        vc = _vendor_card(db_session)
        db_session.commit()
        # "rfq" not in valid_tabs → 404
        resp = client.get(f"/v2/partials/vendors/{vc.id}/tab/rfq")
        assert resp.status_code == 404

    def test_vendor_toggle_blacklist_404(self, client: TestClient):
        resp = client.post("/v2/partials/vendors/99999/toggle-blacklist")
        assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# Customer routes (lines 4292-5200)
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def _grant_account_management(test_user: User, db_session: Session) -> None:
    """Promote the buyer ``test_user`` to MANAGER so it can_manage every account.

    Company detail + tab partials (``GET /v2/partials/customers/{id}`` and
    ``.../tab/{tab}``) now gate on ``can_manage_account``. The class below GETs those
    endpoints as ``test_user`` on companies it creates without assigning ownership, so
    promote the actor to MANAGER (``can_manage_account`` is True for managers, exactly as
    for the account owner) to exercise the authorized render path. Applied per-class via
    ``@pytest.mark.usefixtures`` — scoped narrowly so role-based list tests are untouched.
    """
    test_user.role = "manager"
    db_session.commit()


@pytest.mark.usefixtures("_grant_account_management")
class TestCustomerRoutes:
    @pytest.mark.parametrize(
        "path",
        [
            "/v2/partials/customers",
            "/v2/partials/customers?q=acme",
            "/v2/partials/customers/create-form",
            "/v2/partials/customers/typeahead",
            "/v2/partials/customers/typeahead?q=acme",
            "/v2/partials/customers/check-duplicate?name=TestCo",
        ],
    )
    def test_customers_get_ok(self, client: TestClient, path: str):
        assert client.get(path).status_code == 200

    @pytest.mark.parametrize("suffix", ["", "/edit-form"])
    def test_customer_get_404(self, client: TestClient, suffix: str):
        resp = client.get(f"/v2/partials/customers/99999{suffix}")
        assert resp.status_code == 404

    @pytest.mark.parametrize("suffix", ["", "/edit-form"])
    def test_customer_get_exists_ok(self, client: TestClient, db_session: Session, suffix: str):
        co = _company(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{co.id}{suffix}")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Requisition edit and inline patch (lines 1682-1816)
# ══════════════════════════════════════════════════════════════════════════


class TestRequisitionEditRoutes:
    @pytest.mark.parametrize("field", ["name", "status"])
    def test_edit_field_ok(self, client: TestClient, db_session: Session, test_user: User, field: str):
        req = _requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/edit/{field}")
        assert resp.status_code == 200

    def test_edit_field_invalid_returns_400(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        db_session.commit()
        # "customer_name" not in valid_fields → 400
        resp = client.get(f"/v2/partials/requisitions/{req.id}/edit/customer_name")
        assert resp.status_code == 400

    def test_edit_field_404(self, client: TestClient):
        resp = client.get("/v2/partials/requisitions/99999/edit/name")
        assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# Bulk actions (lines 1620-1682)
# ══════════════════════════════════════════════════════════════════════════


class TestBulkActions:
    @pytest.mark.parametrize("action", ["close", "archive"])
    def test_bulk_action_empty(self, client: TestClient, action: str):
        resp = client.post(f"/v2/partials/requisitions/bulk/{action}", data={})
        assert resp.status_code in (200, 400)


@pytest.fixture(autouse=True)
def _proactive_on(proactive_flag_on):
    """Proactive routes 404 while parked (W2 park, spec §4/§8); this module exercises
    the flag-on behavior so the comeback path stays green."""
