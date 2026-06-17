"""tests/test_prospecting_tab.py — Behavioral coverage for the consolidated prospecting
tab (HTMX routes in app.routers.htmx_views + the prospect_claim / prospect_priority
services).

These tests pin the bug fixes from the consolidation pass:
  - Release returns a claim to the pool (not DISMISSED) and relinquishes ownership.
  - Claim enforces the site cap + ownership in the service (both entry points).
  - Add-domain works through the real service (dict return, no AttributeError 500).
  - Dismiss is SUGGESTED-only and stamps audit + reason.
  - "Most buyer-ready" sort actually ranks by buyer_ready_score.
  - Search preserves the active status filter (hidden field).
  - Detail surfaces signal strength, contacts, and similar wins.
  - Prospect writes are gated by require_buyer (agent excluded).

Called by: pytest autodiscovery
Depends on: conftest.py fixtures (client, db_session, test_user, manager_user)
"""

import os

os.environ["TESTING"] = "1"

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Company
from app.models.prospect_account import ProspectAccount


def make_prospect(db: Session, **kw) -> ProspectAccount:
    defaults = dict(
        name=f"Prospect {uuid.uuid4().hex[:6]}",
        domain=f"p-{uuid.uuid4().hex[:6]}.com",
        status="suggested",
        fit_score=75,
        readiness_score=60,
        discovery_source="manual",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    p = ProspectAccount(**defaults)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


# ── Release semantics (the headline data-integrity fix) ───────────────────


class TestRelease:
    def test_release_returns_prospect_to_pool_and_clears_ownership(self, db_session, test_user):
        from app.services.prospect_claim import claim_prospect, release_prospect

        p = make_prospect(db_session, status="suggested")
        claim_prospect(p.id, test_user.id, db_session)
        db_session.refresh(p)
        assert p.status == "claimed"
        assert p.company_id is not None
        company_id = p.company_id
        assert db_session.get(Company, company_id).account_owner_id == test_user.id

        release_prospect(p.id, test_user.id, db_session)
        db_session.refresh(p)
        assert p.status == "suggested"  # back to the pool, NOT dismissed
        assert p.claimed_by is None
        assert p.claimed_at is None
        assert db_session.get(Company, company_id).account_owner_id is None  # ownership relinquished

    def test_release_rejects_non_owner(self, db_session, test_user, manager_user):
        from app.services.prospect_claim import claim_prospect, release_prospect

        p = make_prospect(db_session, status="suggested")
        claim_prospect(p.id, test_user.id, db_session)
        with pytest.raises(ValueError, match="owner or an admin"):
            release_prospect(p.id, manager_user.id, db_session, is_admin=False)

    def test_release_rejects_non_claimed(self, db_session, test_user):
        from app.services.prospect_claim import release_prospect

        p = make_prospect(db_session, status="suggested")
        with pytest.raises(ValueError, match="claimed"):
            release_prospect(p.id, test_user.id, db_session)

    def test_release_route_returns_to_pool(self, client, db_session, test_user):
        p = make_prospect(db_session, status="suggested")
        with patch("app.services.prospect_claim.trigger_deep_enrichment_bg", new_callable=AsyncMock):
            client.post(f"/v2/partials/prospecting/{p.id}/claim")
        resp = client.post(f"/v2/partials/prospecting/{p.id}/release")
        assert resp.status_code == 200
        db_session.refresh(p)
        assert p.status == "suggested"
        assert p.claimed_by is None


# ── Claim: cap + ownership in the service ─────────────────────────────────


class TestClaim:
    def test_claim_real_service_sets_claimed(self, client, db_session, test_user):
        p = make_prospect(db_session, status="suggested")
        with patch("app.services.prospect_claim.trigger_deep_enrichment_bg", new_callable=AsyncMock):
            resp = client.post(f"/v2/partials/prospecting/{p.id}/claim")
        assert resp.status_code == 200
        assert "Claimed" in resp.text
        db_session.refresh(p)
        assert p.status == "claimed"
        assert p.claimed_by == test_user.id
        assert p.company_id is not None

    def test_claim_blocked_at_site_cap(self, db_session, test_user, monkeypatch):
        monkeypatch.setattr("app.services.prospect_claim.SITE_CAP", 0)
        from app.services.prospect_claim import claim_prospect

        p = make_prospect(db_session, status="suggested")
        with pytest.raises(ValueError, match="cap"):
            claim_prospect(p.id, test_user.id, db_session)

    def test_claim_refuses_company_owned_by_another(self, db_session, test_user, manager_user):
        from app.services.prospect_claim import claim_prospect

        domain = f"collide-{uuid.uuid4().hex[:6]}.com"
        company = Company(name="Owned Co", domain=domain, is_active=True, account_owner_id=manager_user.id)
        db_session.add(company)
        db_session.commit()

        p = make_prospect(db_session, status="suggested", domain=domain, company_id=None)
        with pytest.raises(ValueError, match="already owned"):
            claim_prospect(p.id, test_user.id, db_session)


# ── Add-domain: real-service regression (used to 500 on dict.id) ──────────


class TestAddDomain:
    def test_add_domain_creates_prospect_via_real_service(self, client, db_session):
        domain = f"brandnew-{uuid.uuid4().hex[:6]}.com"
        resp = client.post("/v2/partials/prospecting/add-domain", data={"domain": domain})
        assert resp.status_code == 200
        assert "Added" in resp.text
        assert db_session.query(ProspectAccount).filter(ProspectAccount.domain == domain).first() is not None

    def test_add_domain_dedupes_existing(self, client, db_session):
        p = make_prospect(db_session)
        resp = client.post("/v2/partials/prospecting/add-domain", data={"domain": p.domain})
        assert resp.status_code == 200
        assert "Already in pool" in resp.text


# ── Dismiss: SUGGESTED-only guard + audit + reason ────────────────────────


class TestDismiss:
    def test_dismiss_sets_audit_and_reason(self, client, db_session, test_user):
        p = make_prospect(db_session, status="suggested")
        resp = client.post(f"/v2/partials/prospecting/{p.id}/dismiss", data={"reason": "not_icp"})
        assert resp.status_code == 200
        db_session.refresh(p)
        assert p.status == "dismissed"
        assert p.dismissed_by == test_user.id
        assert p.dismissed_at is not None
        assert p.dismiss_reason == "not_icp"

    def test_dismiss_rejects_claimed(self, client, db_session):
        p = make_prospect(db_session, status="claimed")
        resp = client.post(f"/v2/partials/prospecting/{p.id}/dismiss")
        assert resp.status_code == 200
        assert "Only suggested" in resp.headers.get("HX-Trigger", "")
        db_session.refresh(p)
        assert p.status == "claimed"  # unchanged


# ── List: real buyer-ready sort + per-status counts + search carrier ──────


class TestListBehavior:
    def test_buyer_ready_sort_ranks_by_score(self, client, db_session):
        make_prospect(
            db_session,
            name="HighReadyCo",
            fit_score=90,
            readiness_score=90,
            readiness_signals={"intent": {"strength": "strong"}, "contacts_verified_count": 3},
            contacts_preview=[{"verified": True, "seniority": "decision_maker", "name": "DM"}],
        )
        make_prospect(db_session, name="LowReadyCo", fit_score=20, readiness_score=10, readiness_signals={})
        resp = client.get("/v2/partials/prospecting?sort=buyer_ready_desc")
        assert resp.status_code == 200
        body = resp.text
        assert body.index("HighReadyCo") < body.index("LowReadyCo")

    def test_default_list_hides_dismissed(self, client, db_session):
        make_prospect(db_session, name="ActiveSuggested", status="suggested")
        make_prospect(db_session, name="GoneDismissed", status="dismissed")
        resp = client.get("/v2/partials/prospecting")
        assert "ActiveSuggested" in resp.text
        assert "GoneDismissed" not in resp.text

    def test_search_input_carries_active_status(self, client, db_session):
        # The hidden status field is what makes search preserve the active filter.
        resp = client.get("/v2/partials/prospecting?status=claimed")
        assert 'name="status" value="claimed"' in resp.text


# ── Detail: surfaced buyer-intelligence ───────────────────────────────────


class TestDetailIntelligence:
    def test_detail_shows_signal_strength_not_bare_keys(self, client, db_session):
        p = make_prospect(
            db_session,
            readiness_signals={"intent": {"strength": "strong"}, "hiring": {"type": "procurement"}},
        )
        resp = client.get(f"/v2/partials/prospecting/{p.id}")
        assert "Intent: strong" in resp.text
        assert "Hiring: procurement" in resp.text

    def test_detail_shows_contacts_and_similar(self, client, db_session):
        p = make_prospect(
            db_session,
            contacts_preview=[{"name": "Jane Buyer", "title": "CPO", "seniority": "decision_maker", "verified": True}],
            similar_customers=[{"name": "Acme Reference"}],
        )
        resp = client.get(f"/v2/partials/prospecting/{p.id}")
        assert "Jane Buyer" in resp.text
        assert "Decision-maker" in resp.text
        assert "Similar wins" in resp.text
        assert "Acme Reference" in resp.text

    def test_detail_shows_buyer_ready_score(self, client, db_session):
        p = make_prospect(db_session, fit_score=88, readiness_score=82)
        resp = client.get(f"/v2/partials/prospecting/{p.id}")
        assert "Buyer-ready score" in resp.text


# ── Auth: prospect writes require buyer role (agent excluded) ──────────────


class TestWriteAuth:
    def test_claim_is_gated_by_require_buyer(self, db_session, test_user):
        # Overriding require_buyer to forbid only affects the route if it actually
        # depends on require_buyer (it would be ignored under require_user).
        from app.database import get_db
        from app.dependencies import require_buyer, require_user
        from app.main import app

        p = make_prospect(db_session, status="suggested")

        def _od():
            yield db_session

        def _forbid():
            raise HTTPException(403, "buyers only")

        app.dependency_overrides[get_db] = _od
        app.dependency_overrides[require_user] = lambda: test_user
        app.dependency_overrides[require_buyer] = _forbid
        try:
            with TestClient(app) as c:
                resp = c.post(f"/v2/partials/prospecting/{p.id}/claim")
                assert resp.status_code == 403
        finally:
            for dep in (get_db, require_user, require_buyer):
                app.dependency_overrides.pop(dep, None)


# ── Stats: canonical buyer-ready definition ───────────────────────────────


class TestStats:
    def test_stats_panel_renders_canonical_labels(self, client, db_session):
        make_prospect(db_session, status="suggested", fit_score=90, readiness_score=90)
        resp = client.get("/v2/partials/prospecting/stats")
        assert resp.status_code == 200
        assert "Buyer-ready" in resp.text
        assert "Suggested" in resp.text


# ── Phase 1: background enrichment + polling ──────────────────────────────


class TestEnrichmentJob:
    async def test_run_enrichment_job_marks_done(self, db_session):
        from app.services.prospect_free_enrichment import run_enrichment_job

        p = make_prospect(db_session)
        with (
            patch("app.services.prospect_free_enrichment.run_free_enrichment", new_callable=AsyncMock, return_value={}),
            patch("app.services.prospect_warm_intros.detect_warm_intros", return_value={"has_warm_intro": False}),
            patch("app.services.prospect_warm_intros.generate_one_liner", return_value="opener"),
        ):
            await run_enrichment_job(p.id, db=db_session)
        db_session.refresh(p)
        assert (p.enrichment_data or {}).get("enrich_status") == "done"
        assert (p.enrichment_data or {}).get("one_liner") == "opener"

    async def test_run_enrichment_job_marks_error(self, db_session):
        from app.services.prospect_free_enrichment import run_enrichment_job

        p = make_prospect(db_session)
        with patch(
            "app.services.prospect_free_enrichment.run_free_enrichment",
            new_callable=AsyncMock,
            return_value={"error": "sam down"},
        ):
            await run_enrichment_job(p.id, db=db_session)  # must not raise
        db_session.refresh(p)
        assert (p.enrichment_data or {}).get("enrich_status") == "error"

    async def test_run_enrichment_job_swallows_exceptions(self, db_session):
        from app.services.prospect_free_enrichment import run_enrichment_job

        p = make_prospect(db_session)
        with patch(
            "app.services.prospect_free_enrichment.run_free_enrichment",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            await run_enrichment_job(p.id, db=db_session)  # must not raise
        db_session.refresh(p)
        assert (p.enrichment_data or {}).get("enrich_status") == "error"


class TestEnrichRoute:
    def test_enrich_spawns_bg_and_returns_poller(self, client, db_session):
        p = make_prospect(db_session)
        with (
            patch("app.services.prospect_free_enrichment.run_enrichment_job") as job,
            patch("app.utils.async_helpers.safe_background_task", new_callable=AsyncMock) as sbt,
        ):
            resp = client.post(f"/v2/partials/prospecting/{p.id}/enrich")
        assert resp.status_code == 200
        assert "enrich-status" in resp.text  # returned the poller
        db_session.refresh(p)
        assert (p.enrichment_data or {}).get("enrich_status") == "running"
        job.assert_called_once_with(p.id)
        sbt.assert_called_once()

    def test_enrich_while_running_does_not_respawn(self, client, db_session):
        p = make_prospect(db_session, enrichment_data={"enrich_status": "running"})
        with (
            patch("app.services.prospect_free_enrichment.run_enrichment_job") as job,
            patch("app.utils.async_helpers.safe_background_task", new_callable=AsyncMock),
        ):
            resp = client.post(f"/v2/partials/prospecting/{p.id}/enrich")
        assert resp.status_code == 200
        assert "enrich-status" in resp.text
        job.assert_not_called()


class TestEnrichStatus:
    def test_status_running_keeps_polling(self, client, db_session):
        p = make_prospect(db_session, enrichment_data={"enrich_status": "running"})
        resp = client.get(f"/v2/partials/prospecting/{p.id}/enrich-status")
        assert resp.status_code == 200
        assert "every 2s" in resp.text

    def test_status_done_reloads_detail_and_stops(self, client, db_session):
        p = make_prospect(db_session, enrichment_data={"enrich_status": "done"})
        resp = client.get(f"/v2/partials/prospecting/{p.id}/enrich-status")
        assert resp.status_code == 286  # htmx stop-polling
        assert f"/v2/partials/prospecting/{p.id}" in resp.text  # reloads the detail
        assert "every 2s" not in resp.text  # poller gone

    def test_status_error_stops(self, client, db_session):
        p = make_prospect(db_session, enrichment_data={"enrich_status": "error"})
        resp = client.get(f"/v2/partials/prospecting/{p.id}/enrich-status")
        assert resp.status_code == 286
        assert "failed" in resp.text.lower()

    def test_status_missing_prospect_returns_286(self, client):
        resp = client.get("/v2/partials/prospecting/99999/enrich-status")
        assert resp.status_code == 286

    def test_status_stale_running_self_heals_and_stops(self, client, db_session):
        # A 'running' job whose worker died (started long ago) is treated as failed so
        # the poller stops instead of looping forever.
        from datetime import timedelta

        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        p = make_prospect(db_session, enrichment_data={"enrich_status": "running", "enrich_started_at": old})
        resp = client.get(f"/v2/partials/prospecting/{p.id}/enrich-status")
        assert resp.status_code == 286
        assert "failed" in resp.text.lower()


# ── Phase 2: live grid consistency (OOB card removal + stats refresh) ─────


class TestGridConsistency:
    def test_dismiss_in_suggested_filter_removes_card(self, client, db_session):
        p = make_prospect(db_session, status="suggested", name="ToDismiss")
        resp = client.post(
            f"/v2/partials/prospecting/{p.id}/dismiss",
            data={"flt_status": "suggested"},
            headers={"HX-Target": f"prospect-{p.id}"},  # grid action
        )
        assert resp.status_code == 200
        assert f'id="prospect-{p.id}"' not in resp.text  # card removed (left the filter)
        assert 'id="prospect-stats"' in resp.text  # stats OOB-refreshed

    def test_claim_in_all_filter_keeps_card(self, client, db_session):
        p = make_prospect(db_session, status="suggested", name="ToClaim")
        with patch("app.services.prospect_claim.trigger_deep_enrichment_bg", new_callable=AsyncMock):
            resp = client.post(
                f"/v2/partials/prospecting/{p.id}/claim",
                data={"flt_status": ""},  # default All view shows suggested+claimed
                headers={"HX-Target": f"prospect-{p.id}"},
            )
        assert resp.status_code == 200
        assert f'id="prospect-{p.id}"' in resp.text  # card kept (claimed still visible)
        assert "Claimed" in resp.text
        assert 'id="prospect-stats"' in resp.text

    def test_detail_action_returns_detail_no_oob(self, client, db_session):
        p = make_prospect(db_session, status="suggested")
        resp = client.post(
            f"/v2/partials/prospecting/{p.id}/dismiss",
            headers={"HX-Target": "main-content"},  # detail action
        )
        assert resp.status_code == 200
        assert "Buyer-ready score" in resp.text  # full detail returned
        assert 'id="prospect-stats"' not in resp.text  # no grid OOB on the detail path
