"""Tests for the retry_stuck_diagnosed job function.

Covers: skipping disabled self-heal, skipping exhausted iterations,
        re-diagnosis for missing detailed, execute_fix retry, batch limit.

Called by: pytest
Depends on: app.models.trouble_ticket, app.jobs.selfheal_jobs
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import User
from app.models.trouble_ticket import TroubleTicket


@pytest.fixture()
def retry_user(db_session: Session) -> User:
    user = User(
        email="retry@trioscs.com",
        name="Retry User",
        role="admin",
        azure_id="test-retry-001",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _make_ticket(db, user, *, ticket_number, risk_tier="low", iterations_used=0, diagnosis=None, hours_ago=1):
    t = TroubleTicket(
        ticket_number=ticket_number,
        submitted_by=user.id,
        title=f"Stuck ticket {ticket_number}",
        description="Stuck in diagnosed",
        status="diagnosed",
        risk_tier=risk_tier,
        iterations_used=iterations_used,
        diagnosis=diagnosis,
        created_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


class TestRetryStuckDiagnosed:
    @pytest.mark.asyncio
    async def test_disabled_self_heal_skips(self, db_session, retry_user):
        """When self_heal_enabled is False, returns zeros."""
        from app.jobs.selfheal_jobs import retry_stuck_diagnosed

        _make_ticket(db_session, retry_user, ticket_number="TT-RSD-001", diagnosis={"detailed": {"root_cause": "test"}})

        with patch("app.jobs.selfheal_jobs.settings") as mock_settings:
            mock_settings.self_heal_enabled = False
            result = await retry_stuck_diagnosed(db_session)

        assert result == {"retried": 0, "rediagnosed": 0, "succeeded": 0, "failed": 0}

    @pytest.mark.asyncio
    async def test_skips_exhausted_iterations(self, db_session, retry_user):
        """Tickets at max iterations are skipped."""
        from app.jobs.selfheal_jobs import retry_stuck_diagnosed

        _make_ticket(
            db_session,
            retry_user,
            ticket_number="TT-RSD-002",
            risk_tier="low",
            iterations_used=5,
            diagnosis={"detailed": {"root_cause": "test"}},
        )

        with patch("app.jobs.selfheal_jobs.settings") as mock_settings:
            mock_settings.self_heal_enabled = True
            mock_settings.self_heal_max_iterations_low = 5
            mock_settings.self_heal_max_iterations_medium = 10
            result = await retry_stuck_diagnosed(db_session)

        assert result["retried"] == 0

    @pytest.mark.asyncio
    async def test_retries_good_ticket(self, db_session, retry_user):
        """Ticket with good diagnosis gets execute_fix retried."""
        from app.jobs.selfheal_jobs import retry_stuck_diagnosed

        _make_ticket(
            db_session,
            retry_user,
            ticket_number="TT-RSD-003",
            risk_tier="low",
            iterations_used=1,
            diagnosis={"detailed": {"root_cause": "bug", "affected_files": ["app/foo.py"]}},
        )

        with (
            patch("app.jobs.selfheal_jobs.settings") as mock_settings,
            patch("app.services.execution_service.execute_fix", new_callable=AsyncMock) as mock_exec,
        ):
            mock_settings.self_heal_enabled = True
            mock_settings.self_heal_max_iterations_low = 5
            mock_settings.self_heal_max_iterations_medium = 10
            mock_exec.return_value = {"ok": True, "status": "fix_queued"}
            result = await retry_stuck_diagnosed(db_session)

        assert result["retried"] == 1
        assert result["succeeded"] == 1
        assert result["rediagnosed"] == 0

    @pytest.mark.asyncio
    async def test_rediagnoses_missing_detailed(self, db_session, retry_user):
        """Ticket with diagnosis but no 'detailed' key gets re-diagnosed first."""
        from app.jobs.selfheal_jobs import retry_stuck_diagnosed

        _make_ticket(
            db_session,
            retry_user,
            ticket_number="TT-RSD-004",
            risk_tier="medium",
            iterations_used=0,
            diagnosis={"classification": {"category": "ui"}},
        )

        with (
            patch("app.jobs.selfheal_jobs.settings") as mock_settings,
            patch("app.services.diagnosis_service.diagnose_full", new_callable=AsyncMock) as mock_diag,
            patch("app.services.execution_service.execute_fix", new_callable=AsyncMock) as mock_exec,
        ):
            mock_settings.self_heal_enabled = True
            mock_settings.self_heal_max_iterations_low = 5
            mock_settings.self_heal_max_iterations_medium = 10
            mock_diag.return_value = {
                "classification": {},
                "diagnosis": {},
                "risk_tier": "medium",
                "status": "diagnosed",
            }
            mock_exec.return_value = {"ok": True, "status": "fix_queued"}
            result = await retry_stuck_diagnosed(db_session)

        assert result["rediagnosed"] == 1
        assert result["retried"] == 1
        mock_diag.assert_called_once()

    @pytest.mark.asyncio
    async def test_rediagnosis_failure_skips_execution(self, db_session, retry_user):
        """If re-diagnosis fails, execution is skipped and counted as failed."""
        from app.jobs.selfheal_jobs import retry_stuck_diagnosed

        _make_ticket(
            db_session, retry_user, ticket_number="TT-RSD-005", risk_tier="low", iterations_used=0, diagnosis={}
        )

        with (
            patch("app.jobs.selfheal_jobs.settings") as mock_settings,
            patch("app.services.diagnosis_service.diagnose_full", new_callable=AsyncMock) as mock_diag,
            patch("app.services.execution_service.execute_fix", new_callable=AsyncMock) as mock_exec,
        ):
            mock_settings.self_heal_enabled = True
            mock_settings.self_heal_max_iterations_low = 5
            mock_settings.self_heal_max_iterations_medium = 10
            mock_diag.return_value = {"error": "AI unavailable"}
            result = await retry_stuck_diagnosed(db_session)

        assert result["failed"] == 1
        assert result["retried"] == 0
        mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_failure_counted(self, db_session, retry_user):
        """Failed execute_fix is counted in failed."""
        from app.jobs.selfheal_jobs import retry_stuck_diagnosed

        _make_ticket(
            db_session,
            retry_user,
            ticket_number="TT-RSD-006",
            risk_tier="low",
            iterations_used=2,
            diagnosis={"detailed": {"root_cause": "bug"}},
        )

        with (
            patch("app.jobs.selfheal_jobs.settings") as mock_settings,
            patch("app.services.execution_service.execute_fix", new_callable=AsyncMock) as mock_exec,
        ):
            mock_settings.self_heal_enabled = True
            mock_settings.self_heal_max_iterations_low = 5
            mock_settings.self_heal_max_iterations_medium = 10
            mock_exec.return_value = {"error": "Patch generation failed"}
            result = await retry_stuck_diagnosed(db_session)

        assert result["retried"] == 1
        assert result["failed"] == 1
        assert result["succeeded"] == 0

    @pytest.mark.asyncio
    async def test_skips_high_risk(self, db_session, retry_user):
        """High risk tickets are not retried."""
        from app.jobs.selfheal_jobs import retry_stuck_diagnosed

        _make_ticket(
            db_session,
            retry_user,
            ticket_number="TT-RSD-007",
            risk_tier="high",
            iterations_used=0,
            diagnosis={"detailed": {"root_cause": "critical"}},
        )

        with patch("app.jobs.selfheal_jobs.settings") as mock_settings:
            mock_settings.self_heal_enabled = True
            result = await retry_stuck_diagnosed(db_session)

        assert result["retried"] == 0

    @pytest.mark.asyncio
    async def test_batch_limit(self, db_session, retry_user):
        """At most MAX_RETRY_BATCH tickets are processed per run."""
        from app.jobs.selfheal_jobs import MAX_RETRY_BATCH, retry_stuck_diagnosed

        for i in range(MAX_RETRY_BATCH + 5):
            _make_ticket(
                db_session,
                retry_user,
                ticket_number=f"TT-RSD-B{i:03d}",
                risk_tier="low",
                iterations_used=0,
                diagnosis={"detailed": {"root_cause": "test"}},
                hours_ago=MAX_RETRY_BATCH + 5 - i,
            )

        with (
            patch("app.jobs.selfheal_jobs.settings") as mock_settings,
            patch("app.services.execution_service.execute_fix", new_callable=AsyncMock) as mock_exec,
        ):
            mock_settings.self_heal_enabled = True
            mock_settings.self_heal_max_iterations_low = 5
            mock_settings.self_heal_max_iterations_medium = 10
            mock_exec.return_value = {"ok": True, "status": "fix_queued"}
            result = await retry_stuck_diagnosed(db_session)

        assert result["retried"] == MAX_RETRY_BATCH


class TestJobRegistration:
    def test_retry_stuck_diagnosed_registered(self):
        """register_selfheal_jobs registers the retry_stuck_diagnosed job."""
        from app.jobs.selfheal_jobs import register_selfheal_jobs

        scheduler = MagicMock()
        mock_settings = MagicMock()
        register_selfheal_jobs(scheduler, mock_settings)

        job_ids = [call.kwargs["id"] for call in scheduler.add_job.call_args_list]
        assert "retry_stuck_diagnosed" in job_ids
