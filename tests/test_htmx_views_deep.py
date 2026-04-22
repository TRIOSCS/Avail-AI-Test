"""test_htmx_views_deep.py — Deep coverage tests for app/routers/htmx_views.py.

Targets uncovered routes: buy-plans, sourcing, materials, quotes, prospecting,
settings, proactive, and v2_page path variants.

Called by: pytest
Depends on: conftest.py (client, db_session, test_user, test_requisition)
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import (
    BuyPlanStatus,
    OfferStatus,
    QuoteStatus,
    RequisitionStatus,
    SourcingStatus,
)
from app.models import (
    BuyPlan,
    Company,
    CustomerSite,
    Offer,
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
        status=RequisitionStatus.ACTIVE,
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
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
        created_at=datetime.now(timezone.utc),
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
        created_at=datetime.now(timezone.utc),
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
        created_at=datetime.now(timezone.utc),
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
        created_at=datetime.now(timezone.utc),
        **kw,
    )
    db.add(vc)
    db.flush()
    return vc


def _company(db: Session, **kw) -> Company:
    co = Company(
        name="Deep Co",
        is_active=True,
        created_at=datetime.now(timezone.utc),
        **kw,
    )
    db.add(co)
    db.flush()
    return co


# ══════════════════════════════════════════════════════════════════════════
# v2_page path variant coverage (lines 181-262)
# ══════════════════════════════════════════════════════════════════════════


class TestV2PagePathVariants:
    """Test v2_page routes — must mock get_user since v2_page uses session, not require_user."""

    def _get(self, client: TestClient, path: str, test_user: User) -> int:
        with patch("app.routers.htmx_views.get_user", return_value=test_user):
            resp = client.get(path)
        return resp.status_code

    def test_v2_buy_plans(self, client: TestClient, test_user: User):
        assert self._get(client, "/v2/buy-plans", test_user) == 200

    def test_v2_excess(self, client: TestClient, test_user: User):
        assert self._get(client, "/v2/excess", test_user) == 200

    def test_v2_quotes(self, client: TestClient, test_user: User):
        assert self._get(client, "/v2/quotes", test_user) == 200

    def test_v2_prospecting(self, client: TestClient, test_user: User):
        assert self._get(client, "/v2/prospecting", test_user) == 200

    def test_v2_proactive(self, client: TestClient, test_user: User):
        assert self._get(client, "/v2/proactive", test_user) == 200

    def test_v2_settings(self, client: TestClient, test_user: User):
        assert self._get(client, "/v2/settings", test_user) == 200

    def test_v2_materials(self, client: TestClient, test_user: User):
        assert self._get(client, "/v2/materials", test_user) == 200

    def test_v2_follow_ups(self, client: TestClient, test_user: User):
        assert self._get(client, "/v2/follow-ups", test_user) == 200

    def test_v2_trouble_tickets(self, client: TestClient, test_user: User):
        assert self._get(client, "/v2/trouble-tickets", test_user) == 200

    def test_v2_search(self, client: TestClient, test_user: User):
        assert self._get(client, "/v2/search", test_user) == 200

    def test_v2_crm(self, client: TestClient, test_user: User):
        assert self._get(client, "/v2/crm", test_user) == 200

    def test_v2_sightings(self, client: TestClient, test_user: User):
        assert self._get(client, "/v2/sightings", test_user) == 200

    def test_v2_vendors_id(self, client: TestClient, db_session: Session, test_user: User):
        vc = _vendor_card(db_session)
        db_session.commit()
        assert self._get(client, f"/v2/vendors/{vc.id}", test_user) == 200

    def test_v2_customers_id(self, client: TestClient, db_session: Session, test_user: User):
        co = _company(db_session)
        db_session.commit()
        assert self._get(client, f"/v2/customers/{co.id}", test_user) == 200

    def test_v2_materials_id(self, client: TestClient, test_user: User):
        assert self._get(client, "/v2/materials/1", test_user) == 200

    def test_v2_prospecting_id(self, client: TestClient, test_user: User):
        assert self._get(client, "/v2/prospecting/1", test_user) == 200

    def test_v2_trouble_tickets_id(self, client: TestClient, test_user: User):
        assert self._get(client, "/v2/trouble-tickets/1", test_user) == 200

    def test_v2_buy_plans_id(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        q = _quote(db_session, req, test_user)
        db_session.commit()
        bp = _buy_plan(db_session, q, test_user)
        db_session.commit()
        assert self._get(client, f"/v2/buy-plans/{bp.id}", test_user) == 200

    def test_v2_excess_id(self, client: TestClient, test_user: User):
        assert self._get(client, "/v2/excess/1", test_user) == 200

    def test_v2_quotes_id(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        q = _quote(db_session, req, test_user)
        db_session.commit()
        assert self._get(client, f"/v2/quotes/{q.id}", test_user) == 200

    def test_v2_prospecting_id_param(self, client: TestClient, test_user: User):
        assert self._get(client, "/v2/prospecting/5", test_user) == 200

    def test_v2_sourcing_page(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        r = _requirement(db_session, req)
        db_session.commit()
        assert self._get(client, f"/v2/sourcing/{r.id}", test_user) == 200

    def test_v2_sourcing_workspace_page(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        r = _requirement(db_session, req)
        db_session.commit()
        assert self._get(client, f"/v2/sourcing/{r.id}/workspace", test_user) == 200

    def test_v2_sourcing_lead_page(self, client: TestClient, test_user: User):
        assert self._get(client, "/v2/sourcing/leads/1", test_user) == 200

    def test_v2_unauthenticated_returns_login(self, unauthenticated_client: TestClient):
        with patch("app.routers.htmx_views.get_user", return_value=None):
            resp = unauthenticated_client.get("/v2/buy-plans")
        assert resp.status_code == 200  # login page is 200


# ══════════════════════════════════════════════════════════════════════════
# Buy-plans list and detail (lines 5863-6240)
# ══════════════════════════════════════════════════════════════════════════


class TestBuyPlansRoutes:
    def test_buy_plans_list(self, client: TestClient):
        resp = client.get("/v2/partials/buy-plans")
        assert resp.status_code == 200

    def test_buy_plans_list_with_filter(self, client: TestClient):
        resp = client.get("/v2/partials/buy-plans?q=test&status=pending")
        assert resp.status_code == 200

    def test_buy_plans_list_mine(self, client: TestClient):
        resp = client.get("/v2/partials/buy-plans?mine=true")
        assert resp.status_code == 200

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

    def test_buy_plan_cancel_404(self, client: TestClient):
        resp = client.post("/v2/partials/buy-plans/99999/cancel", data={})
        assert resp.status_code in (404, 400, 422)

    def test_buy_plan_reset_404(self, client: TestClient):
        resp = client.post("/v2/partials/buy-plans/99999/reset", data={})
        assert resp.status_code in (404, 400, 422)


# ══════════════════════════════════════════════════════════════════════════
# Sourcing routes (lines 6343+)
# ══════════════════════════════════════════════════════════════════════════


class TestSourcingRoutes:
    def test_sourcing_results_404(self, client: TestClient):
        resp = client.get("/v2/partials/sourcing/99999")
        assert resp.status_code == 404

    def test_sourcing_results_exists(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        r = _requirement(db_session, req)
        db_session.commit()
        resp = client.get(f"/v2/partials/sourcing/{r.id}")
        assert resp.status_code == 200

    def test_sourcing_results_with_filters(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        r = _requirement(db_session, req)
        db_session.commit()
        resp = client.get(f"/v2/partials/sourcing/{r.id}?confidence=high&sort=freshest")
        assert resp.status_code == 200

    def test_sourcing_workspace_404(self, client: TestClient):
        resp = client.get("/v2/partials/sourcing/99999/workspace")
        assert resp.status_code == 404

    def test_sourcing_workspace_exists(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        r = _requirement(db_session, req)
        db_session.commit()
        resp = client.get(f"/v2/partials/sourcing/{r.id}/workspace")
        assert resp.status_code == 200

    def test_sourcing_workspace_list_404(self, client: TestClient):
        resp = client.get("/v2/partials/sourcing/99999/workspace-list")
        assert resp.status_code == 404

    def test_sourcing_lead_panel_404(self, client: TestClient):
        resp = client.get("/v2/partials/sourcing/leads/99999/panel")
        assert resp.status_code == 404

    def test_sourcing_search_post_404(self, client: TestClient):
        resp = client.post("/v2/partials/sourcing/99999/search", data={})
        assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# Materials routes (lines 6965+)
# ══════════════════════════════════════════════════════════════════════════


class TestMaterialsRoutes:
    def test_materials_list(self, client: TestClient):
        resp = client.get("/v2/partials/materials")
        assert resp.status_code == 200

    def test_materials_workspace(self, client: TestClient):
        resp = client.get("/v2/partials/materials/workspace")
        assert resp.status_code == 200

    def test_materials_filters_manufacturers(self, client: TestClient):
        resp = client.get("/v2/partials/materials/filters/manufacturers")
        assert resp.status_code == 200

    def test_materials_filters_manufacturers_with_commodity(self, client: TestClient):
        resp = client.get("/v2/partials/materials/filters/manufacturers?commodity=resistors")
        assert resp.status_code == 200

    def test_manufacturer_search_empty(self, client: TestClient):
        resp = client.get("/v2/partials/manufacturers/search")
        assert resp.status_code == 200

    def test_manufacturer_search_with_query(self, client: TestClient):
        resp = client.get("/v2/partials/manufacturers/search?q=Texas")
        assert resp.status_code == 200

    def test_materials_filters_tree(self, client: TestClient):
        resp = client.get("/v2/partials/materials/filters/tree")
        assert resp.status_code == 200

    def test_materials_filters_sub(self, client: TestClient):
        resp = client.get("/v2/partials/materials/filters/sub")
        assert resp.status_code == 200

    def test_materials_ai_interpret(self, client: TestClient):
        resp = client.get("/v2/partials/materials/ai-interpret")
        assert resp.status_code == 200

    def test_materials_faceted(self, client: TestClient):
        resp = client.get("/v2/partials/materials/faceted")
        assert resp.status_code == 200

    def test_materials_detail_404(self, client: TestClient):
        resp = client.get("/v2/partials/materials/99999")
        assert resp.status_code == 404

    def test_materials_insights_404(self, client: TestClient):
        resp = client.get("/v2/partials/materials/99999/insights")
        assert resp.status_code == 404

    def test_manufacturer_add_empty(self, client: TestClient):
        resp = client.post("/v2/partials/manufacturers/add", data={})
        assert resp.status_code in (200, 400, 422)


# ══════════════════════════════════════════════════════════════════════════
# Quotes routes (lines 7386+)
# ══════════════════════════════════════════════════════════════════════════


class TestQuotesRoutes:
    def test_quotes_list(self, client: TestClient):
        resp = client.get("/v2/partials/quotes")
        assert resp.status_code == 200

    def test_quotes_list_with_filter(self, client: TestClient):
        resp = client.get("/v2/partials/quotes?q=Q-001&status=draft")
        assert resp.status_code == 200

    def test_quote_detail_404(self, client: TestClient):
        resp = client.get("/v2/partials/quotes/99999")
        assert resp.status_code == 404

    def test_quote_detail_exists(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        q = _quote(db_session, req, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/quotes/{q.id}")
        assert resp.status_code == 200

    def test_quote_recent_terms(self, client: TestClient):
        resp = client.get("/v2/partials/quotes/recent-terms")
        assert resp.status_code == 200

    def test_pricing_history(self, client: TestClient):
        resp = client.get("/v2/partials/pricing-history/LM317T")
        assert resp.status_code == 200

    def test_quote_delete_404(self, client: TestClient):
        resp = client.delete("/v2/partials/quotes/99999")
        assert resp.status_code == 404

    def test_quote_reopen_404(self, client: TestClient):
        resp = client.post("/v2/partials/quotes/99999/reopen")
        assert resp.status_code == 404

    def test_quote_preview_404(self, client: TestClient):
        resp = client.post("/v2/partials/quotes/99999/preview")
        assert resp.status_code == 404

    def test_quote_send_404(self, client: TestClient):
        resp = client.post("/v2/partials/quotes/99999/send", data={})
        assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# Prospecting routes (lines 7777+)
# ══════════════════════════════════════════════════════════════════════════


class TestProspectingRoutes:
    def test_prospecting_list(self, client: TestClient):
        resp = client.get("/v2/partials/prospecting")
        assert resp.status_code == 200

    def test_prospecting_list_with_filter(self, client: TestClient):
        resp = client.get("/v2/partials/prospecting?q=acme&sort=fit_desc")
        assert resp.status_code == 200

    def test_prospecting_list_recent_sort(self, client: TestClient):
        resp = client.get("/v2/partials/prospecting?sort=recent_desc")
        assert resp.status_code == 200

    def test_prospecting_stats(self, client: TestClient):
        resp = client.get("/v2/partials/prospecting/stats")
        assert resp.status_code == 200

    def test_prospecting_add_domain_empty(self, client: TestClient):
        resp = client.post("/v2/partials/prospecting/add-domain", data={})
        assert resp.status_code == 400

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

    def test_prospecting_claim_400(self, client: TestClient):
        # claim_prospect raises LookupError → 400, not 404
        resp = client.post("/v2/partials/prospecting/99999/claim")
        assert resp.status_code == 400

    def test_prospecting_dismiss_404(self, client: TestClient):
        resp = client.post("/v2/partials/prospecting/99999/dismiss")
        assert resp.status_code == 404

    def test_prospecting_enrich_404(self, client: TestClient):
        resp = client.post("/v2/partials/prospecting/99999/enrich")
        assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# Settings routes (lines 7983+)
# ══════════════════════════════════════════════════════════════════════════


class TestSettingsRoutes:
    def test_settings_partial(self, client: TestClient):
        resp = client.get("/v2/partials/settings")
        assert resp.status_code == 200

    def test_settings_partial_with_tab(self, client: TestClient):
        resp = client.get("/v2/partials/settings?tab=profile")
        assert resp.status_code == 200

    def test_settings_sources(self, client: TestClient):
        resp = client.get("/v2/partials/settings/sources")
        assert resp.status_code == 200

    def test_settings_profile(self, client: TestClient):
        resp = client.get("/v2/partials/settings/profile")
        assert resp.status_code == 200

    def test_settings_system_non_admin(self, client: TestClient):
        # test_user is a 'buyer' — should get 403
        resp = client.get("/v2/partials/settings/system")
        assert resp.status_code == 403

    def test_settings_system_admin(
        self, db_session: Session, admin_user: User
    ):
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
        with patch("app.services.proactive_service.get_matches_for_user", return_value={"groups": [], "stats": {"total": 0}}):
            resp = client.get("/v2/partials/proactive")
        assert resp.status_code == 200

    def test_proactive_list_sent_tab(self, client: TestClient):
        with patch("app.services.proactive_service.get_matches_for_user", return_value={"groups": [], "stats": {"total": 0}}):
            with patch("app.services.proactive_service.get_sent_offers", return_value=[]):
                resp = client.get("/v2/partials/proactive?tab=sent")
        assert resp.status_code == 200

    def test_proactive_badge_no_matches(self, client: TestClient):
        resp = client.get("/v2/partials/proactive/badge")
        assert resp.status_code == 200

    def test_proactive_scorecard(self, client: TestClient):
        with patch("app.services.proactive_service.get_scorecard", return_value={"total_sent": 0, "total_converted": 0}):
            resp = client.get("/v2/partials/proactive/scorecard")
        assert resp.status_code == 200

    def test_proactive_batch_dismiss_empty(self, client: TestClient):
        with patch("app.services.proactive_service.get_matches_for_user", return_value={"groups": [], "stats": {"total": 0}}):
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

    def test_proactive_legacy_send_404(self, client: TestClient):
        resp = client.post("/v2/partials/proactive/99999/send", data={"body": "hello"})
        assert resp.status_code == 404

    def test_proactive_convert_404(self, client: TestClient):
        resp = client.post("/v2/partials/proactive/99999/convert")
        assert resp.status_code == 404

    def test_proactive_draft_no_matches(self, client: TestClient):
        resp = client.post("/v2/partials/proactive/draft", data={})
        assert resp.status_code == 200

    def test_proactive_knowledge_list(self, client: TestClient):
        resp = client.get("/v2/partials/knowledge")
        assert resp.status_code == 200

    def test_proactive_knowledge_search(self, client: TestClient):
        resp = client.get("/v2/partials/knowledge?q=resistor")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Admin merge routes (lines 8088+)
# ══════════════════════════════════════════════════════════════════════════


class TestAdminMergeRoutes:
    def test_vendor_merge_non_admin(self, client: TestClient):
        resp = client.post("/v2/partials/admin/vendor-merge", data={"keep_id": "1", "remove_id": "2"})
        # buyer user → 403
        assert resp.status_code == 403

    def test_company_merge_non_admin(self, client: TestClient):
        resp = client.post("/v2/partials/admin/company-merge", data={"keep_id": "1", "remove_id": "2"})
        assert resp.status_code == 403


# ══════════════════════════════════════════════════════════════════════════
# Insights routes (lines 5729+)
# ══════════════════════════════════════════════════════════════════════════


class TestInsightsRoutes:
    def test_requisition_insights_any_id(self, client: TestClient):
        # Returns 200 even for non-existent IDs (renders "no insights" state)
        resp = client.get("/v2/partials/requisitions/99999/insights")
        assert resp.status_code == 200

    def test_requisition_insights_exists(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/insights")
        assert resp.status_code == 200

    def test_vendor_insights_any_id(self, client: TestClient):
        resp = client.get("/v2/partials/vendors/99999/insights")
        assert resp.status_code == 200

    def test_vendor_insights_exists(self, client: TestClient, db_session: Session):
        vc = _vendor_card(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/vendors/{vc.id}/insights")
        assert resp.status_code == 200

    def test_customer_insights_any_id(self, client: TestClient):
        resp = client.get("/v2/partials/customers/99999/insights")
        assert resp.status_code == 200

    def test_customer_insights_exists(self, client: TestClient, db_session: Session):
        co = _company(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{co.id}/insights")
        assert resp.status_code == 200

    def test_dashboard_pipeline_insights(self, client: TestClient):
        with patch("app.services.knowledge_service.get_cached_pipeline_insights", return_value=None):
            resp = client.get("/v2/partials/dashboard/pipeline-insights")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Email / thread routes (lines 5556+)
# ══════════════════════════════════════════════════════════════════════════


class TestEmailRoutes:
    def test_email_thread_404(self, client: TestClient):
        resp = client.get("/v2/partials/emails/thread/nonexistent-id")
        assert resp.status_code in (200, 404, 400)

    def test_email_intelligence(self, client: TestClient):
        resp = client.get("/v2/partials/email-intelligence")
        assert resp.status_code == 200

    def test_follow_ups_badge(self, client: TestClient):
        resp = client.get("/v2/partials/follow-ups/badge")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Dashboard route (line 5688)
# ══════════════════════════════════════════════════════════════════════════


class TestDashboardRoutes:
    def test_dashboard_partial(self, client: TestClient):
        resp = client.get("/v2/partials/dashboard")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Requisition tab routes (lines 1247-1356) — covers all 7 tabs
# ══════════════════════════════════════════════════════════════════════════


class TestRequisitionTabs:
    def test_tab_parts(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/parts")
        assert resp.status_code == 200

    def test_tab_offers(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/offers")
        assert resp.status_code == 200

    def test_tab_quotes(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/quotes")
        assert resp.status_code == 200

    def test_tab_buy_plans(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/buy_plans")
        assert resp.status_code == 200

    def test_tab_tasks(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/tasks")
        assert resp.status_code == 200

    def test_tab_activity(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/activity")
        assert resp.status_code == 200

    def test_tab_responses(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/responses")
        assert resp.status_code == 200

    def test_tab_unknown_404(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/unknown")
        assert resp.status_code == 404

    def test_tab_404_req(self, client: TestClient):
        resp = client.get("/v2/partials/requisitions/99999/tab/parts")
        assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# Parse-email and paste-offer forms (lines 1364+)
# ══════════════════════════════════════════════════════════════════════════


class TestParseForms:
    def test_parse_email_form(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/parse-email-form")
        assert resp.status_code == 200

    def test_paste_offer_form(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/paste-offer-form")
        assert resp.status_code == 200

    def test_parse_email_form_404(self, client: TestClient):
        resp = client.get("/v2/partials/requisitions/99999/parse-email-form")
        assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# Offer routes (lines 1974-2345)
# ══════════════════════════════════════════════════════════════════════════


class TestOfferRoutes:
    def test_add_offer_form(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/add-offer-form")
        assert resp.status_code == 200

    def test_add_offer_form_404(self, client: TestClient):
        resp = client.get("/v2/partials/requisitions/99999/add-offer-form")
        assert resp.status_code == 404

    def test_review_queue(self, client: TestClient):
        resp = client.get("/v2/partials/offers/review-queue")
        assert resp.status_code == 200

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
# RFQ compose and action routes (lines 2372-2650)
# ══════════════════════════════════════════════════════════════════════════


class TestRfqRoutes:
    def test_rfq_compose_404(self, client: TestClient):
        resp = client.get("/v2/partials/requisitions/99999/rfq-compose")
        assert resp.status_code == 404

    def test_rfq_compose_exists(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/rfq-compose")
        assert resp.status_code == 200

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
        # "archive" is valid but req 99999 doesn't exist → 404
        resp = client.post("/v2/partials/requisitions/99999/action/archive")
        assert resp.status_code == 404

    def test_rfq_prepare_404(self, client: TestClient):
        resp = client.get("/v2/partials/requisitions/99999/rfq-prepare")
        assert resp.status_code == 404

    def test_rfq_prepare_exists(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/rfq-prepare")
        assert resp.status_code == 200

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
    def test_follow_ups_list(self, client: TestClient):
        resp = client.get("/v2/partials/follow-ups")
        assert resp.status_code == 200

    def test_follow_ups_list_with_filter(self, client: TestClient):
        resp = client.get("/v2/partials/follow-ups?q=test")
        assert resp.status_code == 200

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
    def test_search_partial(self, client: TestClient):
        resp = client.get("/v2/partials/search")
        assert resp.status_code == 200

    def test_search_filter_with_id(self, client: TestClient):
        # search_id is required; missing results → returns "expired" message
        resp = client.get("/v2/partials/search/filter?search_id=nonexistent")
        assert resp.status_code == 200

    def test_search_lead_detail_missing(self, client: TestClient):
        resp = client.get("/v2/partials/search/lead-detail?lead_id=99999")
        assert resp.status_code in (200, 404)

    def test_search_requisition_picker(self, client: TestClient):
        resp = client.get("/v2/partials/search/requisition-picker")
        assert resp.status_code == 200

    def test_search_requisition_picker_with_q(self, client: TestClient):
        resp = client.get("/v2/partials/search/requisition-picker?q=REQ")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Vendor routes (lines 3396-4100+)
# ══════════════════════════════════════════════════════════════════════════


class TestVendorDetailRoutes:
    def test_vendor_edit_form_404(self, client: TestClient):
        resp = client.get("/v2/partials/vendors/99999/edit-form")
        assert resp.status_code == 404

    def test_vendor_edit_form_exists(self, client: TestClient, db_session: Session):
        vc = _vendor_card(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/vendors/{vc.id}/edit-form")
        assert resp.status_code == 200

    def test_vendor_reviews_404(self, client: TestClient):
        resp = client.get("/v2/partials/vendors/99999/reviews")
        assert resp.status_code == 404

    def test_vendor_reviews_exists(self, client: TestClient, db_session: Session):
        vc = _vendor_card(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/vendors/{vc.id}/reviews")
        assert resp.status_code == 200

    def test_vendor_contact_nudges_404(self, client: TestClient):
        resp = client.get("/v2/partials/vendors/99999/contact-nudges")
        assert resp.status_code == 404

    def test_vendor_contact_nudges_exists(self, client: TestClient, db_session: Session):
        vc = _vendor_card(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/vendors/{vc.id}/contact-nudges")
        assert resp.status_code == 200

    def test_vendor_tab_rfq_invalid(self, client: TestClient, db_session: Session):
        vc = _vendor_card(db_session)
        db_session.commit()
        # "rfq" not in valid_tabs → 404
        resp = client.get(f"/v2/partials/vendors/{vc.id}/tab/rfq")
        assert resp.status_code == 404

    def test_vendor_tab_contacts(self, client: TestClient, db_session: Session):
        vc = _vendor_card(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/vendors/{vc.id}/tab/contacts")
        assert resp.status_code == 200

    def test_vendor_tab_overview(self, client: TestClient, db_session: Session):
        vc = _vendor_card(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/vendors/{vc.id}/tab/overview")
        assert resp.status_code == 200

    def test_vendor_tab_offers(self, client: TestClient, db_session: Session):
        vc = _vendor_card(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/vendors/{vc.id}/tab/offers")
        assert resp.status_code == 200

    def test_vendor_tab_emails(self, client: TestClient, db_session: Session):
        vc = _vendor_card(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/vendors/{vc.id}/tab/emails")
        assert resp.status_code == 200

    def test_vendor_toggle_blacklist_404(self, client: TestClient):
        resp = client.post("/v2/partials/vendors/99999/toggle-blacklist")
        assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# Customer routes (lines 4292-5200)
# ══════════════════════════════════════════════════════════════════════════


class TestCustomerRoutes:
    def test_customers_list(self, client: TestClient):
        resp = client.get("/v2/partials/customers")
        assert resp.status_code == 200

    def test_customers_list_with_filter(self, client: TestClient):
        resp = client.get("/v2/partials/customers?q=acme")
        assert resp.status_code == 200

    def test_create_form(self, client: TestClient):
        resp = client.get("/v2/partials/customers/create-form")
        assert resp.status_code == 200

    def test_typeahead_empty(self, client: TestClient):
        resp = client.get("/v2/partials/customers/typeahead")
        assert resp.status_code == 200

    def test_typeahead_with_q(self, client: TestClient):
        resp = client.get("/v2/partials/customers/typeahead?q=acme")
        assert resp.status_code == 200

    def test_check_duplicate(self, client: TestClient):
        resp = client.get("/v2/partials/customers/check-duplicate?name=TestCo")
        assert resp.status_code == 200

    def test_customer_detail_404(self, client: TestClient):
        resp = client.get("/v2/partials/customers/99999")
        assert resp.status_code == 404

    def test_customer_detail_exists(self, client: TestClient, db_session: Session):
        co = _company(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{co.id}")
        assert resp.status_code == 200

    def test_customer_edit_form_404(self, client: TestClient):
        resp = client.get("/v2/partials/customers/99999/edit-form")
        assert resp.status_code == 404

    def test_customer_edit_form_exists(self, client: TestClient, db_session: Session):
        co = _company(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{co.id}/edit-form")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Requisition edit and inline patch (lines 1682-1816)
# ══════════════════════════════════════════════════════════════════════════


class TestRequisitionEditRoutes:
    def test_edit_field_name(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/edit/name")
        assert resp.status_code == 200

    def test_edit_field_invalid_returns_400(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        db_session.commit()
        # "customer_name" not in valid_fields → 400
        resp = client.get(f"/v2/partials/requisitions/{req.id}/edit/customer_name")
        assert resp.status_code == 400

    def test_edit_field_status(self, client: TestClient, db_session: Session, test_user: User):
        req = _requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/edit/status")
        assert resp.status_code == 200

    def test_edit_field_404(self, client: TestClient):
        resp = client.get("/v2/partials/requisitions/99999/edit/name")
        assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# Bulk actions (lines 1620-1682)
# ══════════════════════════════════════════════════════════════════════════


class TestBulkActions:
    def test_bulk_close_empty(self, client: TestClient):
        resp = client.post("/v2/partials/requisitions/bulk/close", data={})
        assert resp.status_code in (200, 400)

    def test_bulk_archive_empty(self, client: TestClient):
        resp = client.post("/v2/partials/requisitions/bulk/archive", data={})
        assert resp.status_code in (200, 400)
