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

import json
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

    def test_expired_filter_pill_surfaces_expired(self, client, db_session):
        # H4: expired rows are hidden from the default view but reachable via a filter pill.
        make_prospect(db_session, name="ExpiredCo", status="expired")
        default = client.get("/v2/partials/prospecting")
        assert "ExpiredCo" not in default.text  # not in the default suggested+claimed view
        assert "status=expired" in default.text  # Expired pill is rendered
        filtered = client.get("/v2/partials/prospecting?status=expired")
        assert "ExpiredCo" in filtered.text

    def test_default_ai_match_sort_ranks_by_trio_then_readiness(self, client, db_session):
        # M5: the default ai_match_desc sort (ai_screen off) ranks in SQL by
        # trio_match_score first — high-trio outranks a high-readiness low-trio row.
        make_prospect(db_session, name="AlphaTrio", trio_match_score=90, readiness_score=5)
        make_prospect(db_session, name="BravoReady", trio_match_score=5, readiness_score=90)
        resp = client.get("/v2/partials/prospecting")  # default sort=ai_match_desc
        assert resp.status_code == 200
        body = resp.text
        assert body.index("AlphaTrio") < body.index("BravoReady")

    def test_default_ai_match_sort_paginates(self, client, db_session):
        # M5: the SQL path honors per_page (page 1 of 1) without hydrating the whole pool.
        for i in range(3):
            make_prospect(db_session, name=f"PageCo{i}", trio_match_score=50 - i)
        resp = client.get("/v2/partials/prospecting?per_page=2")
        assert resp.status_code == 200
        # 3 rows, 2 per page → pagination control appears.
        assert "Page 1 of 2" in resp.text

    # ── M2: per-user scope (See-All / See-Mine) ──────────────────────────
    def test_scope_mine_filters_to_own_claims(self, client, db_session, test_user, manager_user):
        make_prospect(db_session, name="MyClaim", status="claimed", claimed_by=test_user.id)
        make_prospect(db_session, name="TheirClaim", status="claimed", claimed_by=manager_user.id)
        resp = client.get("/v2/partials/prospecting?status=claimed&scope=mine")
        assert resp.status_code == 200
        assert "MyClaim" in resp.text
        assert "TheirClaim" not in resp.text

    def test_scope_all_shows_every_claim(self, client, db_session, test_user, manager_user):
        make_prospect(db_session, name="MyClaim2", status="claimed", claimed_by=test_user.id)
        make_prospect(db_session, name="TheirClaim2", status="claimed", claimed_by=manager_user.id)
        resp = client.get("/v2/partials/prospecting?status=claimed&scope=all")
        assert "MyClaim2" in resp.text
        assert "TheirClaim2" in resp.text

    def test_scope_toggle_and_carrier_rendered(self, client, db_session):
        resp = client.get("/v2/partials/prospecting?scope=mine")
        # Hidden carrier preserves scope across search/sort; toggle button is present.
        assert 'name="scope" value="mine"' in resp.text
        assert "scope=all" in resp.text and "scope=mine" in resp.text


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


# ── Phase 4: manager reassign route (cooldown override) ───────────────────


class TestReassignRoute:
    def _swept_prospect(self, db_session, *, owner_id):
        from datetime import timedelta

        domain = f"swept-{uuid.uuid4().hex[:6]}.com"
        co = Company(name="Swept Co", domain=domain, is_active=True, account_owner_id=None)
        db_session.add(co)
        db_session.commit()
        db_session.refresh(co)
        p = make_prospect(
            db_session,
            status="suggested",
            domain=domain,
            company_id=co.id,
            swept_from_owner_id=owner_id,
            swept_at=datetime.now(timezone.utc),
            reclaim_blocked_until=datetime.now(timezone.utc) + timedelta(days=15),
        )
        return co, p

    def test_rep_reassign_denied_shows_error_toast(self, client, db_session, test_user):
        # The default `client` is authenticated as a buyer (test_user) → reassign is denied.
        # HTMX suppresses non-2xx swaps, so instead of a silent 403 no-op the handler returns
        # 200 with HX-Reswap:none + an error showToast (honest feedback) and reassigns nothing.
        co, p = self._swept_prospect(db_session, owner_id=test_user.id)
        resp = client.post(
            f"/v2/partials/prospects/{p.id}/reassign",
            data={"to_user_id": str(test_user.id)},
        )
        assert resp.status_code == 200
        assert resp.headers.get("HX-Reswap") == "none"
        trigger = json.loads(resp.headers["HX-Trigger"])
        assert trigger["showToast"]["type"] == "error"
        assert "manager or admin" in trigger["showToast"]["message"]
        # The denied action changed nothing — still a swept suggestion, owner not set.
        db_session.refresh(p)
        db_session.refresh(co)
        assert p.status == "suggested"
        assert co.account_owner_id is None

    def test_manager_reassign_sets_owner_and_dismisses(self, db_session, test_user, manager_user):
        from app.database import get_db
        from app.dependencies import require_user
        from app.main import app

        co, p = self._swept_prospect(db_session, owner_id=test_user.id)

        def _od():
            yield db_session

        app.dependency_overrides[get_db] = _od
        app.dependency_overrides[require_user] = lambda: manager_user
        try:
            with TestClient(app) as c:
                resp = c.post(
                    f"/v2/partials/prospects/{p.id}/reassign",
                    data={"to_user_id": str(test_user.id)},
                )
            assert resp.status_code == 200
        finally:
            for dep in (get_db, require_user):
                app.dependency_overrides.pop(dep, None)

        db_session.refresh(co)
        assert co.account_owner_id == test_user.id
        db_session.refresh(p)
        assert p.status == "dismissed"
        assert p.reclaim_blocked_until is None


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


class TestEnrichStaleEscapeHatch:
    """H2 — a 'running' status left by a crashed worker must NOT wedge enrichment.

    The stale-guard was only wired into the poll route; the detail context (Enrich-button
    disable) and the enrich trigger (double-trigger guard) checked the raw flag, so a
    dead-worker 'running' disabled Enrich forever and Retry just looped. All three now
    route through the shared _enrich_in_progress predicate: stale 'running' = re-enrichable
    everywhere; fresh 'running' = still protected.
    """

    @staticmethod
    def _stale_iso():
        from datetime import timedelta

        return (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()

    @staticmethod
    def _fresh_iso():
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _enrich_button_disabled(html: str) -> bool:
        """True iff the detail's Enrich button carries a standalone `disabled` attribute
        (not the always-present `disabled:opacity-50` class)."""
        import re

        m = re.search(r'<button[^>]*?/enrich"[^>]*?>', html)
        assert m, "Enrich button not found in detail HTML"
        return bool(re.search(r"\sdisabled[\s>]", m.group(0)))

    # ── the shared guard itself ──
    def test_in_progress_predicate(self):
        from app.routers.htmx.prospecting import _enrich_in_progress

        assert _enrich_in_progress({"enrich_status": "running", "enrich_started_at": self._fresh_iso()}) is True
        assert _enrich_in_progress({"enrich_status": "running"}) is True  # no timestamp → treat as fresh
        assert _enrich_in_progress({"enrich_status": "running", "enrich_started_at": self._stale_iso()}) is False
        assert _enrich_in_progress({"enrich_status": "done"}) is False
        assert _enrich_in_progress({}) is False
        assert _enrich_in_progress(None) is False

    # ── detail context: button re-enabled when stale, still disabled when fresh ──
    def test_detail_stale_running_reenables_enrich_button(self, client, db_session):
        p = make_prospect(
            db_session,
            status="suggested",
            enrichment_data={"enrich_status": "running", "enrich_started_at": self._stale_iso()},
        )
        resp = client.get(f"/v2/partials/prospecting/{p.id}")
        assert resp.status_code == 200
        assert self._enrich_button_disabled(resp.text) is False  # re-enrichable, not wedged
        assert "every 2s" not in resp.text  # stale poller not resumed

    def test_detail_fresh_running_keeps_enrich_button_disabled(self, client, db_session):
        p = make_prospect(
            db_session,
            status="suggested",
            enrichment_data={"enrich_status": "running", "enrich_started_at": self._fresh_iso()},
        )
        resp = client.get(f"/v2/partials/prospecting/{p.id}")
        assert resp.status_code == 200
        assert self._enrich_button_disabled(resp.text) is True  # genuine in-flight job protected
        assert "every 2s" in resp.text  # poller resumed

    # ── enrich trigger: stale respawns a fresh job, fresh does not ──
    def test_enrich_trigger_respawns_on_stale_running(self, client, db_session):
        p = make_prospect(
            db_session,
            enrichment_data={"enrich_status": "running", "enrich_started_at": self._stale_iso()},
        )
        with (
            patch("app.services.prospect_free_enrichment.run_enrichment_job") as job,
            patch("app.utils.async_helpers.safe_background_task", new_callable=AsyncMock) as sbt,
        ):
            resp = client.post(f"/v2/partials/prospecting/{p.id}/enrich")
        assert resp.status_code == 200
        assert "enrich-status" in resp.text
        job.assert_called_once_with(p.id)  # crashed-worker job restarted
        sbt.assert_called_once()
        db_session.refresh(p)
        ed = p.enrichment_data or {}
        assert ed.get("enrich_status") == "running"
        assert not _stale_started(ed.get("enrich_started_at"))  # timestamp refreshed to now

    def test_enrich_trigger_does_not_respawn_fresh_running(self, client, db_session):
        started = self._fresh_iso()
        p = make_prospect(
            db_session,
            enrichment_data={"enrich_status": "running", "enrich_started_at": started},
        )
        with (
            patch("app.services.prospect_free_enrichment.run_enrichment_job") as job,
            patch("app.utils.async_helpers.safe_background_task", new_callable=AsyncMock),
        ):
            resp = client.post(f"/v2/partials/prospecting/{p.id}/enrich")
        assert resp.status_code == 200
        job.assert_not_called()  # genuine in-flight job not double-triggered
        db_session.refresh(p)
        assert (p.enrichment_data or {}).get("enrich_started_at") == started  # untouched


def _stale_started(started_iso) -> bool:
    from app.routers.htmx.prospecting import _enrich_is_stale

    return _enrich_is_stale(started_iso)


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


# ── Convert to opportunity (H1/M4) ────────────────────────────────────────


class TestConvertToOpportunity:
    def test_mark_prospect_converted_flips_claimed(self, db_session, test_user):
        from app.services.prospect_claim import mark_prospect_converted

        p = make_prospect(db_session, status="claimed", claimed_by=test_user.id)
        assert mark_prospect_converted(p.id, test_user.id, db_session) is True
        db_session.refresh(p)
        assert p.status == "converted"
        assert (p.enrichment_data or {}).get("converted_by") == test_user.id

    def test_mark_prospect_converted_noop_when_not_claimed(self, db_session, test_user):
        from app.services.prospect_claim import mark_prospect_converted

        p = make_prospect(db_session, status="suggested")
        assert mark_prospect_converted(p.id, test_user.id, db_session) is False
        db_session.refresh(p)
        assert p.status == "suggested"

    def test_import_save_with_prospect_id_converts(self, client, db_session, test_user):
        """Creating a requisition from a claimed prospect flips it to CONVERTED (H1)."""
        from app.services.prospect_claim import claim_prospect

        p = make_prospect(db_session, status="suggested")
        claim_prospect(p.id, test_user.id, db_session)  # service claim; no bg enrichment
        db_session.refresh(p)
        assert p.status == "claimed"

        resp = client.post(
            "/v2/partials/requisitions/import-save",
            data={
                "name": "Prospect RFQ",
                "prospect_id": str(p.id),
                "urgency": "normal",
                "reqs[0].primary_mpn": "LM317T",
                "reqs[0].manufacturer": "Texas Instruments",
                "reqs[0].target_qty": "100",
                "reqs[0].condition": "new",
            },
        )
        assert resp.status_code == 200
        db_session.refresh(p)
        assert p.status == "converted"

    def test_import_save_without_prospect_id_unaffected(self, client, db_session, test_user):
        """A plain requisition create (no prospect_id) touches no prospect."""
        p = make_prospect(db_session, status="claimed", claimed_by=test_user.id)
        resp = client.post(
            "/v2/partials/requisitions/import-save",
            data={
                "name": "Plain RFQ",
                "urgency": "normal",
                "reqs[0].primary_mpn": "LM317T",
                "reqs[0].manufacturer": "Texas Instruments",
                "reqs[0].target_qty": "100",
                "reqs[0].condition": "new",
            },
        )
        assert resp.status_code == 200
        db_session.refresh(p)
        assert p.status == "claimed"  # untouched

    def test_create_form_prefills_from_company(self, client, db_session, test_user):
        """create-form?company_id=..

        prefills the customer picker + carries prospect_id.
        """
        from app.services.prospect_claim import claim_prospect

        p = make_prospect(db_session, status="suggested", name="Prefill Co")
        claim_prospect(p.id, test_user.id, db_session)  # service claim; no bg enrichment
        db_session.refresh(p)

        resp = client.get(
            f"/v2/partials/requisitions/create-form?prospect_id={p.id}&company_id={p.company_id}&customer_name=Prefill+Co"
        )
        assert resp.status_code == 200
        # Hidden prospect_id rides through for the conversion flip.
        assert f'name="prospect_id" value="{p.id}"' in resp.text
        # Customer picker is prefilled with the linked company.
        assert "Prefill Co" in resp.text
        assert "selectedName" in resp.text

    def test_create_form_plain_has_no_prefill(self, client, db_session, test_user):
        """Create-form with no params is the plain modal (empty prospect_id)."""
        resp = client.get("/v2/partials/requisitions/create-form")
        assert resp.status_code == 200
        assert "unifiedReqModal" in resp.text
        assert 'name="prospect_id" value=""' in resp.text

    def test_converted_pill_and_filter(self, client, db_session, test_user):
        """The Converted pill exists; status=converted surfaces converted prospects."""
        p = make_prospect(db_session, status="converted", name="WonDeal", claimed_by=test_user.id)

        resp_all = client.get("/v2/partials/prospecting")
        assert resp_all.status_code == 200
        assert "Converted" in resp_all.text  # filter pill present
        assert "WonDeal" not in resp_all.text  # converted hidden in the default view

        resp_conv = client.get("/v2/partials/prospecting?status=converted")
        assert resp_conv.status_code == 200
        assert "WonDeal" in resp_conv.text  # visible under the Converted filter
        assert str(p.id) in resp_conv.text
