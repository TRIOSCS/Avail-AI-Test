"""Tests for the prospect scheduler — Phase 8 monthly cycle jobs.

Covers: rotation logic, kill switch, expire/resurface logic,
        score refresh, health report, and job isolation.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.constants import DiscoveryBatchStatus
from app.models.discovery_batch import DiscoveryBatch
from app.models.prospect_account import ProspectAccount
from app.services.prospect_scheduler import (
    DISCOVERY_ROTATION,
    _persist_discovery_results,
    get_next_discovery_slice,
    job_discover_prospects,
    job_enrich_pool,
    job_expire_and_resurface,
    job_find_contacts,
    job_pool_health_report,
    job_refresh_scores,
)

# ── Helpers ──────────────────────────────────────────────────────────


def _make_prospect(db: Session, **overrides) -> ProspectAccount:
    defaults = {
        "name": "Test Corp",
        "domain": "testcorp.com",
        "industry": "Electronics",
        "region": "US",
        "fit_score": 60,
        "readiness_score": 50,
        "status": "suggested",
        "discovery_source": "explorium",
        "readiness_signals": {},
        "contacts_preview": [],
        "similar_customers": [],
    }
    defaults.update(overrides)
    p = ProspectAccount(**defaults)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _make_batch(db: Session, **overrides) -> DiscoveryBatch:
    defaults = {
        "batch_id": f"batch-{id(overrides)}",
        "source": "explorium",
        "status": DiscoveryBatchStatus.COMPLETED,
        "started_at": datetime.now(timezone.utc),
    }
    defaults.update(overrides)
    b = DiscoveryBatch(**defaults)
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


class TestPersistScoresAtCreation:
    """_persist_discovery_results scores rows at persist (not just the monthly
    refresh)."""

    def test_persisted_prospect_is_scored(self, db_session):
        from app.schemas.prospect_account import ProspectAccountCreate

        batch = _make_batch(db_session, batch_id="score-at-persist")
        pc = ProspectAccountCreate(
            name="Acme Aerospace",
            domain="acme-aero.com",
            industry="Aerospace & Defense",
            naics_code="336412",
            employee_count_range="201-500",
            region="US",
            discovery_source="email_history",
        )
        n = _persist_discovery_results(db_session, batch, [pc])
        db_session.commit()
        assert n == 1
        saved = db_session.query(ProspectAccount).filter_by(domain="acme-aero.com").first()
        # Scored at persist: fit_reasoning is set (NULL by default) and a fit score computed.
        assert saved.fit_reasoning is not None
        assert saved.fit_score and saved.fit_score > 0

    def test_bare_prospect_persists_without_crash(self, db_session):
        from app.schemas.prospect_account import ProspectAccountCreate

        batch = _make_batch(db_session, batch_id="score-bare")
        pc = ProspectAccountCreate(name="x.com", domain="x.com", discovery_source="email_history")
        _persist_discovery_results(db_session, batch, [pc])
        db_session.commit()
        saved = db_session.query(ProspectAccount).filter_by(domain="x.com").first()
        # Bare prospect still gets a (low) score, not left at the column default unscored.
        assert saved.fit_reasoning is not None


# ── Rotation Logic ──────────────────────────────────────────────────


class TestDiscoveryRotation:
    def test_first_run_picks_aerospace_us(self, db_session):
        """No previous batches → start with Aerospace US."""
        result = get_next_discovery_slice(db_session)
        assert result["segment"] == "Aerospace & Defense"
        assert result["regions"] == ["US"]

    @pytest.mark.parametrize(
        ("batch_id", "prev_segment", "prev_regions", "next_segment", "next_regions"),
        [
            pytest.param(
                "b1",
                "Aerospace & Defense",
                ["US"],
                "Aerospace & Defense",
                ["EU", "Asia"],
                id="aerospace_us-to-aerospace_eu_asia",
            ),
            pytest.param(
                "b2",
                "Aerospace & Defense",
                ["EU", "Asia"],
                "Service Supply Chain",
                ["US"],
                id="aerospace_eu-to-service_us",
            ),
            pytest.param(
                "b3",
                "Service Supply Chain",
                ["US"],
                "Service Supply Chain",
                ["EU", "Asia"],
                id="service_us-to-service_eu",
            ),
            pytest.param(
                "b4",
                "Service Supply Chain",
                ["EU", "Asia"],
                "EMS / Electronics Mfg",
                None,
                id="service_eu-to-ems",
            ),
            pytest.param(
                "b5",
                "EMS / Electronics Mfg",
                ["US", "EU", "Asia"],
                "Automotive + catch-all",
                None,
                id="ems-to-automotive",
            ),
            pytest.param(
                "b6",
                "Automotive + catch-all",
                ["US", "EU", "Asia"],
                "Aerospace & Defense",
                ["US"],
                id="automotive-wraps-to-aerospace_us",
            ),
        ],
    )
    def test_rotation_advances(self, db_session, batch_id, prev_segment, prev_regions, next_segment, next_regions):
        """Each completed slice advances to the next slice; month 6 wraps to the
        start."""
        _make_batch(db_session, batch_id=batch_id, segment=prev_segment, regions=prev_regions)
        result = get_next_discovery_slice(db_session)
        assert result["segment"] == next_segment
        if next_regions is not None:
            assert result["regions"] == next_regions

    def test_uses_most_recent_batch(self, db_session):
        """Multiple batches — picks based on most recent."""
        _make_batch(
            db_session,
            batch_id="old",
            segment="Aerospace & Defense",
            regions=["US"],
            created_at=datetime.now(timezone.utc) - timedelta(days=60),
        )
        _make_batch(
            db_session,
            batch_id="new",
            segment="Service Supply Chain",
            regions=["EU", "Asia"],
            created_at=datetime.now(timezone.utc),
        )
        result = get_next_discovery_slice(db_session)
        assert result["segment"] == "EMS / Electronics Mfg"

    def test_ignores_running_batches(self, db_session):
        _make_batch(
            db_session,
            batch_id="running",
            segment="Aerospace & Defense",
            regions=["US"],
            status="running",
        )
        result = get_next_discovery_slice(db_session)
        assert result["segment"] == "Aerospace & Defense"
        assert result["regions"] == ["US"]

    def test_ignores_non_explorium_batches(self, db_session):
        _make_batch(
            db_session,
            batch_id="email",
            source="email_mining",
            segment="Aerospace & Defense",
            regions=["US"],
        )
        result = get_next_discovery_slice(db_session)
        assert result["segment"] == "Aerospace & Defense"
        assert result["regions"] == ["US"]

    def test_unrecognized_segment_resets(self, db_session):
        _make_batch(
            db_session,
            batch_id="weird",
            segment="Unknown Segment",
            regions=["US"],
        )
        result = get_next_discovery_slice(db_session)
        assert result["segment"] == "Aerospace & Defense"

    def test_rotation_has_6_slots(self):
        assert len(DISCOVERY_ROTATION) == 6

    def test_rotation_slices_map_to_real_explorium_keys(self):
        """H6: every rotation slot's segment_keys/regions must resolve to real
        SEGMENT_SEARCH_PARAMS / REGIONS keys, else run_explorium_discovery_batch would
        drop them and scan nothing (or the wrong cells)."""
        from app.services.prospect_discovery_explorium import REGIONS, SEGMENT_SEARCH_PARAMS

        for slot in DISCOVERY_ROTATION:
            assert slot["segment_keys"], f"{slot['segment']} has no segment_keys"
            for sk in slot["segment_keys"]:
                assert sk in SEGMENT_SEARCH_PARAMS, f"{sk} not a real segment key"
            for rk in slot["regions"]:
                assert rk in REGIONS, f"{rk} not a real region key"


# ── Kill Switch ─────────────────────────────────────────────────────


class TestKillSwitch:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "job",
        [
            pytest.param(job_discover_prospects, id="discover"),
            pytest.param(job_enrich_pool, id="enrich"),
            pytest.param(job_find_contacts, id="contacts"),
            pytest.param(job_refresh_scores, id="refresh"),
            pytest.param(job_expire_and_resurface, id="expire"),
        ],
    )
    async def test_job_skips_when_disabled(self, job):
        with patch("app.services.prospect_scheduler.settings") as mock_s:
            mock_s.prospecting_enabled = False
            result = await job()
        assert result["skipped"] is True


# ── Expire Logic ────────────────────────────────────────────────────


class TestExpireLogic:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("overrides", "expected_status"),
        [
            pytest.param(
                {
                    "name": "Old Low",
                    "domain": "oldlow.com",
                    "readiness_score": 30,
                    "created_at": datetime.now(timezone.utc) - timedelta(days=100),
                    "last_enriched_at": datetime.now(timezone.utc) - timedelta(days=70),
                },
                "expired",
                id="expires_old_low_readiness",
            ),
            pytest.param(
                {
                    "name": "Old High",
                    "domain": "oldhigh.com",
                    "readiness_score": 70,
                    "created_at": datetime.now(timezone.utc) - timedelta(days=100),
                    "last_enriched_at": datetime.now(timezone.utc) - timedelta(days=70),
                },
                "suggested",
                id="does_not_expire_high_readiness",
            ),
            pytest.param(
                {
                    "name": "Recent",
                    "domain": "recent.com",
                    "readiness_score": 30,
                    "created_at": datetime.now(timezone.utc) - timedelta(days=100),
                    "last_enriched_at": datetime.now(timezone.utc) - timedelta(days=10),
                },
                "suggested",
                id="does_not_expire_recently_enriched",
            ),
            pytest.param(
                {
                    "name": "Intent",
                    "domain": "intent.com",
                    "readiness_score": 30,
                    "readiness_signals": {"intent": {"strength": "strong"}},
                    "created_at": datetime.now(timezone.utc) - timedelta(days=100),
                    "last_enriched_at": datetime.now(timezone.utc) - timedelta(days=70),
                },
                "suggested",
                id="does_not_expire_strong_intent",
            ),
            pytest.param(
                {
                    "name": "Young",
                    "domain": "young.com",
                    "readiness_score": 20,
                    "created_at": datetime.now(timezone.utc) - timedelta(days=30),
                },
                "suggested",
                id="does_not_expire_young_prospect",
            ),
            pytest.param(
                {
                    "name": "Boundary",
                    "domain": "boundary.com",
                    "readiness_score": 20,
                    "created_at": datetime.now(timezone.utc) - timedelta(days=89),
                    "last_enriched_at": datetime.now(timezone.utc) - timedelta(days=70),
                },
                "suggested",
                id="within_90_day_boundary",
            ),
        ],
    )
    async def test_expire_status(self, db_session, overrides, expected_status):
        """Old low-readiness prospects expire; high-readiness / fresh / strong-intent /
        young / within-90-day-boundary prospects stay suggested."""
        p = _make_prospect(db_session, **overrides)
        with patch("app.database.SessionLocal", return_value=db_session), patch.object(db_session, "close"):
            await job_expire_and_resurface()
        db_session.refresh(p)
        assert p.status == expected_status


# ── Resurface Logic ─────────────────────────────────────────────────


class TestResurfaceLogic:
    @pytest.mark.asyncio
    async def test_resurfaces_dismissed_with_fresh_signals(self, db_session):
        p = _make_prospect(
            db_session,
            name="Dismissed",
            domain="dismissed.com",
            status="dismissed",
            readiness_score=50,
            readiness_signals={"intent": {"strength": "strong"}},
            last_enriched_at=datetime.now(timezone.utc) - timedelta(days=5),
            dismissed_at=datetime.now(timezone.utc) - timedelta(days=60),
        )
        with patch("app.database.SessionLocal", return_value=db_session), patch.object(db_session, "close"):
            await job_expire_and_resurface()
        db_session.refresh(p)
        assert p.status == "suggested"
        assert p.dismissed_by is None
        assert p.dismiss_reason is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("overrides", "expected_status"),
        [
            pytest.param(
                {
                    "name": "Expired",
                    "domain": "expired.com",
                    "status": "expired",
                    "readiness_score": 45,
                    "readiness_signals": {"hiring": {"type": "procurement"}},
                    "last_enriched_at": datetime.now(timezone.utc) - timedelta(days=10),
                },
                "suggested",
                id="resurfaces_expired_with_hiring_signals",
            ),
            pytest.param(
                {
                    "name": "NoSignals",
                    "domain": "nosignals.com",
                    "status": "dismissed",
                    "readiness_score": 50,
                    "readiness_signals": {},
                    "last_enriched_at": datetime.now(timezone.utc) - timedelta(days=5),
                },
                "dismissed",
                id="does_not_resurface_without_signals",
            ),
            pytest.param(
                {
                    "name": "LowRead",
                    "domain": "lowread.com",
                    "status": "dismissed",
                    "readiness_score": 20,
                    "readiness_signals": {"intent": {"strength": "strong"}},
                    "last_enriched_at": datetime.now(timezone.utc) - timedelta(days=5),
                },
                "dismissed",
                id="does_not_resurface_low_readiness",
            ),
            pytest.param(
                {
                    "name": "OldEnrich",
                    "domain": "oldenrich.com",
                    "status": "dismissed",
                    "readiness_score": 60,
                    "readiness_signals": {"intent": {"strength": "strong"}},
                    "last_enriched_at": datetime.now(timezone.utc) - timedelta(days=45),
                },
                "suggested",
                id="resurfaces_regardless_of_enrichment_age",
            ),
            pytest.param(
                {
                    "name": "ExpiredNeverEnriched",
                    "domain": "expired-never.com",
                    "status": "expired",
                    "readiness_score": 55,
                    "readiness_signals": {"intent": {"strength": "strong"}},
                    "last_enriched_at": None,
                },
                "suggested",
                id="resurfaces_expired_even_without_enrichment_timestamp",
            ),
        ],
    )
    async def test_resurface_status(self, db_session, overrides, expected_status):
        """Expired/dismissed prospects resurface on fresh signals AND readiness >= 40;
        low readiness or no signals do not.

        Enrichment age is NOT a gate (H4) — a strong- signal row resurfaces even if it
        was never re-enriched (its last_enriched_at is stale or NULL), because nothing
        re-enriches non-SUGGESTED rows.
        """
        p = _make_prospect(db_session, **overrides)
        with patch("app.database.SessionLocal", return_value=db_session), patch.object(db_session, "close"):
            await job_expire_and_resurface()
        db_session.refresh(p)
        assert p.status == expected_status


# ── Score Refresh ───────────────────────────────────────────────────


class TestScoreRefresh:
    @pytest.mark.asyncio
    async def test_refreshes_all_suggested(self, db_session):
        _make_prospect(db_session, name="A", domain="a.com", industry="Aerospace", fit_score=10)
        _make_prospect(db_session, name="B", domain="b.com", industry="Automotive", fit_score=10)
        _make_prospect(db_session, name="C", domain="c.com", status="claimed", fit_score=10)

        with patch("app.database.SessionLocal", return_value=db_session), patch.object(db_session, "close"):
            result = await job_refresh_scores()

        assert result["refreshed"] == 2  # Only suggested, not claimed

    @pytest.mark.asyncio
    async def test_tracks_upgrades_and_downgrades(self, db_session):
        # High initial score but no industry → will score lower
        _make_prospect(
            db_session,
            name="Down",
            domain="down.com",
            industry=None,
            fit_score=90,
            readiness_score=90,
        )
        with patch("app.database.SessionLocal", return_value=db_session), patch.object(db_session, "close"):
            result = await job_refresh_scores()
        # Should detect a downgrade since fit_score will drop without industry match
        assert result["refreshed"] == 1


# ── Health Report ───────────────────────────────────────────────────


class TestHealthReport:
    @pytest.mark.asyncio
    async def test_empty_pool_report(self, db_session):
        with patch("app.database.SessionLocal", return_value=db_session), patch.object(db_session, "close"):
            result = await job_pool_health_report()
        assert result["claimed_this_month"] == 0
        assert result["dismissed_this_month"] == 0
        assert result["credits_used_this_month"] == 0

    @pytest.mark.asyncio
    async def test_report_counts(self, db_session):
        _make_prospect(db_session, name="S1", domain="s1.com", status="suggested")
        _make_prospect(db_session, name="S2", domain="s2.com", status="suggested", region="EU")
        _make_prospect(
            db_session,
            name="C1",
            domain="c1.com",
            status="claimed",
            claimed_at=datetime.now(timezone.utc),
        )
        _make_prospect(
            db_session,
            name="D1",
            domain="d1.com",
            status="dismissed",
            dismissed_at=datetime.now(timezone.utc),
        )
        _make_batch(db_session, batch_id="b-report", credits_used=42)

        with patch("app.database.SessionLocal", return_value=db_session), patch.object(db_session, "close"):
            result = await job_pool_health_report()

        assert result["by_status"]["suggested"] == 2
        assert result["by_status"]["claimed"] == 1
        assert result["claimed_this_month"] == 1
        assert result["dismissed_this_month"] == 1
        assert result["credits_used_this_month"] == 42
        assert result["by_region"]["US"] == 1
        assert result["by_region"]["EU"] == 1


# ── Job Error Handling ──────────────────────────────────────────────


class TestJobErrorHandling:
    @pytest.mark.asyncio
    async def test_enrich_handles_service_error(self):
        with patch("app.services.prospect_scheduler.settings") as mock_s:
            mock_s.prospecting_enabled = True
            with patch(
                "app.services.prospect_signals.run_signal_enrichment_batch",
                new_callable=AsyncMock,
                side_effect=Exception("Service crash"),
            ):
                result = await job_enrich_pool()
        assert "error" in result

    @pytest.mark.asyncio
    async def test_contacts_handles_service_error(self):
        """When run_contact_enrichment_batch raises at runtime, job returns error."""
        with patch("app.services.prospect_scheduler.settings") as mock_s:
            mock_s.prospecting_enabled = True
            mock_s.prospecting_min_fit_for_contacts = 60
            with patch(
                "app.services.prospect_scheduler.run_contact_enrichment_batch",
                new_callable=AsyncMock,
                side_effect=Exception("Service crash"),
            ):
                result = await job_find_contacts()
        assert "error" in result

    @pytest.mark.asyncio
    async def test_health_report_handles_error(self):
        with patch(
            "app.database.SessionLocal",
            side_effect=Exception("DB unavailable"),
        ):
            result = await job_pool_health_report()
        assert "error" in result


# ── Scheduler Happy-Path Tests (coverage gap-fill) ─────────────────


class TestDiscoverProspectsJob:
    """Happy-path tests for job_discover_prospects — the most complex job."""

    @staticmethod
    def _connected_user(db):
        """Seed an M365-connected user so the email-mining branch can resolve a
        mailbox."""
        from app.models import User

        u = User(
            email="miner@trioscs.com",
            name="Inbox Miner",
            role="buyer",
            azure_id="miner-001",
            m365_connected=True,
            access_token="at",
            refresh_token="rt",
        )
        db.add(u)
        db.commit()
        return u

    @pytest.mark.asyncio
    async def test_discover_happy_path(self, db_session):
        """Full discovery job with mocked external services."""
        from app.schemas.prospect_account import ProspectAccountCreate

        self._connected_user(db_session)

        mock_explorium_result = ProspectAccountCreate(
            name="Discovered Corp",
            domain="discovered.com",
            industry="Aerospace",
            region="US",
            discovery_source="explorium",
        )

        mock_email_result = ProspectAccountCreate(
            name="Email Corp",
            domain="emailcorp.com",
            industry="Electronics",
            region="EU",
            discovery_source="email_history",
        )

        mock_run_explorium = AsyncMock(return_value=[mock_explorium_result])
        mock_run_email = AsyncMock(return_value=[mock_email_result])

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch("app.utils.token_manager.get_valid_token", new=AsyncMock(return_value="graph-tok")),
            patch("app.utils.graph_client.GraphClient", return_value=MagicMock()),
            patch.dict(
                "sys.modules",
                {
                    "app.services.prospect_discovery_explorium": MagicMock(
                        run_explorium_discovery_batch=mock_run_explorium,
                    ),
                    "app.services.prospect_discovery_email": MagicMock(
                        run_email_mining_batch=mock_run_email,
                    ),
                },
            ),
        ):
            result = await job_discover_prospects()

        assert result["explorium_count"] == 1
        assert result["email_count"] == 1
        assert "batch_id" in result
        # S10 regression: the mining batch was invoked with a real GraphClient token path.
        mock_run_email.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_discover_explorium_fails_email_continues(self, db_session):
        """Explorium failure doesn't block email mining."""
        from app.schemas.prospect_account import ProspectAccountCreate

        self._connected_user(db_session)

        mock_email_result = ProspectAccountCreate(
            name="Email Only",
            domain="emailonly.com",
            discovery_source="email_history",
        )

        mock_run_explorium = AsyncMock(side_effect=Exception("Explorium down"))
        mock_run_email = AsyncMock(return_value=[mock_email_result])

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch("app.utils.token_manager.get_valid_token", new=AsyncMock(return_value="graph-tok")),
            patch("app.utils.graph_client.GraphClient", return_value=MagicMock()),
            patch.dict(
                "sys.modules",
                {
                    "app.services.prospect_discovery_explorium": MagicMock(
                        run_explorium_discovery_batch=mock_run_explorium,
                    ),
                    "app.services.prospect_discovery_email": MagicMock(
                        run_email_mining_batch=mock_run_email,
                    ),
                },
            ),
        ):
            result = await job_discover_prospects()

        assert result["explorium_count"] == 0
        assert result["email_count"] == 1

    @pytest.mark.asyncio
    async def test_discover_both_fail_still_completes(self, db_session):
        """Both sources fail — batch still completes with 0 results."""
        self._connected_user(db_session)

        mock_run_explorium = AsyncMock(side_effect=Exception("Explorium down"))

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            # Token acquisition blows up — email mining must swallow it (no crash) and report 0.
            patch("app.utils.token_manager.get_valid_token", new=AsyncMock(side_effect=Exception("No graph token"))),
            patch.dict(
                "sys.modules",
                {
                    "app.services.prospect_discovery_explorium": MagicMock(
                        run_explorium_discovery_batch=mock_run_explorium,
                    ),
                },
            ),
        ):
            result = await job_discover_prospects()

        assert result["explorium_count"] == 0
        assert result["email_count"] == 0

    @pytest.mark.asyncio
    async def test_discover_no_connected_user_skips_email_mining(self, db_session):
        """S10: with no M365-connected user, email mining is skipped (no crash, count=0)."""
        mock_run_explorium = AsyncMock(return_value=[])
        mock_run_email = AsyncMock(return_value=[])

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch.dict(
                "sys.modules",
                {
                    "app.services.prospect_discovery_explorium": MagicMock(
                        run_explorium_discovery_batch=mock_run_explorium,
                    ),
                    "app.services.prospect_discovery_email": MagicMock(
                        run_email_mining_batch=mock_run_email,
                    ),
                },
            ),
        ):
            result = await job_discover_prospects()

        assert result["email_count"] == 0
        # No mailbox → mining batch never runs (and never raises NameError on get_graph_client).
        mock_run_email.assert_not_awaited()


class TestEnrichPoolJob:
    """Happy-path for job_enrich_pool."""

    @pytest.mark.asyncio
    async def test_enrich_happy_path(self):
        mock_result = {"enriched": 10, "errors": 0}

        with patch(
            "app.services.prospect_signals.run_signal_enrichment_batch",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            result = await job_enrich_pool()

        assert result == mock_result


class TestFindContactsJob:
    """job_find_contacts is wired to the real contact-enrichment batch (C1 fix)."""

    def test_batch_symbol_resolves_at_module_top(self):
        """Regression for C1: the batch is imported at scheduler module top, so a
        missing symbol fails loudly at import instead of being swallowed by the job's
        blanket except."""
        import app.services.prospect_scheduler as sched

        assert callable(sched.run_contact_enrichment_batch)

    @pytest.mark.asyncio
    async def test_contacts_happy_path_invokes_batch(self):
        """The job resolves and awaits run_contact_enrichment_batch, forwarding the
        configured min-fit gate and returning its summary."""
        mock_summary = {
            "prospects_processed": 4,
            "total_verified": 7,
            "total_contacts": 12,
            "errors": 0,
        }
        with patch("app.services.prospect_scheduler.settings") as mock_s:
            mock_s.prospecting_enabled = True
            mock_s.prospecting_min_fit_for_contacts = 60
            with patch(
                "app.services.prospect_scheduler.run_contact_enrichment_batch",
                new_callable=AsyncMock,
                return_value=mock_summary,
            ) as mock_batch:
                result = await job_find_contacts()

        assert result == mock_summary
        mock_batch.assert_awaited_once_with(min_fit_score=60)


# ── Coverage Gap Tests ──────────────────────────────────────────────


class TestSchedulerCoverageGaps:
    """Cover exception paths and edge cases."""

    def test_ensure_utc_with_tz(self):
        """Line 35: _ensure_utc returns dt unchanged when it already has tzinfo."""
        from app.services.prospect_scheduler import _ensure_utc

        dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
        result = _ensure_utc(dt)
        assert result is dt  # same object, unchanged

    @pytest.mark.asyncio
    async def test_discover_prospects_exception_path(self):
        """Inner try blocks catch errors; function returns summary with 0 counts."""
        with (
            patch("app.database.SessionLocal") as mock_sl,
            patch(
                "app.services.prospect_scheduler.get_next_discovery_slice",
                return_value={"segment": "Test", "segment_keys": ["aerospace_defense"], "regions": ["US"]},
            ),
        ):
            mock_db = MagicMock()
            mock_sl.return_value = mock_db
            mock_db.query.side_effect = RuntimeError("DB exploded")

            result = await job_discover_prospects()

        # Inner try/except blocks handle errors gracefully — returns success
        # summary with zero counts, not an error dict.
        assert result["explorium_count"] == 0
        assert result["email_count"] == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("job", "boom"),
        [
            pytest.param(job_refresh_scores, "Score refresh exploded", id="refresh_scores"),
            pytest.param(job_expire_and_resurface, "Expire job exploded", id="expire_and_resurface"),
        ],
    )
    async def test_job_returns_error_on_exception(self, job, boom):
        """job_refresh_scores / job_expire_and_resurface return error on exception."""
        with patch("app.database.SessionLocal") as mock_sl:
            mock_db = MagicMock()
            mock_sl.return_value = mock_db
            mock_db.query.side_effect = RuntimeError(boom)

            result = await job()

        assert "error" in result
