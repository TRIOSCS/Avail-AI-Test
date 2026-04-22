"""test_htmx_views_nightly6.py — Coverage boost for htmx_views.py (nightly batch 6).

Targets:
- find-crosses (AI cross-reference search)
- proactive prepare page
- proactive draft AI endpoint
- buy-plan submit/approve workflows
- knowledge base routes
- admin vendor-merge / api-health
- proactive scorecard / badge

Called by: pytest
Depends on: conftest.py (client, db_session, test_user, test_requisition, test_vendor_card)
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import ProactiveMatchStatus
from app.models import Company, CustomerSite, Offer, Requisition, User, VendorCard
from app.models.intelligence import MaterialCard, ProactiveMatch

# ── Admin client fixture ──────────────────────────────────────────────


@pytest.fixture()
def admin_client(db_session: Session, admin_user: User) -> TestClient:
    """TestClient authenticated as an admin user (role='admin')."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    def _db():
        yield db_session

    def _user():
        return admin_user

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


# ── Helper factories ──────────────────────────────────────────────────


def _make_material_card(db: Session, mpn: str = "LM317T", crosses=None) -> MaterialCard:
    mc = MaterialCard(
        normalized_mpn=mpn.lower(),
        display_mpn=mpn,
        manufacturer="Texas Instruments",
        category="Voltage Regulator",
        cross_references=crosses,
        created_at=datetime.now(timezone.utc),
    )
    db.add(mc)
    db.commit()
    db.refresh(mc)
    return mc


def _make_offer(db: Session, req: Requisition, user: User, mpn: str = "LM317T") -> Offer:
    offer = Offer(
        requisition_id=req.id,
        vendor_name="Arrow Electronics",
        mpn=mpn,
        qty_available=500,
        unit_price=0.75,
        entered_by_id=user.id,
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    db.add(offer)
    db.commit()
    db.refresh(offer)
    return offer


def _make_company(db: Session, name: str = "Acme Corp") -> Company:
    co = Company(
        name=name,
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(co)
    db.commit()
    db.refresh(co)
    return co


def _make_site(db: Session, company: Company) -> CustomerSite:
    # CustomerSite has no is_primary field — use valid columns only
    site = CustomerSite(
        company_id=company.id,
        site_name="HQ",
    )
    db.add(site)
    db.commit()
    db.refresh(site)
    return site


def _make_proactive_match(
    db: Session,
    offer: Offer,
    user: User,
    site: CustomerSite,
    mpn: str = "LM317T",
) -> ProactiveMatch:
    pm = ProactiveMatch(
        offer_id=offer.id,
        salesperson_id=user.id,
        customer_site_id=site.id,
        mpn=mpn,
        match_score=80,
        status=ProactiveMatchStatus.NEW,
        created_at=datetime.now(timezone.utc),
    )
    db.add(pm)
    db.commit()
    db.refresh(pm)
    return pm


# ── Section 1: Find Crosses ───────────────────────────────────────────


class TestFindCrosses:
    """Tests for POST /v2/partials/materials/{material_id}/find-crosses."""

    def test_material_not_found_returns_404(self, client, db_session):
        resp = client.post("/v2/partials/materials/999999/find-crosses")
        assert resp.status_code == 404

    def test_cache_hit_skips_ai_call(self, client, db_session):
        """When cross_references already set and refresh=False, returns template
        immediately."""
        existing_crosses = [{"mpn": "LM117", "manufacturer": "TI"}]
        mc = _make_material_card(db_session, crosses=existing_crosses)

        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock) as mock_ai:
            resp = client.post(f"/v2/partials/materials/{mc.id}/find-crosses")

        assert resp.status_code == 200
        mock_ai.assert_not_called()

    def test_ai_search_success_saves_crosses(self, client, db_session):
        """AI returns crosses → saved to DB, template returned."""
        mc = _make_material_card(db_session, crosses=None)
        ai_result = {"crosses": [{"mpn": "LM317", "manufacturer": "ON Semi"}]}

        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock) as mock_ai:
            mock_ai.return_value = ai_result
            resp = client.post(f"/v2/partials/materials/{mc.id}/find-crosses")

        assert resp.status_code == 200
        db_session.refresh(mc)
        assert mc.cross_references is not None

    def test_ai_search_failure_returns_error_template(self, client, db_session):
        """AI exception → template returned with error message (no 500)."""
        mc = _make_material_card(db_session, crosses=None)

        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock) as mock_ai:
            mock_ai.side_effect = Exception("AI unavailable")
            resp = client.post(f"/v2/partials/materials/{mc.id}/find-crosses")

        assert resp.status_code == 200
        assert "failed" in resp.text.lower() or "try again" in resp.text.lower()

    def test_refresh_flag_bypasses_cache(self, client, db_session):
        """Refresh=True forces AI call even when cross_references already set."""
        existing_crosses = [{"mpn": "LM117", "manufacturer": "TI"}]
        mc = _make_material_card(db_session, crosses=existing_crosses)
        ai_result = {"crosses": [{"mpn": "MC7817", "manufacturer": "Motorola"}]}

        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock) as mock_ai:
            mock_ai.return_value = ai_result
            resp = client.post(
                f"/v2/partials/materials/{mc.id}/find-crosses",
                data={"refresh": "true"},
            )

        assert resp.status_code == 200
        mock_ai.assert_called_once()


# ── Section 2: Proactive Prepare Page ────────────────────────────────


class TestProactivePrepare:
    """Tests for POST /v2/proactive/prepare/{site_id}."""

    def test_no_match_ids_redirects_to_proactive(self, client, db_session):
        """No match_ids in form → 303 redirect to /v2/proactive."""
        resp = client.post("/v2/proactive/prepare/999", data={}, follow_redirects=False)
        assert resp.status_code == 303
        assert "/v2/proactive" in resp.headers.get("location", "")

    def test_invalid_match_ids_redirects(self, client, db_session, test_requisition, test_user):
        """match_ids that don't match any ProactiveMatch owned by user → redirect."""
        resp = client.post(
            "/v2/proactive/prepare/999",
            data={"match_ids": "99999"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_valid_matches_renders_prepare_template(self, client, db_session, test_requisition, test_user):
        """Valid match_ids for the current user → renders prepare template."""
        company = _make_company(db_session)
        site = _make_site(db_session, company)
        offer = _make_offer(db_session, test_requisition, test_user)
        pm = _make_proactive_match(db_session, offer, test_user, site)

        resp = client.post(
            f"/v2/proactive/prepare/{site.id}",
            data={"match_ids": str(pm.id)},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert pm.mpn in resp.text or "prepare" in resp.text.lower()


# ── Section 3: Proactive Draft AI ────────────────────────────────────


class TestProactiveDraft:
    """Tests for POST /v2/partials/proactive/draft."""

    def test_no_match_ids_returns_error_html(self, client, db_session):
        """Empty match_ids → 200 with error HTML."""
        resp = client.post("/v2/partials/proactive/draft", data={})
        assert resp.status_code == 200
        assert "No matches selected" in resp.text

    def test_nonexistent_matches_returns_no_valid_matches(self, client, db_session):
        """match_ids with no DB rows → 200 with 'No valid matches' message."""
        resp = client.post("/v2/partials/proactive/draft", data={"match_ids": "99999"})
        assert resp.status_code == 200
        assert "No valid matches" in resp.text

    def test_ai_draft_success_returns_js_html(self, client, db_session, test_requisition, test_user):
        """AI draft succeeds → HTML contains script to populate form."""
        company = _make_company(db_session)
        site = _make_site(db_session, company)
        offer = _make_offer(db_session, test_requisition, test_user)
        pm = _make_proactive_match(db_session, offer, test_user, site)

        ai_response = {
            "subject": "Parts Available — Acme Corp",
            "body": "Dear customer, we have LM317T available.",
        }

        with patch(
            "app.services.proactive_email.draft_proactive_email",
            new_callable=AsyncMock,
        ) as mock_draft:
            mock_draft.return_value = ai_response
            resp = client.post(
                "/v2/partials/proactive/draft",
                data={"match_ids": str(pm.id)},
            )

        assert resp.status_code == 200
        assert "Draft generated" in resp.text or "subject-input" in resp.text

    def test_ai_draft_failure_returns_retry_html(self, client, db_session, test_requisition, test_user):
        """AI draft exception → retry HTML returned, no 500."""
        company = _make_company(db_session)
        site = _make_site(db_session, company)
        offer = _make_offer(db_session, test_requisition, test_user)
        pm = _make_proactive_match(db_session, offer, test_user, site)

        with patch(
            "app.services.proactive_email.draft_proactive_email",
            new_callable=AsyncMock,
        ) as mock_draft:
            mock_draft.side_effect = Exception("Claude down")
            resp = client.post(
                "/v2/partials/proactive/draft",
                data={"match_ids": str(pm.id)},
            )

        assert resp.status_code == 200
        assert "Retry" in resp.text or "unavailable" in resp.text.lower()


# ── Section 4: Buy-plan workflow routes ──────────────────────────────


class TestBuyPlanWorkflow:
    """Tests for buy-plan submit / approve routes."""

    def test_submit_missing_so_raises_400(self, client, db_session):
        """POST submit without sales_order_number → 400."""
        resp = client.post(
            "/v2/partials/buy-plans/999/submit",
            data={"sales_order_number": ""},
        )
        assert resp.status_code == 400

    def test_approve_non_manager_raises_403(self, client, db_session, test_user):
        """Buyer role (not manager/admin) calling approve → 403.

        The conftest client uses test_user which has role='buyer'. The approve route
        checks user.role not in (MANAGER, ADMIN).
        """
        resp = client.post(
            "/v2/partials/buy-plans/999/approve",
            data={"action": "approve"},
        )
        assert resp.status_code == 403


# ── Section 5: Knowledge base routes ─────────────────────────────────


class TestKnowledgeBase:
    """Tests for GET/POST /v2/partials/knowledge."""

    def test_get_knowledge_list_returns_200(self, client, db_session):
        resp = client.get("/v2/partials/knowledge")
        assert resp.status_code == 200

    def test_get_knowledge_with_query_returns_200(self, client, db_session):
        resp = client.get("/v2/partials/knowledge?q=LM317T")
        assert resp.status_code == 200

    def test_post_knowledge_missing_content_raises_400(self, client, db_session):
        resp = client.post("/v2/partials/knowledge", data={"content": ""})
        assert resp.status_code == 400

    def test_post_knowledge_creates_entry_returns_200(self, client, db_session):
        resp = client.post(
            "/v2/partials/knowledge",
            data={"content": "LM317T is a voltage regulator", "entry_type": "note"},
        )
        assert resp.status_code == 200


# ── Section 6: Admin routes ───────────────────────────────────────────


class TestAdminRoutes:
    """Tests for vendor-merge, company-merge, api-health.

    All routes call is_admin(user) internally, so an admin_client is required.
    """

    def test_vendor_merge_success(self, admin_client, db_session, test_vendor_card):
        """Two valid vendor IDs → merge result returned."""
        vc2 = VendorCard(
            normalized_name="arrow-electronics-2",
            display_name="Arrow Electronics 2",
            emails=[],
            phones=[],
            sighting_count=0,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vc2)
        db_session.commit()
        db_session.refresh(vc2)

        with patch("app.services.vendor_merge_service.merge_vendor_cards") as mock_merge:
            mock_merge.return_value = {"kept_name": "Arrow Electronics", "reassigned": 3}
            resp = admin_client.post(
                "/v2/partials/admin/vendor-merge",
                data={"keep_id": str(test_vendor_card.id), "remove_id": str(vc2.id)},
            )

        assert resp.status_code == 200
        assert "Merged" in resp.text or "Arrow" in resp.text

    def test_vendor_merge_value_error_returns_error_html(self, admin_client, db_session, test_vendor_card):
        """merge_vendor_cards raises ValueError → error HTML, no 500."""
        with patch("app.services.vendor_merge_service.merge_vendor_cards") as mock_merge:
            mock_merge.side_effect = ValueError("Cannot merge vendor with itself")
            resp = admin_client.post(
                "/v2/partials/admin/vendor-merge",
                data={
                    "keep_id": str(test_vendor_card.id),
                    "remove_id": str(test_vendor_card.id),
                },
            )

        assert resp.status_code == 200
        assert "Error" in resp.text or "error" in resp.text.lower()

    def test_vendor_merge_non_admin_raises_403(self, client, db_session, test_vendor_card):
        """Non-admin user calling vendor-merge → 403."""
        resp = client.post(
            "/v2/partials/admin/vendor-merge",
            data={"keep_id": str(test_vendor_card.id), "remove_id": "2"},
        )
        assert resp.status_code == 403

    def test_api_health_returns_200(self, admin_client, db_session):
        """GET /v2/partials/admin/api-health → 200.

        The route catches ImportError when connector_health doesn't exist and returns a
        fallback dict, so no mocking needed.
        """
        resp = admin_client.get("/v2/partials/admin/api-health")
        assert resp.status_code == 200

    def test_api_health_with_health_service_mocked(self, admin_client, db_session):
        """When connector_health module exists and returns data → 200 with
        connectors."""
        import sys
        import types

        # Inject a fake connector_health module so the import succeeds
        fake_mod = types.ModuleType("app.services.connector_health")
        fake_mod.get_health_dashboard = lambda db: {
            "connectors": [{"name": "digikey", "status": "ok"}],
            "overall_status": "healthy",
        }
        sys.modules["app.services.connector_health"] = fake_mod
        try:
            resp = admin_client.get("/v2/partials/admin/api-health")
        finally:
            sys.modules.pop("app.services.connector_health", None)

        assert resp.status_code == 200

    def test_company_merge_success(self, admin_client, db_session):
        """Company merge with mocked service → success HTML."""
        co1 = _make_company(db_session, "Acme A")
        co2 = _make_company(db_session, "Acme B")

        with patch("app.services.company_merge_service.merge_companies") as mock_merge:
            mock_merge.return_value = {"kept_name": "Acme A"}
            resp = admin_client.post(
                "/v2/partials/admin/company-merge",
                data={"keep_id": str(co1.id), "remove_id": str(co2.id)},
            )

        assert resp.status_code == 200
        assert "Merged" in resp.text or "Acme" in resp.text

    def test_company_merge_error_returns_error_html(self, admin_client, db_session):
        """merge_companies raises → error HTML returned."""
        co1 = _make_company(db_session, "Acme C")
        co2 = _make_company(db_session, "Acme D")

        with patch("app.services.company_merge_service.merge_companies") as mock_merge:
            mock_merge.side_effect = ValueError("Cannot merge same company")
            resp = admin_client.post(
                "/v2/partials/admin/company-merge",
                data={"keep_id": str(co1.id), "remove_id": str(co2.id)},
            )

        assert resp.status_code == 200
        assert "Error" in resp.text or "error" in resp.text.lower()

    def test_company_merge_non_admin_raises_403(self, client, db_session):
        """Non-admin calling company-merge → 403."""
        resp = client.post(
            "/v2/partials/admin/company-merge",
            data={"keep_id": "1", "remove_id": "2"},
        )
        assert resp.status_code == 403


# ── Section 7: Proactive scorecard and badge ─────────────────────────


class TestProactiveScorecardBadge:
    """Tests for scorecard and badge endpoints."""

    def test_scorecard_returns_200(self, client, db_session):
        with patch("app.services.proactive_service.get_scorecard") as mock_sc:
            mock_sc.return_value = {
                "total_sent": 5,
                "total_converted": 2,
                "conversion_rate": 40,
                "total_revenue": 5000,
            }
            resp = client.get("/v2/partials/proactive/scorecard")

        assert resp.status_code == 200

    def test_scorecard_service_error_returns_200(self, client, db_session):
        """Service raises → fallback stats, still 200."""
        with patch("app.services.proactive_service.get_scorecard") as mock_sc:
            mock_sc.side_effect = RuntimeError("DB error")
            resp = client.get("/v2/partials/proactive/scorecard")

        assert resp.status_code == 200

    def test_badge_no_matches_returns_empty_html(self, client, db_session):
        """No new ProactiveMatches → empty HTML response (not a badge span)."""
        resp = client.get("/v2/partials/proactive/badge")
        assert resp.status_code == 200
        # Empty or just whitespace when no matches
        assert resp.text.strip() == "" or "span" not in resp.text

    def test_badge_with_new_matches_returns_count_span(self, client, db_session, test_requisition, test_user):
        """With new ProactiveMatches → response contains the count badge."""
        company = _make_company(db_session)
        site = _make_site(db_session, company)
        offer = _make_offer(db_session, test_requisition, test_user)
        _make_proactive_match(db_session, offer, test_user, site)

        resp = client.get("/v2/partials/proactive/badge")
        assert resp.status_code == 200
        assert "span" in resp.text and "1" in resp.text


# ── Section 8: Vendor import CSV ─────────────────────────────────────


class TestVendorImportCSV:
    """Tests for POST /v2/partials/admin/import/vendors."""

    def test_missing_file_raises_400(self, client, db_session):
        resp = client.post("/v2/partials/admin/import/vendors", data={})
        assert resp.status_code == 400

    def test_valid_csv_imports_vendors(self, client, db_session):
        import io

        csv_content = b"name,email,website\nTestVendorImport,tv@example.com,https://tv.com\n"
        resp = client.post(
            "/v2/partials/admin/import/vendors",
            files={"file": ("vendors.csv", io.BytesIO(csv_content), "text/csv")},
        )
        assert resp.status_code == 200
        assert "Imported" in resp.text
