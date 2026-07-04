"""Regression tests for the confirmed Prospecting audit findings (2026-07-03 audit).

Each test names the finding it locks down (H8, M1, M7-M10, M12, M13, M15-M19). They are
fail-before/pass-after guards: the assertion fails against the pre-fix code and passes
against the fix. Grouped by finding for traceability against
docs/superpowers/specs/2026-07-03-prospecting-audit.md.

Depends on: conftest db_session fixture; external paid APIs are always mocked.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.responses import HTMLResponse

from app.models.discovery_batch import DiscoveryBatch
from app.models.prospect_account import ProspectAccount
from app.schemas.prospect_account import ProspectAccountCreate
from app.services.enrichment_credit_guard import ProviderQuotaError

EXPL = "app.services.prospect_discovery_explorium"


def _prospect(db, **kw) -> ProspectAccount:
    defaults = dict(
        name=f"P-{uuid.uuid4().hex[:6]}",
        domain=f"p-{uuid.uuid4().hex[:6]}.com",
        status="suggested",
        discovery_source="manual",
    )
    defaults.update(kw)
    p = ProspectAccount(**defaults)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _batch(db, **kw) -> DiscoveryBatch:
    from datetime import datetime, timezone

    defaults = {
        "batch_id": "audit-batch",
        "source": "explorium",
        "status": "running",
        "started_at": datetime.now(timezone.utc),
    }
    defaults.update(kw)
    b = DiscoveryBatch(**defaults)
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


# ── H8 — one duplicate domain must not abort the whole discovery persist ──────


class TestH8DiscoveryPersistPerRow:
    def test_duplicate_domain_does_not_abort_batch(self, db_session):
        from app.services.prospect_scheduler import _persist_discovery_results

        batch = _batch(db_session, batch_id="h8")
        # Occupy the unique domain that a re-discovery will collide with.
        db_session.add(ProspectAccount(name="Existing", domain="dup-h8.com", discovery_source="manual"))
        db_session.commit()

        results = [
            ProspectAccountCreate(name="Dup", domain="dup-h8.com", discovery_source="explorium"),
            ProspectAccountCreate(name="Fresh", domain="fresh-h8.com", discovery_source="explorium"),
            ProspectAccountCreate(name="Dup2", domain="dup-h8.com", discovery_source="explorium"),
        ]
        saved = _persist_discovery_results(db_session, batch, results)
        db_session.commit()  # pre-fix: IntegrityError aborts the whole batch here

        assert saved == 1
        assert db_session.query(ProspectAccount).filter_by(domain="fresh-h8.com").first() is not None


# ── M15 — discovery credits_used must be recorded ─────────────────────────────


class TestM15CreditsRecorded:
    @pytest.mark.asyncio
    async def test_batch_reports_credits_via_meter(self):
        from app.services.prospect_discovery_explorium import run_explorium_discovery_batch

        meter: dict = {}
        raw = [
            {"company_name": "A", "domain": "a-m15.com", "country_code": "US"},
            {"company_name": "B", "domain": "b-m15.com", "country_code": "US"},
        ]
        with (
            patch(f"{EXPL}._get_api_key", return_value="key"),
            patch(f"{EXPL}.explorium.discover_businesses", new_callable=AsyncMock, return_value=raw),
            patch(f"{EXPL}.calculate_fit_score", return_value=(70, "fit")),
            patch(f"{EXPL}.calculate_readiness_score", return_value=(50, {})),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await run_explorium_discovery_batch(
                "m15", set(), segment_keys=["aerospace_defense"], region_keys=["US"], credit_meter=meter
            )
        assert meter["credits_est"] == 2

    @pytest.mark.asyncio
    async def test_scheduler_writes_credits_used(self, db_session):
        from app.services.prospect_scheduler import job_discover_prospects

        async def _explorium(*_a, credit_meter=None, **_k):
            if credit_meter is not None:
                credit_meter["credits_est"] = 9
            return [ProspectAccountCreate(name="C", domain="c-m15.com", discovery_source="explorium")]

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch.dict(
                "sys.modules",
                {EXPL: MagicMock(run_explorium_discovery_batch=AsyncMock(side_effect=_explorium))},
            ),
        ):
            await job_discover_prospects()

        batch = (
            db_session.query(DiscoveryBatch).filter_by(source="explorium").order_by(DiscoveryBatch.id.desc()).first()
        )
        assert batch is not None
        assert batch.credits_used == 9


# ── M8 — scheduler refresh uses the shared scorers (historical bonus + composite) ─


class TestM8SharedScorers:
    @pytest.mark.asyncio
    async def test_refresh_applies_historical_bonus(self, db_session):
        from datetime import datetime, timezone

        from app.services.prospect_scheduler import job_refresh_scores

        p = ProspectAccount(
            name="Warm Co",
            domain="warm-m8.com",
            discovery_source="reactivation",
            status="suggested",
            historical_context={
                "bought_before": True,
                "quote_count": 25,
                "last_activity": str(datetime.now(timezone.utc).year),
            },
        )
        db_session.add(p)
        db_session.commit()

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch("app.services.prospect_scheduler.calculate_fit_score", return_value=(50, "base")),
            patch("app.services.prospect_scheduler.calculate_readiness_score", return_value=(40, {})),
        ):
            await job_refresh_scores()

        db_session.refresh(p)
        # bought_before → fit +15; recent last_activity → readiness +10; quote_count>20 → +5.
        # Pre-fix: no bonus applied → fit stayed 50, readiness 40.
        assert p.fit_score == 65
        assert p.readiness_score == 55


# ── M16 — email-discovery exclusion sets must be unbounded ────────────────────


class TestM16UnboundedExclusion:
    @pytest.mark.asyncio
    async def test_prospect_and_vendor_exclusions_not_capped(self, db_session):
        """No .limit() truncates the known-domain exclusion set (audit M16)."""
        from app.services.prospect_discovery_email import mine_unknown_domains

        # A known prospect domain must be excluded from mining regardless of pool size.
        db_session.add(ProspectAccount(name="Known", domain="known-m16.com", discovery_source="manual"))
        db_session.commit()

        graph = AsyncMock()
        graph.get_all_pages = AsyncMock(
            return_value=[
                {"from": {"emailAddress": {"address": "a@known-m16.com", "name": "A"}}},
                {"from": {"emailAddress": {"address": "b@known-m16.com", "name": "B"}}},
                {"from": {"emailAddress": {"address": "c@fresh-m16.com", "name": "C"}}},
                {"from": {"emailAddress": {"address": "d@fresh-m16.com", "name": "D"}}},
            ]
        )
        results = await mine_unknown_domains(graph, db_session, days_back=30)
        domains = {r["domain"] for r in results}
        assert "known-m16.com" not in domains  # excluded (was a known prospect)
        assert "fresh-m16.com" in domains

    def test_no_limit_in_exclusion_queries(self):
        import inspect

        from app.services import prospect_discovery_email as mod

        src = inspect.getsource(mod.mine_unknown_domains)
        assert ".limit(5000)" not in src


# ── M1 — claim's domain-collision warning must reach the toast ────────────────


class TestM1ClaimWarningSurfaced:
    def test_domain_collision_warning_in_toast(self, client, db_session):
        p = _prospect(db_session)
        with (
            patch(
                "app.services.prospect_claim.claim_prospect",
                return_value={
                    "prospect_id": p.id,
                    "warning": "Linked to existing company 'Other Co' (same domain)",
                },
            ),
            patch("app.services.prospect_claim.trigger_deep_enrichment_bg", return_value=MagicMock()),
            patch("app.utils.async_helpers.safe_background_task", new_callable=AsyncMock),
            patch("app.routers.htmx.prospecting.template_response", return_value=HTMLResponse("<html/>")),
        ):
            resp = client.post(f"/v2/partials/prospecting/{p.id}/claim")
        assert resp.status_code == 200
        trigger = resp.headers.get("HX-Trigger", "")
        # Pre-fix: the router discarded the return value and toasted a flat "Claimed X".
        assert "same domain" in trigger
        assert "warning" in trigger

    def test_plain_claim_still_success(self, client, db_session):
        p = _prospect(db_session)
        with (
            patch("app.services.prospect_claim.claim_prospect", return_value={"prospect_id": p.id}),
            patch("app.services.prospect_claim.trigger_deep_enrichment_bg", return_value=MagicMock()),
            patch("app.utils.async_helpers.safe_background_task", new_callable=AsyncMock),
            patch("app.routers.htmx.prospecting.template_response", return_value=HTMLResponse("<html/>")),
        ):
            resp = client.post(f"/v2/partials/prospecting/{p.id}/claim")
        assert "success" in resp.headers.get("HX-Trigger", "")


# ── M17 — dismiss is a service (thin router) + captures a reason ──────────────


class TestM17DismissService:
    def test_dismiss_sets_status_and_reason(self, db_session, test_user):
        from app.constants import ProspectAccountStatus
        from app.services.prospect_claim import dismiss_prospect

        p = _prospect(db_session)
        out = dismiss_prospect(p.id, user_id=test_user.id, db=db_session, reason="not_a_fit")
        db_session.refresh(p)
        assert out["status"] == "dismissed"
        assert p.status == ProspectAccountStatus.DISMISSED
        assert p.dismiss_reason == "not_a_fit"
        assert p.dismissed_by == test_user.id

    def test_dismiss_rejects_non_suggested(self, db_session, test_user):
        from app.services.prospect_claim import dismiss_prospect

        p = _prospect(db_session, status="claimed")
        with pytest.raises(ValueError):
            dismiss_prospect(p.id, test_user.id, db_session)

    def test_dismiss_button_has_confirm(self):
        from pathlib import Path

        for tmpl in ("_card.html", "detail.html"):
            html = Path(f"app/templates/htmx/partials/prospecting/{tmpl}").read_text()
            assert "/dismiss" in html and "hx-confirm" in html


# ── M7 — enrich locks the row before the read-check-write ─────────────────────


class TestM7EnrichRowLock:
    def test_enrich_endpoint_locks_row(self):
        import inspect

        from app.routers.htmx import prospecting as router

        src = inspect.getsource(router.enrich_prospect_htmx)
        # Pre-fix used db.get(...) with no lock; two clicks could both spawn a job.
        assert "with_for_update()" in src


# ── M13 — manual add adopts the winner on a duplicate-domain race ─────────────


class TestM13ManualAddRace:
    def test_adopts_existing_on_integrity_race(self, db_session, monkeypatch):
        from sqlalchemy.orm import Query

        from app.services import prospect_claim

        existing = _prospect(db_session, domain="race-m13.com", name="Race")

        real_first = Query.first
        calls = {"n": 0}

        def fake_first(self):
            # Hide the row from ONLY the pre-insert dedup check to force the TOCTOU race;
            # the post-IntegrityError adopt lookup (call 2) sees the real row.
            calls["n"] += 1
            if calls["n"] == 1:
                return None
            return real_first(self)

        monkeypatch.setattr(Query, "first", fake_first)
        out = prospect_claim.add_prospect_manually("race-m13.com", user_id=1, db=db_session)
        # Pre-fix: db.commit() raised IntegrityError to the caller (500).
        assert out["is_new"] is False
        assert out["prospect_id"] == existing.id


# ── M12 — reclaim/reassign under /v2/partials/prospects must be module-gated ──


class TestM12ReclaimReassignGuarded:
    @pytest.mark.parametrize(
        "path",
        [
            "/v2/partials/prospects/9/reclaim",
            "/v2/partials/prospects/9/reassign",
            "/v2/partials/prospects/9/reassign-form",
        ],
    )
    def test_prospects_plural_requires_prospecting_key(self, path):
        from app.access_paths import module_key_for_path
        from app.constants import AccessKey

        assert module_key_for_path(path) == AccessKey.PROSPECTING  # pre-fix: None

    def test_prospecting_singular_still_guarded(self):
        # The new plural base must not stop the -ing tab/grid from matching.
        from app.access_paths import module_key_for_path
        from app.constants import AccessKey

        assert module_key_for_path("/v2/partials/prospecting/9/claim") == AccessKey.PROSPECTING


# ── M18 — a non-string intent topic must not drop the whole page ──────────────


class TestM18IntentTopicTypeGuard:
    def test_nonstring_topic_ignored_not_crash(self):
        from app.services.prospect_discovery_explorium import normalize_explorium_result

        raw = {
            "company_name": "X",
            "domain": "x-m18.com",
            "business_intent_topics": [{"t": "a dict"}, None, "electronic components", "semiconductors"],
        }
        out = normalize_explorium_result(raw, "ems_electronics")  # pre-fix: AttributeError
        assert out["intent"]["component_topics"] == ["electronic components", "semiconductors"]


# ── M19 — ProviderQuotaError short-circuits discovery, not swallowed ──────────


class TestM19QuotaShortCircuit:
    @pytest.mark.asyncio
    async def test_discover_reraises_quota(self):
        from app.services.prospect_discovery_explorium import discover_companies_with_signals

        with (
            patch(f"{EXPL}._get_api_key", return_value="key"),
            patch(
                f"{EXPL}.explorium.discover_businesses",
                new_callable=AsyncMock,
                side_effect=ProviderQuotaError("402"),
            ),
        ):
            with pytest.raises(ProviderQuotaError):  # pre-fix: swallowed → returns []
                await discover_companies_with_signals("ems_electronics", "US")

    @pytest.mark.asyncio
    async def test_batch_stops_after_quota(self):
        from app.services.prospect_discovery_explorium import run_explorium_discovery_batch

        with (
            patch(f"{EXPL}._get_api_key", return_value="key"),
            patch(
                f"{EXPL}.discover_companies_with_signals",
                new_callable=AsyncMock,
                side_effect=ProviderQuotaError("429"),
            ) as mock_disc,
            patch("app.services.enrichment_credit_guard.trip_circuit") as mock_trip,
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            # Full matrix requested (12 cells) but the very first call hits quota.
            results = await run_explorium_discovery_batch("q", set())

        assert results == []
        mock_disc.assert_awaited_once()  # stopped after slice 1, did not burn all 12
        mock_trip.assert_called_once()
