"""test_jobs_coverage.py — Coverage gap tests for job modules at 0%.

Covers missing tests for:
  - health_jobs: register_health_jobs (no-op since W1 — pollers removed)
  - prospecting_jobs: register_prospecting_jobs (W1 no-op guard)
  - task_jobs: register_task_jobs (no-op since W1 — bid_due_alerts deleted)
  - knowledge_jobs: register_knowledge_jobs (no-op since W1)
  - tagging_jobs: register_tagging_jobs (no-op since W1 — tagging runs on-demand
                  via app/management commands)
  - maintenance_jobs: register_maintenance_jobs (no-op since W1)

Called by: pytest
Depends on: conftest.py fixtures, app/jobs/*
"""

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.scheduler import scheduler

# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture()
def scheduler_db(db_session: Session):
    """Patch SessionLocal so scheduler jobs use the test DB."""
    original_close = db_session.close
    db_session.close = lambda: None
    with patch("app.database.SessionLocal", return_value=db_session):
        yield db_session
    db_session.close = original_close


@pytest.fixture(autouse=True)
def _clear_scheduler_jobs():
    """Remove all jobs before/after each test to prevent leakage."""
    for job in scheduler.get_jobs():
        job.remove()
    yield
    for job in scheduler.get_jobs():
        job.remove()


# ═══════════════════════════════════════════════════════════════════════
# health_jobs — register_health_jobs
# ═══════════════════════════════════════════════════════════════════════


class TestRegisterHealthJobs:
    def test_registers_no_jobs(self):
        """register_health_jobs registers NOTHING (W1 simplification, 2026-08-04).

        health_ping / health_deep / cleanup_usage_log / reset_monthly_usage were removed
        per docs/W1_JOB_DISPOSITION.md; the health-check fan-out stays code-complete in
        app/services/health_monitor.py.
        """
        from app.jobs.health_jobs import register_health_jobs

        mock_scheduler = MagicMock()
        register_health_jobs(mock_scheduler, MagicMock())

        mock_scheduler.add_job.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# prospecting_jobs — register_prospecting_jobs
# ═══════════════════════════════════════════════════════════════════════


class TestRegisterProspectingJobs:
    def test_registers_nothing_since_w1_delete(self):
        """register_prospecting_jobs is a no-op — the 6 Explorium monthly jobs were
        deleted in W1 (docs/W1_JOB_DISPOSITION.md) and must never re-appear."""
        from app.jobs.prospecting_jobs import register_prospecting_jobs

        mock_scheduler = MagicMock()
        mock_settings = MagicMock(prospecting_enabled=True)
        register_prospecting_jobs(mock_scheduler, mock_settings)

        mock_scheduler.add_job.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# task_jobs — register_task_jobs (no-op since W1)
# ═══════════════════════════════════════════════════════════════════════


class TestRegisterTaskJobs:
    def test_registers_no_jobs(self):
        """register_task_jobs registers NOTHING (W1 simplification, 2026-08-04).

        bid_due_alerts (daily bid-due tasks; 2 ever created) was deleted per
        docs/W1_JOB_DISPOSITION.md — inline task hooks are unaffected.
        """
        from app.jobs.task_jobs import register_task_jobs

        mock_scheduler = MagicMock()
        mock_settings = MagicMock()
        register_task_jobs(mock_scheduler, mock_settings)

        mock_scheduler.add_job.assert_not_called()


class TestRegisterKnowledgeJobs:
    def test_registers_no_jobs(self):
        """register_knowledge_jobs registers NOTHING (W1 simplification, 2026-08-04).

        knowledge_expire_stale (log-only count of expired entries) was removed per
        docs/W1_JOB_DISPOSITION.md; expiry is applied at query time, not by a job.
        """
        from app.jobs.knowledge_jobs import register_knowledge_jobs

        mock_scheduler = MagicMock()
        register_knowledge_jobs(mock_scheduler, MagicMock())

        mock_scheduler.add_job.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# tagging_jobs — register (no-op since W1 simplification)
# ═══════════════════════════════════════════════════════════════════════


class TestRegisterTaggingJobs:
    def test_registers_no_jobs(self):
        """register_tagging_jobs registers NOTHING (W1 simplification, 2026-08-04).

        The five scheduled tagging jobs were removed per docs/W1_JOB_DISPOSITION.md:
        internal_confidence_boost + sighting_mining deleted (zero yield);
        prefix_backfill / ai_tagging / spec_enrichment run on-demand via app/management
        commands only.
        """
        from app.jobs.tagging_jobs import register_tagging_jobs

        mock_scheduler = MagicMock()
        register_tagging_jobs(mock_scheduler, MagicMock())

        mock_scheduler.add_job.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# maintenance_jobs — register (no-op since W1 simplification)
# ═══════════════════════════════════════════════════════════════════════


class TestRegisterMaintenanceJobs:
    def test_registers_no_jobs(self):
        """register_maintenance_jobs registers NOTHING (W1 simplification, 2026-08-04).

        The six maintenance jobs (cache_cleanup, auto_attribute_activities, auto_dedup,
        reset_connector_errors, integrity_check, contact_dedup) were removed per
        docs/W1_JOB_DISPOSITION.md; the manual merge path and the on-demand /api/admin
        integrity route remain.
        """
        from app.jobs.maintenance_jobs import register_maintenance_jobs

        mock_scheduler = MagicMock()
        register_maintenance_jobs(mock_scheduler, MagicMock())

        mock_scheduler.add_job.assert_not_called()
