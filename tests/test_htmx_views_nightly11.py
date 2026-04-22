"""tests/test_htmx_views_nightly11.py — Coverage for proactive, materials enrichment,
prospecting, settings, admin routes.

Targets: proactive list/batch-dismiss/draft/send, prospecting list/claim/dismiss,
settings partials, materials enrichment/find-crosses/insights, admin import/data-ops.

Called by: pytest autodiscovery
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

import os

os.environ["TESTING"] = "1"

import io
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    Company,
    CustomerSite,
    MaterialCard,
    Offer,
    ProactiveOffer,
    User,
    VendorCard,
)
from app.models.intelligence import ProactiveDoNotOffer, ProactiveMatch
from app.models.knowledge import KnowledgeEntry

# ── Helpers ────────────────────────────────────────────────────────────────


def make_prospect(db_session: Session, **kw):
    """Create a ProspectAccount row for testing."""
    from app.models.prospect_account import ProspectAccount

    defaults = dict(
        name=f"Acme Prospects {uuid.uuid4().hex[:6]}",
        domain=f"acme-{uuid.uuid4().hex[:6]}.com",
        status="suggested",
        fit_score=75,
        readiness_score=60,
        discovery_source="manual",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    p = ProspectAccount(**defaults)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


def make_proactive_match(db_session: Session, offer: Offer, user: User, site: CustomerSite, **kw) -> ProactiveMatch:
    """Create a ProactiveMatch row."""
    defaults = dict(
        offer_id=offer.id,
        salesperson_id=user.id,
        customer_site_id=site.id,
        mpn="LM317T",
        status="new",
        match_score=80,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    m = ProactiveMatch(**defaults)
    db_session.add(m)
    db_session.commit()
    db_session.refresh(m)
    return m


def make_knowledge_entry(db_session: Session, user: User, **kw) -> KnowledgeEntry:
    """Create a KnowledgeEntry row."""
    defaults = dict(
        entry_type="note",
        content=f"Note about components {uuid.uuid4().hex[:6]}",
        source="manual",
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    e = KnowledgeEntry(**defaults)
    db_session.add(e)
    db_session.commit()
    db_session.refresh(e)
    return e


# ── Section 1: Prospecting list partial ───────────────────────────────────


class TestProspectingListPartial:
    """Covers GET /v2/partials/prospecting."""

    def test_prospecting_list_returns_200(self, client: TestClient, db_session: Session):
        make_prospect(db_session)
        resp = client.get("/v2/partials/prospecting")
        assert resp.status_code == 200

    def test_prospecting_list_with_query(self, client: TestClient, db_session: Session):
        make_prospect(db_session, name="SearchableProspect Corp")
        resp = client.get("/v2/partials/prospecting?q=SearchableProspect")
        assert resp.status_code == 200

    def test_prospecting_list_with_status_filter(self, client: TestClient, db_session: Session):
        make_prospect(db_session, status="claimed")
        resp = client.get("/v2/partials/prospecting?status=claimed")
        assert resp.status_code == 200

    def test_prospecting_list_sort_fit_desc(self, client: TestClient, db_session: Session):
        make_prospect(db_session)
        resp = client.get("/v2/partials/prospecting?sort=fit_desc")
        assert resp.status_code == 200

    def test_prospecting_list_sort_recent_desc(self, client: TestClient, db_session: Session):
        make_prospect(db_session)
        resp = client.get("/v2/partials/prospecting?sort=recent_desc")
        assert resp.status_code == 200

    def test_prospecting_list_pagination(self, client: TestClient, db_session: Session):
        for _ in range(3):
            make_prospect(db_session)
        resp = client.get("/v2/partials/prospecting?page=1&per_page=2")
        assert resp.status_code == 200

    def test_prospecting_stats(self, client: TestClient, db_session: Session):
        make_prospect(db_session, readiness_score=80)
        resp = client.get("/v2/partials/prospecting/stats")
        assert resp.status_code == 200


# ── Section 2: Prospecting add-domain ─────────────────────────────────────


class TestProspectingAddDomain:
    """Covers POST /v2/partials/prospecting/add-domain."""

    def test_add_domain_empty_returns_400(self, client: TestClient):
        resp = client.post("/v2/partials/prospecting/add-domain", data={"domain": ""})
        assert resp.status_code == 400

    def test_add_domain_success(self, client: TestClient):
        with patch("app.routers.htmx_views.add_prospect_domain.__wrapped__", None, create=True):
            with patch("app.services.prospect_claim.add_prospect_manually") as mock_add:
                mock_add.return_value = MagicMock(id=42)
                resp = client.post(
                    "/v2/partials/prospecting/add-domain",
                    data={"domain": "testdomain.com"},
                )
                assert resp.status_code == 200

    def test_add_domain_service_error_returns_error_html(self, client: TestClient):
        with patch("app.services.prospect_claim.add_prospect_manually", side_effect=ValueError("fail")):
            resp = client.post(
                "/v2/partials/prospecting/add-domain",
                data={"domain": "baddomain.com"},
            )
            assert resp.status_code == 200
            assert "Error" in resp.text


# ── Section 3: Prospecting detail, claim, dismiss, enrich ─────────────────


class TestProspectingDetailClaimDismiss:
    """Covers GET/POST /v2/partials/prospecting/{prospect_id}."""

    def test_detail_returns_200(self, client: TestClient, db_session: Session):
        p = make_prospect(db_session)
        resp = client.get(f"/v2/partials/prospecting/{p.id}")
        assert resp.status_code == 200

    def test_detail_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/prospecting/99999")
        assert resp.status_code == 404

    def test_detail_with_enrichment_data(self, client: TestClient, db_session: Session):
        p = make_prospect(db_session, enrichment_data={"warm_intro": {"score": 50}})
        resp = client.get(f"/v2/partials/prospecting/{p.id}")
        assert resp.status_code == 200

    def test_dismiss_prospect(self, client: TestClient, db_session: Session):
        p = make_prospect(db_session)
        resp = client.post(f"/v2/partials/prospecting/{p.id}/dismiss")
        assert resp.status_code == 200
        db_session.refresh(p)
        assert p.status == "dismissed"

    def test_dismiss_not_found(self, client: TestClient):
        resp = client.post("/v2/partials/prospecting/99999/dismiss")
        assert resp.status_code == 404

    def test_claim_success(self, client: TestClient, db_session: Session):
        p = make_prospect(db_session)
        with patch("app.services.prospect_claim.claim_prospect") as mock_claim:
            mock_claim.return_value = None
            resp = client.post(f"/v2/partials/prospecting/{p.id}/claim")
            assert resp.status_code == 200

    def test_claim_already_claimed_raises_400(self, client: TestClient, db_session: Session):
        p = make_prospect(db_session)
        with patch("app.services.prospect_claim.claim_prospect", side_effect=ValueError("already claimed")):
            resp = client.post(f"/v2/partials/prospecting/{p.id}/claim")
            assert resp.status_code == 400

    def test_enrich_prospect_success(self, client: TestClient, db_session: Session):
        p = make_prospect(db_session)
        with patch("app.services.prospect_free_enrichment.run_free_enrichment", new_callable=AsyncMock):
            with patch("app.services.prospect_warm_intros.detect_warm_intros", return_value={}):
                with patch("app.services.prospect_warm_intros.generate_one_liner", return_value="test liner"):
                    resp = client.post(f"/v2/partials/prospecting/{p.id}/enrich")
                    assert resp.status_code == 200

    def test_enrich_prospect_not_found(self, client: TestClient):
        resp = client.post("/v2/partials/prospecting/99999/enrich")
        assert resp.status_code == 404

    def test_enrich_prospect_service_error_graceful(self, client: TestClient, db_session: Session):
        """Enrichment errors are caught; endpoint still returns 200."""
        p = make_prospect(db_session)
        with patch(
            "app.services.prospect_free_enrichment.run_free_enrichment",
            new_callable=AsyncMock,
            side_effect=RuntimeError("API down"),
        ):
            with patch("app.services.prospect_warm_intros.detect_warm_intros", side_effect=RuntimeError("fail")):
                resp = client.post(f"/v2/partials/prospecting/{p.id}/enrich")
                assert resp.status_code == 200


# ── Section 4: Settings partials ──────────────────────────────────────────


class TestSettingsPartials:
    """Covers GET /v2/partials/settings, /settings/sources, /settings/profile,
    /settings/system."""

    def test_settings_index(self, client: TestClient):
        resp = client.get("/v2/partials/settings")
        assert resp.status_code == 200

    def test_settings_with_tab_param(self, client: TestClient):
        resp = client.get("/v2/partials/settings?tab=sources")
        assert resp.status_code == 200

    def test_settings_sources_tab(self, client: TestClient):
        resp = client.get("/v2/partials/settings/sources")
        assert resp.status_code == 200

    def test_settings_profile_tab(self, client: TestClient):
        resp = client.get("/v2/partials/settings/profile")
        assert resp.status_code == 200

    def test_settings_system_tab_non_admin_returns_403(self, client: TestClient, db_session: Session, test_user: User):
        """Buyer user should get 403 from system tab (admin-only)."""
        resp = client.get("/v2/partials/settings/system")
        assert resp.status_code == 403

    def test_toggle_8x8(self, client: TestClient, db_session: Session, test_user: User):
        initial = test_user.eight_by_eight_enabled
        resp = client.post("/api/user/toggle-8x8")
        assert resp.status_code == 200
        db_session.refresh(test_user)
        assert test_user.eight_by_eight_enabled != initial

    def test_toggle_8x8_twice_reverts(self, client: TestClient, db_session: Session, test_user: User):
        original = test_user.eight_by_eight_enabled
        client.post("/api/user/toggle-8x8")
        client.post("/api/user/toggle-8x8")
        db_session.refresh(test_user)
        assert test_user.eight_by_eight_enabled == original


# ── Section 5: Settings admin data-ops ───────────────────────────────────


class TestSettingsDataOps:
    """Covers GET /v2/partials/settings/data-ops (buyer role → 403)."""

    def test_data_ops_non_admin_returns_403(self, client: TestClient):
        resp = client.get("/v2/partials/settings/data-ops")
        assert resp.status_code == 403


# ── Section 6: Proactive list and batch-dismiss ───────────────────────────


class TestProactiveListAndDismiss:
    """Covers GET /v2/partials/proactive and POST /v2/partials/proactive/batch-
    dismiss."""

    def test_proactive_list_matches_tab(self, client: TestClient, db_session: Session):
        with patch(
            "app.services.proactive_service.get_matches_for_user", return_value={"groups": [], "stats": {"total": 0}}
        ):
            with patch("app.services.proactive_service.get_sent_offers", return_value=[]):
                resp = client.get("/v2/partials/proactive")
                assert resp.status_code == 200

    def test_proactive_list_sent_tab(self, client: TestClient, db_session: Session):
        with patch(
            "app.services.proactive_service.get_matches_for_user", return_value={"groups": [], "stats": {"total": 0}}
        ):
            with patch("app.services.proactive_service.get_sent_offers", return_value=[]):
                resp = client.get("/v2/partials/proactive?tab=sent")
                assert resp.status_code == 200

    def test_proactive_list_returns_list_when_service_returns_list(self, client: TestClient):
        """Service may return plain list rather than dict — both should work."""
        with patch("app.services.proactive_service.get_matches_for_user", return_value=[]):
            with patch("app.services.proactive_service.get_sent_offers", return_value=[]):
                resp = client.get("/v2/partials/proactive")
                assert resp.status_code == 200

    def test_batch_dismiss_empty_form(self, client: TestClient, db_session: Session):
        with patch(
            "app.services.proactive_service.get_matches_for_user", return_value={"groups": [], "stats": {"total": 0}}
        ):
            resp = client.post("/v2/partials/proactive/batch-dismiss", data={})
            assert resp.status_code == 200

    def test_batch_dismiss_with_ids(
        self,
        client: TestClient,
        db_session: Session,
        test_offer: Offer,
        test_user: User,
        test_customer_site: CustomerSite,
    ):
        m = make_proactive_match(db_session, test_offer, test_user, test_customer_site)
        with patch(
            "app.services.proactive_service.get_matches_for_user", return_value={"groups": [], "stats": {"total": 0}}
        ):
            resp = client.post(
                "/v2/partials/proactive/batch-dismiss",
                data={"match_ids": [str(m.id)]},
            )
            assert resp.status_code == 200
            db_session.refresh(m)
            assert m.status == "dismissed"


# ── Section 7: Proactive draft endpoint ───────────────────────────────────


class TestProactiveDraft:
    """Covers POST /v2/partials/proactive/draft."""

    def test_draft_no_match_ids_returns_error_html(self, client: TestClient):
        resp = client.post("/v2/partials/proactive/draft", data={})
        assert resp.status_code == 200
        assert "No matches selected" in resp.text

    def test_draft_invalid_match_ids_returns_no_valid_matches(self, client: TestClient, db_session: Session):
        resp = client.post("/v2/partials/proactive/draft", data={"match_ids": "999999"})
        assert resp.status_code == 200
        assert "No valid matches found" in resp.text

    def test_draft_success(
        self,
        client: TestClient,
        db_session: Session,
        test_offer: Offer,
        test_user: User,
        test_customer_site: CustomerSite,
    ):
        m = make_proactive_match(db_session, test_offer, test_user, test_customer_site)
        with patch("app.services.proactive_email.draft_proactive_email", new_callable=AsyncMock) as mock_draft:
            mock_draft.return_value = {
                "subject": "Parts Available",
                "body": "Hello, we have LM317T in stock.",
            }
            resp = client.post(
                "/v2/partials/proactive/draft",
                data={"match_ids": str(m.id)},
            )
            assert resp.status_code == 200

    def test_draft_ai_failure_returns_retry_html(
        self,
        client: TestClient,
        db_session: Session,
        test_offer: Offer,
        test_user: User,
        test_customer_site: CustomerSite,
    ):
        m = make_proactive_match(db_session, test_offer, test_user, test_customer_site)
        with patch("app.services.proactive_email.draft_proactive_email", new_callable=AsyncMock) as mock_draft:
            mock_draft.side_effect = RuntimeError("AI unavailable")
            resp = client.post(
                "/v2/partials/proactive/draft",
                data={"match_ids": str(m.id)},
            )
            assert resp.status_code == 200
            assert "Auto-draft unavailable" in resp.text


# ── Section 8: Proactive scorecard and badge ──────────────────────────────


class TestProactiveScorecardAndBadge:
    """Covers GET /v2/partials/proactive/scorecard and /badge."""

    def test_scorecard_returns_200(self, client: TestClient):
        with patch(
            "app.services.proactive_service.get_scorecard",
            return_value={"total_sent": 5, "total_converted": 2, "conversion_rate": 40, "total_revenue": 1000},
        ):
            resp = client.get("/v2/partials/proactive/scorecard")
            assert resp.status_code == 200

    def test_scorecard_service_error_graceful(self, client: TestClient):
        with patch("app.services.proactive_service.get_scorecard", side_effect=RuntimeError("db error")):
            resp = client.get("/v2/partials/proactive/scorecard")
            assert resp.status_code == 200

    def test_badge_no_matches_returns_empty(
        self,
        client: TestClient,
        db_session: Session,
    ):
        resp = client.get("/v2/partials/proactive/badge")
        assert resp.status_code == 200
        assert resp.text == ""

    def test_badge_with_matches_returns_count(
        self,
        client: TestClient,
        db_session: Session,
        test_offer: Offer,
        test_user: User,
        test_customer_site: CustomerSite,
    ):
        make_proactive_match(db_session, test_offer, test_user, test_customer_site, status="new")
        resp = client.get("/v2/partials/proactive/badge")
        assert resp.status_code == 200
        assert "1" in resp.text


# ── Section 9: Proactive do-not-offer ────────────────────────────────────


class TestProactiveDoNotOffer:
    """Covers POST /v2/partials/proactive/do-not-offer."""

    def test_do_not_offer_missing_fields_returns_400(self, client: TestClient):
        resp = client.post("/v2/partials/proactive/do-not-offer", data={})
        assert resp.status_code == 400

    def test_do_not_offer_bad_company_id_returns_400(self, client: TestClient):
        resp = client.post(
            "/v2/partials/proactive/do-not-offer",
            data={"mpn": "LM317T", "company_id": "notanumber"},
        )
        assert resp.status_code == 400

    def test_do_not_offer_creates_record(self, client: TestClient, db_session: Session, test_company: Company):
        resp = client.post(
            "/v2/partials/proactive/do-not-offer",
            data={"mpn": "LM317T", "company_id": str(test_company.id)},
        )
        assert resp.status_code == 200
        assert "Suppressed" in resp.text
        rec = db_session.query(ProactiveDoNotOffer).filter_by(company_id=test_company.id).first()
        assert rec is not None
        assert rec.mpn == "LM317T"

    def test_do_not_offer_duplicate_is_idempotent(self, client: TestClient, db_session: Session, test_company: Company):
        client.post(
            "/v2/partials/proactive/do-not-offer",
            data={"mpn": "LM317T", "company_id": str(test_company.id)},
        )
        resp = client.post(
            "/v2/partials/proactive/do-not-offer",
            data={"mpn": "LM317T", "company_id": str(test_company.id)},
        )
        assert resp.status_code == 200
        count = db_session.query(ProactiveDoNotOffer).filter_by(company_id=test_company.id, mpn="LM317T").count()
        assert count == 1


# ── Section 10: Proactive legacy send ────────────────────────────────────


class TestProactiveLegacySend:
    """Covers POST /v2/partials/proactive/{match_id}/send."""

    def test_send_not_found_returns_404(self, client: TestClient):
        resp = client.post("/v2/partials/proactive/99999/send", data={"body": "Hello"})
        assert resp.status_code == 404

    def test_send_empty_body_returns_400(
        self,
        client: TestClient,
        db_session: Session,
        test_offer: Offer,
        test_user: User,
        test_customer_site: CustomerSite,
    ):
        m = make_proactive_match(db_session, test_offer, test_user, test_customer_site)
        resp = client.post(f"/v2/partials/proactive/{m.id}/send", data={"body": ""})
        assert resp.status_code == 400

    def test_send_success_marks_sent(
        self,
        client: TestClient,
        db_session: Session,
        test_offer: Offer,
        test_user: User,
        test_customer_site: CustomerSite,
    ):
        m = make_proactive_match(db_session, test_offer, test_user, test_customer_site)
        resp = client.post(f"/v2/partials/proactive/{m.id}/send", data={"body": "Hello, we have parts."})
        assert resp.status_code == 200
        db_session.refresh(m)
        assert m.status == "sent"


# ── Section 11: Proactive convert ─────────────────────────────────────────


class TestProactiveConvert:
    """Covers POST /v2/partials/proactive/{offer_id}/convert."""

    def test_convert_not_found_returns_404(self, client: TestClient):
        resp = client.post("/v2/partials/proactive/99999/convert")
        assert resp.status_code == 404

    def test_convert_success(
        self,
        client: TestClient,
        db_session: Session,
        test_proactive_offer: ProactiveOffer,
    ):
        with patch("app.services.proactive_service.convert_proactive_to_win") as mock_conv:
            mock_conv.return_value = {"requisition_id": 1, "quote_id": 2}
            resp = client.post(f"/v2/partials/proactive/{test_proactive_offer.id}/convert")
            assert resp.status_code == 200

    def test_convert_service_error_returns_500(
        self,
        client: TestClient,
        db_session: Session,
        test_proactive_offer: ProactiveOffer,
    ):
        with patch("app.services.proactive_service.convert_proactive_to_win", side_effect=RuntimeError("fail")):
            resp = client.post(f"/v2/partials/proactive/{test_proactive_offer.id}/convert")
            assert resp.status_code == 500


# ── Section 12: Materials enrichment ──────────────────────────────────────


class TestMaterialsEnrichment:
    """Covers POST /v2/partials/materials/{id}/enrich."""

    def test_enrich_not_found_returns_404(self, client: TestClient):
        resp = client.post("/v2/partials/materials/99999/enrich")
        assert resp.status_code == 404

    def test_enrich_success(
        self,
        client: TestClient,
        db_session: Session,
        test_material_card: MaterialCard,
    ):
        with patch("app.services.material_enrichment_service.enrich_material_cards", new_callable=AsyncMock):
            resp = client.post(f"/v2/partials/materials/{test_material_card.id}/enrich")
            assert resp.status_code == 200

    def test_enrich_service_error_graceful(
        self,
        client: TestClient,
        db_session: Session,
        test_material_card: MaterialCard,
    ):
        with patch(
            "app.services.material_enrichment_service.enrich_material_cards",
            new_callable=AsyncMock,
            side_effect=RuntimeError("AI error"),
        ):
            resp = client.post(f"/v2/partials/materials/{test_material_card.id}/enrich")
            assert resp.status_code == 200


# ── Section 13: Materials find-crosses ────────────────────────────────────


class TestMaterialsFindCrosses:
    """Covers POST /v2/partials/materials/{id}/find-crosses."""

    def test_find_crosses_not_found(self, client: TestClient):
        resp = client.post("/v2/partials/materials/99999/find-crosses")
        assert resp.status_code == 404

    def test_find_crosses_uses_cache_when_available(
        self,
        client: TestClient,
        db_session: Session,
        test_material_card: MaterialCard,
    ):
        test_material_card.cross_references = [{"mpn": "LM117T", "manufacturer": "ST"}]
        db_session.commit()
        resp = client.post(f"/v2/partials/materials/{test_material_card.id}/find-crosses")
        assert resp.status_code == 200

    def test_find_crosses_calls_ai_when_empty(
        self,
        client: TestClient,
        db_session: Session,
        test_material_card: MaterialCard,
    ):
        test_material_card.cross_references = None
        db_session.commit()
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock) as mock_ai:
            mock_ai.return_value = {"crosses": [{"mpn": "LM117T", "manufacturer": "ST"}]}
            resp = client.post(f"/v2/partials/materials/{test_material_card.id}/find-crosses")
            assert resp.status_code == 200

    def test_find_crosses_ai_failure_returns_error_section(
        self,
        client: TestClient,
        db_session: Session,
        test_material_card: MaterialCard,
    ):
        test_material_card.cross_references = None
        db_session.commit()
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock) as mock_ai:
            mock_ai.side_effect = RuntimeError("timeout")
            resp = client.post(f"/v2/partials/materials/{test_material_card.id}/find-crosses")
            assert resp.status_code == 200

    def test_find_crosses_refresh_bypasses_cache(
        self,
        client: TestClient,
        db_session: Session,
        test_material_card: MaterialCard,
    ):
        test_material_card.cross_references = [{"mpn": "OLD", "manufacturer": "OldCo"}]
        db_session.commit()
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock) as mock_ai:
            mock_ai.return_value = {"crosses": [{"mpn": "LM117T", "manufacturer": "ST"}]}
            resp = client.post(
                f"/v2/partials/materials/{test_material_card.id}/find-crosses",
                data={"refresh": "true"},
            )
            assert resp.status_code == 200


# ── Section 14: Materials insights ───────────────────────────────────────


class TestMaterialsInsights:
    """Covers GET /v2/partials/materials/{id}/insights."""

    def test_insights_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/materials/99999/insights")
        assert resp.status_code == 404

    def test_insights_returns_200(
        self,
        client: TestClient,
        db_session: Session,
        test_material_card: MaterialCard,
    ):
        resp = client.get(f"/v2/partials/materials/{test_material_card.id}/insights")
        assert resp.status_code == 200

    def test_insights_with_offers(
        self,
        client: TestClient,
        db_session: Session,
        test_material_card: MaterialCard,
        test_offer: Offer,
    ):
        """Insights with an offer that has a matching normalized_mpn."""
        test_offer.normalized_mpn = test_material_card.normalized_mpn
        db_session.commit()
        resp = client.get(f"/v2/partials/materials/{test_material_card.id}/insights")
        assert resp.status_code == 200


# ── Section 15: Knowledge partial ────────────────────────────────────────


class TestKnowledgePartial:
    """Covers GET and POST /v2/partials/knowledge."""

    def test_knowledge_list_empty(self, client: TestClient):
        resp = client.get("/v2/partials/knowledge")
        assert resp.status_code == 200

    def test_knowledge_list_with_entries(self, client: TestClient, db_session: Session, test_user: User):
        make_knowledge_entry(db_session, test_user, content="LM317T voltage regulator notes")
        resp = client.get("/v2/partials/knowledge")
        assert resp.status_code == 200

    def test_knowledge_list_with_query(self, client: TestClient, db_session: Session, test_user: User):
        make_knowledge_entry(db_session, test_user, content="Searchable content about LM317T")
        resp = client.get("/v2/partials/knowledge?q=LM317T")
        assert resp.status_code == 200

    def test_create_knowledge_entry_success(self, client: TestClient, db_session: Session):
        resp = client.post(
            "/v2/partials/knowledge",
            data={"content": "This is a test knowledge entry", "entry_type": "note"},
        )
        assert resp.status_code == 200
        entry = (
            db_session.query(KnowledgeEntry).filter(KnowledgeEntry.content == "This is a test knowledge entry").first()
        )
        assert entry is not None

    def test_create_knowledge_entry_empty_content_returns_400(self, client: TestClient):
        resp = client.post("/v2/partials/knowledge", data={"content": ""})
        assert resp.status_code == 400

    def test_create_knowledge_entry_defaults_entry_type(self, client: TestClient, db_session: Session):
        resp = client.post("/v2/partials/knowledge", data={"content": "Default type entry"})
        assert resp.status_code == 200
        entry = db_session.query(KnowledgeEntry).filter(KnowledgeEntry.content == "Default type entry").first()
        assert entry is not None
        assert entry.entry_type == "note"


# ── Section 16: Admin import/data-ops ────────────────────────────────────


class TestAdminImportVendors:
    """Covers POST /v2/partials/admin/import/vendors and GET /v2/partials/admin/data-
    ops."""

    def test_import_vendors_no_file_returns_400(
        self, client: TestClient, db_session: Session, test_user: User, admin_user: User
    ):
        from app.database import get_db
        from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
        from app.main import app

        def _override_db():
            yield db_session

        def _override_admin():
            return admin_user

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[require_user] = _override_admin
        app.dependency_overrides[require_admin] = _override_admin
        app.dependency_overrides[require_buyer] = _override_admin
        app.dependency_overrides[require_fresh_token] = AsyncMock(return_value="mock-token")

        try:
            with TestClient(app) as admin_client:
                resp = admin_client.post("/v2/partials/admin/import/vendors")
                assert resp.status_code == 400
        finally:
            for dep in [get_db, require_user, require_admin, require_buyer, require_fresh_token]:
                app.dependency_overrides.pop(dep, None)

    def test_import_vendors_csv_success(self, client: TestClient, db_session: Session, admin_user: User):
        from app.database import get_db
        from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
        from app.main import app

        def _override_db():
            yield db_session

        def _override_admin():
            return admin_user

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[require_user] = _override_admin
        app.dependency_overrides[require_admin] = _override_admin
        app.dependency_overrides[require_buyer] = _override_admin
        app.dependency_overrides[require_fresh_token] = AsyncMock(return_value="mock-token")

        csv_content = b"name,email,phone,website\nNewVendorCSV Inc,sales@newvendor.com,555-1234,https://newvendor.com\n"

        try:
            with TestClient(app) as admin_client:
                resp = admin_client.post(
                    "/v2/partials/admin/import/vendors",
                    files={"file": ("vendors.csv", io.BytesIO(csv_content), "text/csv")},
                )
                assert resp.status_code == 200
                assert "Imported" in resp.text
        finally:
            for dep in [get_db, require_user, require_admin, require_buyer, require_fresh_token]:
                app.dependency_overrides.pop(dep, None)

    def test_import_vendors_skips_duplicates(
        self, client: TestClient, db_session: Session, admin_user: User, test_vendor_card: VendorCard
    ):
        from app.database import get_db
        from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
        from app.main import app

        def _override_db():
            yield db_session

        def _override_admin():
            return admin_user

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[require_user] = _override_admin
        app.dependency_overrides[require_admin] = _override_admin
        app.dependency_overrides[require_buyer] = _override_admin
        app.dependency_overrides[require_fresh_token] = AsyncMock(return_value="mock-token")

        # Use normalized name that already exists ("arrow electronics")
        csv_content = b"name,email\nArrow Electronics,sales@arrow.com\n"

        try:
            with TestClient(app) as admin_client:
                resp = admin_client.post(
                    "/v2/partials/admin/import/vendors",
                    files={"file": ("vendors.csv", io.BytesIO(csv_content), "text/csv")},
                )
                assert resp.status_code == 200
                assert "Imported 0" in resp.text
        finally:
            for dep in [get_db, require_user, require_admin, require_buyer, require_fresh_token]:
                app.dependency_overrides.pop(dep, None)

    def test_admin_data_ops_returns_200_for_admin(self, client: TestClient, db_session: Session, admin_user: User):
        """Admin users can access data-ops panel."""
        from app.database import get_db
        from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
        from app.main import app

        def _override_db():
            yield db_session

        def _override_admin():
            return admin_user

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[require_user] = _override_admin
        app.dependency_overrides[require_admin] = _override_admin
        app.dependency_overrides[require_buyer] = _override_admin
        app.dependency_overrides[require_fresh_token] = AsyncMock(return_value="mock-token")

        try:
            with TestClient(app) as admin_client:
                resp = admin_client.get("/v2/partials/admin/data-ops")
                assert resp.status_code == 200
        finally:
            for dep in [get_db, require_user, require_admin, require_buyer, require_fresh_token]:
                app.dependency_overrides.pop(dep, None)

    def test_admin_api_health(self, client: TestClient, db_session: Session, admin_user: User):
        from app.database import get_db
        from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
        from app.main import app

        def _override_db():
            yield db_session

        def _override_admin():
            return admin_user

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[require_user] = _override_admin
        app.dependency_overrides[require_admin] = _override_admin
        app.dependency_overrides[require_buyer] = _override_admin
        app.dependency_overrides[require_fresh_token] = AsyncMock(return_value="mock-token")

        try:
            with TestClient(app) as admin_client:
                resp = admin_client.get("/v2/partials/admin/api-health")
                assert resp.status_code == 200
        finally:
            for dep in [get_db, require_user, require_admin, require_buyer, require_fresh_token]:
                app.dependency_overrides.pop(dep, None)


# ── Section 17: Admin vendor/company merge ────────────────────────────────


class TestAdminMerge:
    """Covers POST /v2/partials/admin/vendor-merge and /company-merge (buyer role →
    403)."""

    def test_vendor_merge_non_admin_returns_403(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.post(
            "/v2/partials/admin/vendor-merge",
            data={"keep_id": "1", "remove_id": "2"},
        )
        assert resp.status_code == 403

    def test_company_merge_non_admin_returns_403(self, client: TestClient):
        resp = client.post(
            "/v2/partials/admin/company-merge",
            data={"keep_id": "1", "remove_id": "2"},
        )
        assert resp.status_code == 403
