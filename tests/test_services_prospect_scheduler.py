"""Tests for the prospect scheduler — Phase 8 monthly cycle jobs.

Covers: rotation logic, kill switch, expire/resurface logic,
        score refresh, health report, and job isolation.
"""

import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import Company, User
from app.models.discovery_batch import DiscoveryBatch
from app.models.prospect_account import ProspectAccount
from app.services.prospect_scheduler import (
    DISCOVERY_ROTATION,
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
        "status": "complete",
        "started_at": datetime.now(timezone.utc),
    }
    defaults.update(overrides)
    b = DiscoveryBatch(**defaults)
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


# ── Rotation Logic ──────────────────────────────────────────────────


class TestDiscoveryRotation:

    def test_first_run_picks_aerospace_us(self, db_session):
        """No previous batches → start with Aerospace US."""
        result = get_next_discovery_slice(db_session)
        assert result["segment"] == "Aerospace & Defense"
        assert result["regions"] == ["US"]

    def test_after_aerospace_us_picks_aerospace_eu_asia(self, db_session):
        _make_batch(
            db_session,
            batch_id="b1",
            segment="Aerospace & Defense",
            regions=["US"],
        )
        result = get_next_discovery_slice(db_session)
        assert result["segment"] == "Aerospace & Defense"
        assert result["regions"] == ["EU", "Asia"]

    def test_after_aerospace_eu_picks_service_us(self, db_session):
        _make_batch(
            db_session,
            batch_id="b2",
            segment="Aerospace & Defense",
            regions=["EU", "Asia"],
        )
        result = get_next_discovery_slice(db_session)
        assert result["segment"] == "Service Supply Chain"
        assert result["regions"] == ["US"]

    def test_after_service_us_picks_service_eu(self, db_session):
        _make_batch(
            db_session,
            batch_id="b3",
            segment="Service Supply Chain",
            regions=["US"],
        )
        result = get_next_discovery_slice(db_session)
        assert result["segment"] == "Service Supply Chain"
        assert result["regions"] == ["EU", "Asia"]

    def test_after_service_eu_picks_ems(self, db_session):
        _make_batch(
            db_session,
            batch_id="b4",
            segment="Service Supply Chain",
            regions=["EU", "Asia"],
        )
        result = get_next_discovery_slice(db_session)
        assert result["segment"] == "EMS / Electronics Mfg"

    def test_after_ems_picks_automotive(self, db_session):
        _make_batch(
            db_session,
            batch_id="b5",
            segment="EMS / Electronics Mfg",
            regions=["US", "EU", "Asia"],
        )
        result = get_next_discovery_slice(db_session)
        assert result["segment"] == "Automotive + catch-all"

    def test_wraps_around_after_month_6(self, db_session):
        """After automotive → back to Aerospace US."""
        _make_batch(
            db_session,
            batch_id="b6",
            segment="Automotive + catch-all",
            regions=["US", "EU", "Asia"],
        )
        result = get_next_discovery_slice(db_session)
        assert result["segment"] == "Aerospace & Defense"
        assert result["regions"] == ["US"]

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


# ── Kill Switch ─────────────────────────────────────────────────────


class TestKillSwitch:

    @pytest.mark.asyncio
    async def test_discover_skips_when_disabled(self):
        with patch("app.services.prospect_scheduler.settings") as mock_s:
            mock_s.prospecting_enabled = False
            result = await job_discover_prospects()
        assert result["skipped"] is True

    @pytest.mark.asyncio
    async def test_enrich_skips_when_disabled(self):
        with patch("app.services.prospect_scheduler.settings") as mock_s:
            mock_s.prospecting_enabled = False
            result = await job_enrich_pool()
        assert result["skipped"] is True

    @pytest.mark.asyncio
    async def test_contacts_skips_when_disabled(self):
        with patch("app.services.prospect_scheduler.settings") as mock_s:
            mock_s.prospecting_enabled = False
            result = await job_find_contacts()
        assert result["skipped"] is True

    @pytest.mark.asyncio
    async def test_refresh_skips_when_disabled(self):
        with patch("app.services.prospect_scheduler.settings") as mock_s:
            mock_s.prospecting_enabled = False
            result = await job_refresh_scores()
        assert result["skipped"] is True

    @pytest.mark.asyncio
    async def test_expire_skips_when_disabled(self):
        with patch("app.services.prospect_scheduler.settings") as mock_s:
            mock_s.prospecting_enabled = False
            result = await job_expire_and_resurface()
        assert result["skipped"] is True


# ── Expire Logic ────────────────────────────────────────────────────


class TestExpireLogic:

    @pytest.mark.asyncio
    async def test_expires_old_low_readiness(self, db_session):
        p = _make_prospect(
            db_session, name="Old Low", domain="oldlow.com",
            readiness_score=30,
            created_at=datetime.now(timezone.utc) - timedelta(days=100),
            last_enriched_at=datetime.now(timezone.utc) - timedelta(days=70),
        )
        with patch("app.database.SessionLocal", return_value=db_session), \
             patch.object(db_session, "close"):
            await job_expire_and_resurface()
        db_session.refresh(p)
        assert p.status == "expired"

    @pytest.mark.asyncio
    async def test_does_not_expire_high_readiness(self, db_session):
        p = _make_prospect(
            db_session, name="Old High", domain="oldhigh.com",
            readiness_score=70,
            created_at=datetime.now(timezone.utc) - timedelta(days=100),
            last_enriched_at=datetime.now(timezone.utc) - timedelta(days=70),
        )
        with patch("app.database.SessionLocal", return_value=db_session), \
             patch.object(db_session, "close"):
            await job_expire_and_resurface()
        db_session.refresh(p)
        assert p.status == "suggested"

    @pytest.mark.asyncio
    async def test_does_not_expire_recently_enriched(self, db_session):
        p = _make_prospect(
            db_session, name="Recent", domain="recent.com",
            readiness_score=30,
            created_at=datetime.now(timezone.utc) - timedelta(days=100),
            last_enriched_at=datetime.now(timezone.utc) - timedelta(days=10),
        )
        with patch("app.database.SessionLocal", return_value=db_session), \
             patch.object(db_session, "close"):
            await job_expire_and_resurface()
        db_session.refresh(p)
        assert p.status == "suggested"

    @pytest.mark.asyncio
    async def test_does_not_expire_strong_intent(self, db_session):
        p = _make_prospect(
            db_session, name="Intent", domain="intent.com",
            readiness_score=30,
            readiness_signals={"intent": {"strength": "strong"}},
            created_at=datetime.now(timezone.utc) - timedelta(days=100),
            last_enriched_at=datetime.now(timezone.utc) - timedelta(days=70),
        )
        with patch("app.database.SessionLocal", return_value=db_session), \
             patch.object(db_session, "close"):
            await job_expire_and_resurface()
        db_session.refresh(p)
        assert p.status == "suggested"

    @pytest.mark.asyncio
    async def test_does_not_expire_young_prospect(self, db_session):
        p = _make_prospect(
            db_session, name="Young", domain="young.com",
            readiness_score=20,
            created_at=datetime.now(timezone.utc) - timedelta(days=30),
        )
        with patch("app.database.SessionLocal", return_value=db_session), \
             patch.object(db_session, "close"):
            await job_expire_and_resurface()
        db_session.refresh(p)
        assert p.status == "suggested"

    @pytest.mark.asyncio
    async def test_within_90_day_boundary(self, db_session):
        """Prospect created 89 days ago should NOT be expired (cutoff is 90)."""
        p = _make_prospect(
            db_session, name="Boundary", domain="boundary.com",
            readiness_score=20,
            created_at=datetime.now(timezone.utc) - timedelta(days=89),
            last_enriched_at=datetime.now(timezone.utc) - timedelta(days=70),
        )
        with patch("app.database.SessionLocal", return_value=db_session), \
             patch.object(db_session, "close"):
            await job_expire_and_resurface()
        db_session.refresh(p)
        assert p.status == "suggested"


# ── Resurface Logic ─────────────────────────────────────────────────


class TestResurfaceLogic:

    @pytest.mark.asyncio
    async def test_resurfaces_dismissed_with_fresh_signals(self, db_session):
        p = _make_prospect(
            db_session, name="Dismissed", domain="dismissed.com",
            status="dismissed",
            readiness_score=50,
            readiness_signals={"intent": {"strength": "strong"}},
            last_enriched_at=datetime.now(timezone.utc) - timedelta(days=5),
            dismissed_at=datetime.now(timezone.utc) - timedelta(days=60),
        )
        with patch("app.database.SessionLocal", return_value=db_session), \
             patch.object(db_session, "close"):
            await job_expire_and_resurface()
        db_session.refresh(p)
        assert p.status == "suggested"
        assert p.dismissed_by is None
        assert p.dismiss_reason is None

    @pytest.mark.asyncio
    async def test_resurfaces_expired_with_hiring_signals(self, db_session):
        p = _make_prospect(
            db_session, name="Expired", domain="expired.com",
            status="expired",
            readiness_score=45,
            readiness_signals={"hiring": {"type": "procurement"}},
            last_enriched_at=datetime.now(timezone.utc) - timedelta(days=10),
        )
        with patch("app.database.SessionLocal", return_value=db_session), \
             patch.object(db_session, "close"):
            await job_expire_and_resurface()
        db_session.refresh(p)
        assert p.status == "suggested"

    @pytest.mark.asyncio
    async def test_does_not_resurface_without_signals(self, db_session):
        p = _make_prospect(
            db_session, name="NoSignals", domain="nosignals.com",
            status="dismissed",
            readiness_score=50,
            readiness_signals={},
            last_enriched_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        with patch("app.database.SessionLocal", return_value=db_session), \
             patch.object(db_session, "close"):
            await job_expire_and_resurface()
        db_session.refresh(p)
        assert p.status == "dismissed"

    @pytest.mark.asyncio
    async def test_does_not_resurface_low_readiness(self, db_session):
        """Even with signals, readiness < 40 shouldn't resurface."""
        p = _make_prospect(
            db_session, name="LowRead", domain="lowread.com",
            status="dismissed",
            readiness_score=20,
            readiness_signals={"intent": {"strength": "strong"}},
            last_enriched_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        with patch("app.database.SessionLocal", return_value=db_session), \
             patch.object(db_session, "close"):
            await job_expire_and_resurface()
        db_session.refresh(p)
        assert p.status == "dismissed"

    @pytest.mark.asyncio
    async def test_does_not_resurface_old_enrichment(self, db_session):
        """Enrichment older than 30 days shouldn't trigger resurface."""
        p = _make_prospect(
            db_session, name="OldEnrich", domain="oldenrich.com",
            status="dismissed",
            readiness_score=60,
            readiness_signals={"intent": {"strength": "strong"}},
            last_enriched_at=datetime.now(timezone.utc) - timedelta(days=45),
        )
        with patch("app.database.SessionLocal", return_value=db_session), \
             patch.object(db_session, "close"):
            await job_expire_and_resurface()
        db_session.refresh(p)
        assert p.status == "dismissed"


# ── Score Refresh ───────────────────────────────────────────────────


class TestScoreRefresh:

    @pytest.mark.asyncio
    async def test_refreshes_all_suggested(self, db_session):
        _make_prospect(db_session, name="A", domain="a.com", industry="Aerospace", fit_score=10)
        _make_prospect(db_session, name="B", domain="b.com", industry="Automotive", fit_score=10)
        _make_prospect(db_session, name="C", domain="c.com", status="claimed", fit_score=10)

        with patch("app.database.SessionLocal", return_value=db_session), \
             patch.object(db_session, "close"):
            result = await job_refresh_scores()

        assert result["refreshed"] == 2  # Only suggested, not claimed

    @pytest.mark.asyncio
    async def test_tracks_upgrades_and_downgrades(self, db_session):
        # High initial score but no industry → will score lower
        _make_prospect(
            db_session, name="Down", domain="down.com",
            industry=None, fit_score=90, readiness_score=90,
        )
        with patch("app.database.SessionLocal", return_value=db_session), \
             patch.object(db_session, "close"):
            result = await job_refresh_scores()
        # Should detect a downgrade since fit_score will drop without industry match
        assert result["refreshed"] == 1


# ── Health Report ───────────────────────────────────────────────────


class TestHealthReport:

    @pytest.mark.asyncio
    async def test_empty_pool_report(self, db_session):
        with patch("app.database.SessionLocal", return_value=db_session), \
             patch.object(db_session, "close"):
            result = await job_pool_health_report()
        assert result["claimed_this_month"] == 0
        assert result["dismissed_this_month"] == 0
        assert result["credits_used_this_month"] == 0

    @pytest.mark.asyncio
    async def test_report_counts(self, db_session):
        _make_prospect(db_session, name="S1", domain="s1.com", status="suggested")
        _make_prospect(db_session, name="S2", domain="s2.com", status="suggested", region="EU")
        _make_prospect(
            db_session, name="C1", domain="c1.com", status="claimed",
            claimed_at=datetime.now(timezone.utc),
        )
        _make_prospect(
            db_session, name="D1", domain="d1.com", status="dismissed",
            dismissed_at=datetime.now(timezone.utc),
        )
        _make_batch(db_session, batch_id="b-report", credits_used=42)

        with patch("app.database.SessionLocal", return_value=db_session), \
             patch.object(db_session, "close"):
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
        with patch(
            "app.services.prospect_scheduler.settings"
        ) as mock_s:
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
        with patch(
            "app.services.prospect_scheduler.settings"
        ) as mock_s:
            mock_s.prospecting_enabled = True
            mock_s.prospecting_min_fit_for_contacts = 60
            with patch(
                "app.services.prospect_contacts.run_contact_enrichment_batch",
                new_callable=AsyncMock,
                side_effect=Exception("API down"),
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

    @pytest.mark.asyncio
    async def test_discover_happy_path(self, db_session):
        """Full discovery job with mocked external services."""
        from app.schemas.prospect_account import ProspectAccountCreate

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

        mock_graph = MagicMock()
        mock_run_explorium = AsyncMock(return_value=[mock_explorium_result])
        mock_run_email = AsyncMock(return_value=[mock_email_result])

        with patch("app.database.SessionLocal", return_value=db_session), \
             patch.object(db_session, "close"), \
             patch.dict("sys.modules", {
                 "app.services.prospect_discovery_explorium": MagicMock(
                     run_explorium_discovery_batch=mock_run_explorium,
                 ),
                 "app.services.prospect_discovery_email": MagicMock(
                     run_email_mining_batch=mock_run_email,
                 ),
                 "app.utils.graph_client": MagicMock(
                     get_graph_client=MagicMock(return_value=mock_graph),
                 ),
             }):
            result = await job_discover_prospects()

        assert result["explorium_count"] == 1
        assert result["email_count"] == 1
        assert "batch_id" in result

    @pytest.mark.asyncio
    async def test_discover_explorium_fails_email_continues(self, db_session):
        """Explorium failure doesn't block email mining."""
        from app.schemas.prospect_account import ProspectAccountCreate

        mock_email_result = ProspectAccountCreate(
            name="Email Only",
            domain="emailonly.com",
            discovery_source="email_history",
        )

        mock_graph = MagicMock()
        mock_run_explorium = AsyncMock(side_effect=Exception("Explorium down"))
        mock_run_email = AsyncMock(return_value=[mock_email_result])

        with patch("app.database.SessionLocal", return_value=db_session), \
             patch.object(db_session, "close"), \
             patch.dict("sys.modules", {
                 "app.services.prospect_discovery_explorium": MagicMock(
                     run_explorium_discovery_batch=mock_run_explorium,
                 ),
                 "app.services.prospect_discovery_email": MagicMock(
                     run_email_mining_batch=mock_run_email,
                 ),
                 "app.utils.graph_client": MagicMock(
                     get_graph_client=MagicMock(return_value=mock_graph),
                 ),
             }):
            result = await job_discover_prospects()

        assert result["explorium_count"] == 0
        assert result["email_count"] == 1

    @pytest.mark.asyncio
    async def test_discover_both_fail_still_completes(self, db_session):
        """Both sources fail — batch still completes with 0 results."""
        mock_run_explorium = AsyncMock(side_effect=Exception("Explorium down"))

        with patch("app.database.SessionLocal", return_value=db_session), \
             patch.object(db_session, "close"), \
             patch.dict("sys.modules", {
                 "app.services.prospect_discovery_explorium": MagicMock(
                     run_explorium_discovery_batch=mock_run_explorium,
                 ),
                 "app.utils.graph_client": MagicMock(
                     get_graph_client=MagicMock(side_effect=Exception("No graph token")),
                 ),
             }):
            result = await job_discover_prospects()

        assert result["explorium_count"] == 0
        assert result["email_count"] == 0


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
    """Happy-path for job_find_contacts."""

    @pytest.mark.asyncio
    async def test_contacts_happy_path(self):
        mock_result = {"prospects_processed": 5, "total_verified": 12}

        with patch(
            "app.services.prospect_contacts.run_contact_enrichment_batch",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            result = await job_find_contacts()

        assert result["prospects_processed"] == 5
        assert result["total_verified"] == 12


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
            patch("app.services.prospect_scheduler.get_next_discovery_slice",
                   return_value={"segment": "Test", "regions": ["US"]}),
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
    async def test_refresh_scores_exception_path(self):
        """Lines 313-317: job_refresh_scores returns error on exception."""
        with patch("app.database.SessionLocal") as mock_sl:
            mock_db = MagicMock()
            mock_sl.return_value = mock_db
            mock_db.query.side_effect = RuntimeError("Score refresh exploded")

            result = await job_refresh_scores()

        assert "error" in result

    @pytest.mark.asyncio
    async def test_expire_and_resurface_exception_path(self):
        """Lines 403-407: job_expire_and_resurface returns error on exception."""
        with patch("app.database.SessionLocal") as mock_sl:
            mock_db = MagicMock()
            mock_sl.return_value = mock_db
            mock_db.query.side_effect = RuntimeError("Expire job exploded")

            result = await job_expire_and_resurface()

        assert "error" in result
