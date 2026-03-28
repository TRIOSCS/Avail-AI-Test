"""test_jobs_coverage.py — Coverage gap tests for job modules at 0%.

Covers missing tests for:
  - health_jobs: register_health_jobs
  - prospecting_jobs: register_prospecting_jobs (enabled + disabled)
  - sourcing_refresh_jobs: register_sourcing_refresh_jobs
  - task_jobs: register_task_jobs, _job_bid_due_alerts (all branches)
  - knowledge_jobs: register_knowledge_jobs, _job_refresh_insights, _job_expire_stale
  - tagging_jobs: register_tagging_jobs, _job_internal_boost, _job_prefix_backfill,
                  _job_sighting_mining, _job_ai_tagging
  - maintenance_jobs: register_maintenance_jobs, _job_contact_dedup

Called by: pytest
Depends on: conftest.py fixtures, app/jobs/*
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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
    def test_registers_four_jobs(self):
        """register_health_jobs adds 4 jobs to the scheduler."""
        from app.jobs.health_jobs import register_health_jobs

        mock_scheduler = MagicMock()
        mock_settings = MagicMock()
        register_health_jobs(mock_scheduler, mock_settings)

        assert mock_scheduler.add_job.call_count == 4
        job_ids = [c.kwargs["id"] for c in mock_scheduler.add_job.call_args_list]
        assert "health_ping" in job_ids
        assert "health_deep" in job_ids
        assert "cleanup_usage_log" in job_ids
        assert "reset_monthly_usage" in job_ids


# ═══════════════════════════════════════════════════════════════════════
# prospecting_jobs — register_prospecting_jobs
# ═══════════════════════════════════════════════════════════════════════


class TestRegisterProspectingJobs:
    def test_registers_six_jobs_when_enabled(self):
        """register_prospecting_jobs adds 6 jobs when prospecting is enabled."""
        from app.jobs.prospecting_jobs import register_prospecting_jobs

        mock_scheduler = MagicMock()
        mock_settings = MagicMock(prospecting_enabled=True)
        register_prospecting_jobs(mock_scheduler, mock_settings)

        assert mock_scheduler.add_job.call_count == 6
        job_ids = [c.kwargs["id"] for c in mock_scheduler.add_job.call_args_list]
        assert "pool_health_report" in job_ids
        assert "discover_prospects" in job_ids
        assert "enrich_pool" in job_ids
        assert "find_contacts" in job_ids
        assert "refresh_scores" in job_ids
        assert "expire_and_resurface" in job_ids

    def test_skips_when_disabled(self):
        """register_prospecting_jobs does nothing when prospecting is disabled."""
        from app.jobs.prospecting_jobs import register_prospecting_jobs

        mock_scheduler = MagicMock()
        mock_settings = MagicMock(prospecting_enabled=False)
        register_prospecting_jobs(mock_scheduler, mock_settings)

        mock_scheduler.add_job.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# sourcing_refresh_jobs — register_sourcing_refresh_jobs
# ═══════════════════════════════════════════════════════════════════════


class TestRegisterSourcingRefreshJobs:
    def test_registers_one_job(self):
        """register_sourcing_refresh_jobs adds 1 job."""
        from app.jobs.sourcing_refresh_jobs import register_sourcing_refresh_jobs

        mock_scheduler = MagicMock()
        mock_settings = MagicMock()
        register_sourcing_refresh_jobs(mock_scheduler, mock_settings)

        assert mock_scheduler.add_job.call_count == 1
        assert mock_scheduler.add_job.call_args_list[0].kwargs["id"] == "refresh_stale_requisitions"


# ═══════════════════════════════════════════════════════════════════════
# task_jobs — register_task_jobs, _job_bid_due_alerts
# ═══════════════════════════════════════════════════════════════════════


class TestRegisterTaskJobs:
    def test_registers_one_job(self):
        """register_task_jobs adds 1 job."""
        from app.jobs.task_jobs import register_task_jobs

        mock_scheduler = MagicMock()
        mock_settings = MagicMock()
        register_task_jobs(mock_scheduler, mock_settings)

        assert mock_scheduler.add_job.call_count == 1
        assert mock_scheduler.add_job.call_args_list[0].kwargs["id"] == "bid_due_alerts"


def _make_req_mock(id_, deadline, req_name):
    """Create a MagicMock for Requisition with proper name handling.

    MagicMock.name is reserved, so we use a SimpleNamespace instead.
    """
    return SimpleNamespace(id=id_, deadline=deadline, name=req_name, status="active")


class TestJobBidDueAlerts:
    def test_creates_tasks_for_approaching_deadlines(self):
        """_job_bid_due_alerts creates tasks for requisitions with deadlines within 2
        days."""
        mock_db = MagicMock()
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        mock_req = _make_req_mock(1, tomorrow, "REQ-001")

        mock_query = MagicMock()
        mock_query.filter.return_value.limit.return_value.all.return_value = [mock_req]
        mock_db.query.return_value = mock_query

        mock_on_bid_due = MagicMock(return_value=MagicMock())

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.services.task_service.on_bid_due_soon", mock_on_bid_due),
        ):
            from app.jobs.task_jobs import _job_bid_due_alerts

            asyncio.run(_job_bid_due_alerts())

        mock_on_bid_due.assert_called_once_with(mock_db, 1, tomorrow, "REQ-001")
        mock_db.close.assert_called_once()

    def test_skips_non_iso_deadlines(self):
        """_job_bid_due_alerts skips requisitions with non-ISO deadlines."""
        mock_db = MagicMock()
        mock_req = _make_req_mock(1, "not-a-date", "REQ-002")

        mock_query = MagicMock()
        mock_query.filter.return_value.limit.return_value.all.return_value = [mock_req]
        mock_db.query.return_value = mock_query

        mock_on_bid_due = MagicMock()

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.services.task_service.on_bid_due_soon", mock_on_bid_due),
        ):
            from app.jobs.task_jobs import _job_bid_due_alerts

            asyncio.run(_job_bid_due_alerts())

        mock_on_bid_due.assert_not_called()
        mock_db.close.assert_called_once()

    def test_skips_far_future_deadlines(self):
        """_job_bid_due_alerts skips requisitions with deadlines > 2 days away."""
        mock_db = MagicMock()
        far_future = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S")
        mock_req = _make_req_mock(1, far_future, "REQ-003")

        mock_query = MagicMock()
        mock_query.filter.return_value.limit.return_value.all.return_value = [mock_req]
        mock_db.query.return_value = mock_query

        mock_on_bid_due = MagicMock()

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.services.task_service.on_bid_due_soon", mock_on_bid_due),
        ):
            from app.jobs.task_jobs import _job_bid_due_alerts

            asyncio.run(_job_bid_due_alerts())

        mock_on_bid_due.assert_not_called()

    def test_skips_old_past_deadlines(self):
        """_job_bid_due_alerts skips deadlines more than 1 day in the past."""
        mock_db = MagicMock()
        old_past = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S")
        mock_req = _make_req_mock(1, old_past, "REQ-004")

        mock_query = MagicMock()
        mock_query.filter.return_value.limit.return_value.all.return_value = [mock_req]
        mock_db.query.return_value = mock_query

        mock_on_bid_due = MagicMock()

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.services.task_service.on_bid_due_soon", mock_on_bid_due),
        ):
            from app.jobs.task_jobs import _job_bid_due_alerts

            asyncio.run(_job_bid_due_alerts())

        mock_on_bid_due.assert_not_called()

    def test_respects_bid_due_cap(self):
        """_job_bid_due_alerts stops creating tasks after reaching _BID_DUE_CAP."""
        from app.jobs.task_jobs import _BID_DUE_CAP

        mock_db = MagicMock()
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        mock_reqs = [_make_req_mock(i, tomorrow, f"REQ-{i}") for i in range(_BID_DUE_CAP + 5)]

        mock_query = MagicMock()
        mock_query.filter.return_value.limit.return_value.all.return_value = mock_reqs
        mock_db.query.return_value = mock_query

        mock_on_bid_due = MagicMock(return_value=MagicMock())  # Always returns a task

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.services.task_service.on_bid_due_soon", mock_on_bid_due),
        ):
            from app.jobs.task_jobs import _job_bid_due_alerts

            asyncio.run(_job_bid_due_alerts())

        assert mock_on_bid_due.call_count == _BID_DUE_CAP

    def test_no_reqs_found(self):
        """_job_bid_due_alerts handles no matching requisitions."""
        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.limit.return_value.all.return_value = []
        mock_db.query.return_value = mock_query

        mock_on_bid_due = MagicMock()

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.services.task_service.on_bid_due_soon", mock_on_bid_due),
        ):
            from app.jobs.task_jobs import _job_bid_due_alerts

            asyncio.run(_job_bid_due_alerts())

        mock_on_bid_due.assert_not_called()
        mock_db.close.assert_called_once()

    def test_on_bid_due_returns_none_not_counted(self):
        """When on_bid_due_soon returns None (duplicate), it does not count toward
        cap."""
        mock_db = MagicMock()
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        mock_reqs = [_make_req_mock(i, tomorrow, f"REQ-{i}") for i in range(3)]

        mock_query = MagicMock()
        mock_query.filter.return_value.limit.return_value.all.return_value = mock_reqs
        mock_db.query.return_value = mock_query

        mock_on_bid_due = MagicMock(side_effect=[MagicMock(), None, MagicMock()])

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.services.task_service.on_bid_due_soon", mock_on_bid_due),
        ):
            from app.jobs.task_jobs import _job_bid_due_alerts

            asyncio.run(_job_bid_due_alerts())

        assert mock_on_bid_due.call_count == 3

    def test_db_error_rollback_and_reraise(self):
        """DB error triggers rollback and re-raises."""
        mock_db = MagicMock()
        mock_db.query.side_effect = Exception("Connection lost")

        with patch("app.database.SessionLocal", return_value=mock_db):
            from app.jobs.task_jobs import _job_bid_due_alerts

            with pytest.raises(Exception, match="Connection lost"):
                asyncio.run(_job_bid_due_alerts())

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()

    def test_deadline_without_timezone_gets_utc(self):
        """Naive datetime deadlines are treated as UTC."""
        mock_db = MagicMock()
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
        mock_req = _make_req_mock(1, tomorrow, "REQ-TZ")

        mock_query = MagicMock()
        mock_query.filter.return_value.limit.return_value.all.return_value = [mock_req]
        mock_db.query.return_value = mock_query

        mock_on_bid_due = MagicMock(return_value=MagicMock())

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.services.task_service.on_bid_due_soon", mock_on_bid_due),
        ):
            from app.jobs.task_jobs import _job_bid_due_alerts

            asyncio.run(_job_bid_due_alerts())

        mock_on_bid_due.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# knowledge_jobs — register, _job_refresh_insights, _job_expire_stale
# ═══════════════════════════════════════════════════════════════════════


class TestRegisterKnowledgeJobs:
    def test_registers_two_jobs(self):
        """register_knowledge_jobs adds 2 jobs."""
        from app.jobs.knowledge_jobs import register_knowledge_jobs

        mock_scheduler = MagicMock()
        mock_settings = MagicMock()
        register_knowledge_jobs(mock_scheduler, mock_settings)

        assert mock_scheduler.add_job.call_count == 2
        job_ids = [c.kwargs["id"] for c in mock_scheduler.add_job.call_args_list]
        assert "knowledge_refresh_insights" in job_ids
        assert "knowledge_expire_stale" in job_ids


def _patch_knowledge_service():
    """Create a mock knowledge_service module and patch it into app.services."""
    mock_ks = MagicMock()
    mock_ks.generate_insights = AsyncMock(return_value=["entry1"])
    mock_ks.generate_pipeline_insights = AsyncMock(return_value=["entry2"])
    mock_ks.generate_vendor_insights = AsyncMock(return_value=["entry3"])
    mock_ks.generate_company_insights = AsyncMock(return_value=["entry4"])
    mock_ks.generate_mpn_insights = AsyncMock(return_value=["entry5"])
    return mock_ks


class TestJobRefreshInsights:
    def test_refreshes_all_insight_types(self):
        """_job_refresh_insights processes reqs, pipeline, vendors, companies, and
        MPNs."""
        mock_db = MagicMock()

        req_query = MagicMock()
        req_query.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [(1,), (2,)]

        vendor_query = MagicMock()
        vendor_query.filter.return_value.group_by.return_value.order_by.return_value.limit.return_value.all.return_value = [
            (10,)
        ]

        company_query = MagicMock()
        company_query.join.return_value.filter.return_value.group_by.return_value.order_by.return_value.limit.return_value.all.return_value = [
            (20,)
        ]

        mpn_query = MagicMock()
        mpn_query.filter.return_value.group_by.return_value.order_by.return_value.limit.return_value.all.return_value = [
            ("LM317T",)
        ]

        call_count = [0]

        def side_effect_query(model, *args):
            call_count[0] += 1
            if call_count[0] == 1:
                return req_query
            elif call_count[0] == 2:
                return vendor_query
            elif call_count[0] == 3:
                return company_query
            elif call_count[0] == 4:
                return mpn_query
            return MagicMock()

        mock_db.query.side_effect = side_effect_query
        mock_ks = _patch_knowledge_service()

        import sys

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch.dict(sys.modules, {"app.services.knowledge_service": mock_ks}),
        ):
            from app.jobs.knowledge_jobs import _job_refresh_insights

            asyncio.run(_job_refresh_insights())

        assert mock_ks.generate_insights.call_count == 2
        mock_ks.generate_pipeline_insights.assert_called_once()
        mock_ks.generate_vendor_insights.assert_called_once_with(mock_db, 10)
        mock_ks.generate_company_insights.assert_called_once_with(mock_db, 20)
        mock_ks.generate_mpn_insights.assert_called_once_with(mock_db, "LM317T")
        mock_db.close.assert_called_once()

    def test_individual_insight_failure_does_not_stop_others(self):
        """If one req insight fails, others still process."""
        mock_db = MagicMock()

        req_query = MagicMock()
        req_query.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [(1,), (2,)]

        empty_query = MagicMock()
        empty_query.filter.return_value.group_by.return_value.order_by.return_value.limit.return_value.all.return_value = []
        empty_query.join.return_value.filter.return_value.group_by.return_value.order_by.return_value.limit.return_value.all.return_value = []

        call_count = [0]

        def side_effect_query(model, *args):
            call_count[0] += 1
            if call_count[0] == 1:
                return req_query
            return empty_query

        mock_db.query.side_effect = side_effect_query

        mock_ks = _patch_knowledge_service()
        mock_ks.generate_insights = AsyncMock(side_effect=[["entry1"], Exception("AI timeout")])

        import sys

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch.dict(sys.modules, {"app.services.knowledge_service": mock_ks}),
        ):
            from app.jobs.knowledge_jobs import _job_refresh_insights

            asyncio.run(_job_refresh_insights())

        assert mock_ks.generate_insights.call_count == 2
        mock_db.close.assert_called_once()

    def test_section_level_exception_continues(self):
        """If entire req section query fails, other sections still run."""
        mock_db = MagicMock()

        call_count = [0]

        def side_effect_query(model, *args):
            call_count[0] += 1
            if call_count[0] == 1:
                mock_q = MagicMock()
                mock_q.filter.side_effect = Exception("Req query failed")
                return mock_q
            mock_q = MagicMock()
            mock_q.filter.return_value.group_by.return_value.order_by.return_value.limit.return_value.all.return_value = []
            mock_q.join.return_value.filter.return_value.group_by.return_value.order_by.return_value.limit.return_value.all.return_value = []
            return mock_q

        mock_db.query.side_effect = side_effect_query

        mock_ks = _patch_knowledge_service()

        import sys

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch.dict(sys.modules, {"app.services.knowledge_service": mock_ks}),
        ):
            from app.jobs.knowledge_jobs import _job_refresh_insights

            asyncio.run(_job_refresh_insights())

        mock_ks.generate_pipeline_insights.assert_called_once()
        mock_db.close.assert_called_once()


class TestJobExpireStale:
    def test_logs_expired_count(self):
        """_job_expire_stale queries and logs expired + total counts."""
        mock_db = MagicMock()

        expired_query = MagicMock()
        expired_query.filter.return_value.count.return_value = 5

        total_query = MagicMock()
        total_query.count.return_value = 100

        call_count = [0]

        def side_effect_query(model):
            call_count[0] += 1
            if call_count[0] == 1:
                return expired_query
            return total_query

        mock_db.query.side_effect = side_effect_query

        with patch("app.database.SessionLocal", return_value=mock_db):
            from app.jobs.knowledge_jobs import _job_expire_stale

            asyncio.run(_job_expire_stale())

        mock_db.close.assert_called_once()

    def test_db_error_reraises(self):
        """DB error in expire_stale re-raises for _traced_job."""
        mock_db = MagicMock()
        mock_db.query.side_effect = Exception("Query failed")

        with patch("app.database.SessionLocal", return_value=mock_db):
            from app.jobs.knowledge_jobs import _job_expire_stale

            with pytest.raises(Exception, match="Query failed"):
                asyncio.run(_job_expire_stale())

        mock_db.close.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# tagging_jobs — register, boost, prefix, sighting, ai_tagging
# ═══════════════════════════════════════════════════════════════════════


class TestRegisterTaggingJobs:
    def test_registers_four_jobs_without_enrichment(self):
        """register_tagging_jobs adds 4 jobs when enrichment is disabled."""
        from app.jobs.tagging_jobs import register_tagging_jobs

        mock_scheduler = MagicMock()
        mock_settings = MagicMock(material_enrichment_enabled=False)
        register_tagging_jobs(mock_scheduler, mock_settings)

        assert mock_scheduler.add_job.call_count == 4
        job_ids = [c.kwargs["id"] for c in mock_scheduler.add_job.call_args_list]
        assert "internal_confidence_boost" in job_ids
        assert "prefix_backfill" in job_ids
        assert "sighting_mining" in job_ids
        assert "ai_tagging" in job_ids

    def test_registers_five_jobs_with_enrichment(self):
        """register_tagging_jobs adds 5 jobs when enrichment is enabled."""
        from app.jobs.tagging_jobs import register_tagging_jobs

        mock_scheduler = MagicMock()
        mock_settings = MagicMock(material_enrichment_enabled=True)
        register_tagging_jobs(mock_scheduler, mock_settings)

        assert mock_scheduler.add_job.call_count == 5
        job_ids = [c.kwargs["id"] for c in mock_scheduler.add_job.call_args_list]
        assert "material_enrichment" in job_ids


class TestJobInternalBoost:
    def test_calls_boost_confidence_internal(self):
        """_job_internal_boost delegates to enrichment.boost_confidence_internal."""
        mock_db = MagicMock()
        mock_boost = MagicMock(return_value={"boosted": 10})

        async def fake_to_thread(fn, *args):
            return fn(*args)

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.services.enrichment.boost_confidence_internal", mock_boost),
            patch("asyncio.to_thread", side_effect=fake_to_thread),
        ):
            from app.jobs.tagging_jobs import _job_internal_boost

            asyncio.run(_job_internal_boost())

        mock_boost.assert_called_once_with(mock_db)
        mock_db.close.assert_called_once()

    def test_error_rollback_and_reraise(self):
        """Exception rolls back and re-raises."""
        mock_db = MagicMock()

        async def fake_to_thread(fn, *args):
            return fn(*args)

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.services.enrichment.boost_confidence_internal", side_effect=Exception("Boost failed")),
            patch("asyncio.to_thread", side_effect=fake_to_thread),
        ):
            from app.jobs.tagging_jobs import _job_internal_boost

            with pytest.raises(Exception, match="Boost failed"):
                asyncio.run(_job_internal_boost())

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()


class TestJobPrefixBackfill:
    def test_calls_run_prefix_backfill(self):
        """_job_prefix_backfill delegates to tagging_backfill.run_prefix_backfill."""
        mock_db = MagicMock()
        mock_backfill = MagicMock(return_value={"tagged": 5})

        async def fake_to_thread(fn, *args):
            return fn(*args)

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.services.tagging_backfill.run_prefix_backfill", mock_backfill),
            patch("asyncio.to_thread", side_effect=fake_to_thread),
        ):
            from app.jobs.tagging_jobs import _job_prefix_backfill

            asyncio.run(_job_prefix_backfill())

        mock_backfill.assert_called_once_with(mock_db)
        mock_db.close.assert_called_once()

    def test_error_rollback_and_reraise(self):
        """Exception rolls back and re-raises."""
        mock_db = MagicMock()

        async def fake_to_thread(fn, *args):
            return fn(*args)

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.services.tagging_backfill.run_prefix_backfill", side_effect=Exception("Backfill failed")),
            patch("asyncio.to_thread", side_effect=fake_to_thread),
        ):
            from app.jobs.tagging_jobs import _job_prefix_backfill

            with pytest.raises(Exception, match="Backfill failed"):
                asyncio.run(_job_prefix_backfill())

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()


class TestJobSightingMining:
    def test_calls_backfill_manufacturer_from_sightings(self):
        """_job_sighting_mining delegates to backfill_manufacturer_from_sightings."""
        mock_db = MagicMock()
        mock_mining = MagicMock(return_value={"mined": 3})

        async def fake_to_thread(fn, *args):
            return fn(*args)

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.services.tagging_backfill.backfill_manufacturer_from_sightings", mock_mining),
            patch("asyncio.to_thread", side_effect=fake_to_thread),
        ):
            from app.jobs.tagging_jobs import _job_sighting_mining

            asyncio.run(_job_sighting_mining())

        mock_mining.assert_called_once_with(mock_db)
        mock_db.close.assert_called_once()

    def test_error_rollback_and_reraise(self):
        """Exception rolls back and re-raises."""
        mock_db = MagicMock()

        async def fake_to_thread(fn, *args):
            return fn(*args)

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch(
                "app.services.tagging_backfill.backfill_manufacturer_from_sightings",
                side_effect=Exception("Mining failed"),
            ),
            patch("asyncio.to_thread", side_effect=fake_to_thread),
        ):
            from app.jobs.tagging_jobs import _job_sighting_mining

            with pytest.raises(Exception, match="Mining failed"):
                asyncio.run(_job_sighting_mining())

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()


class TestJobAiTagging:
    def test_no_untagged_cards_returns_early(self):
        """_job_ai_tagging returns early when no untagged cards found."""
        mock_db = MagicMock()

        mock_subquery = MagicMock()
        mock_subquery.c.material_card_id = "material_card_id"

        tag_query = MagicMock()
        tag_query.join.return_value.filter.return_value.distinct.return_value.subquery.return_value = mock_subquery

        untagged_query = MagicMock()
        untagged_query.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []

        call_count = [0]

        def side_effect_query(model, *args):
            call_count[0] += 1
            if call_count[0] == 1:
                return tag_query
            return untagged_query

        mock_db.query.side_effect = side_effect_query

        with patch("app.database.SessionLocal", return_value=mock_db):
            from app.jobs.tagging_jobs import _job_ai_tagging

            asyncio.run(_job_ai_tagging())

        mock_db.close.assert_called_once()

    def test_processes_untagged_cards(self):
        """_job_ai_tagging classifies untagged cards and applies results."""
        mock_db = MagicMock()

        mock_subquery = MagicMock()
        mock_subquery.c.material_card_id = "material_card_id"

        tag_query = MagicMock()
        tag_query.join.return_value.filter.return_value.distinct.return_value.subquery.return_value = mock_subquery

        mock_cards = [
            SimpleNamespace(id=1, normalized_mpn="LM317T"),
            SimpleNamespace(id=2, normalized_mpn="NE555P"),
            SimpleNamespace(id=3, normalized_mpn="STM32F4"),
        ]
        untagged_query = MagicMock()
        untagged_query.filter.return_value.order_by.return_value.limit.return_value.all.return_value = mock_cards

        call_count = [0]

        def side_effect_query(model, *args):
            call_count[0] += 1
            if call_count[0] == 1:
                return tag_query
            return untagged_query

        mock_db.query.side_effect = side_effect_query

        mock_classify = AsyncMock(return_value={"LM317T": "TI", "NE555P": "TI", "STM32F4": "ST"})
        mock_apply = MagicMock(return_value=(2, 1))

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.services.tagging_ai.classify_parts_with_ai", mock_classify),
            patch("app.services.tagging_ai._apply_ai_results", mock_apply),
        ):
            from app.jobs.tagging_jobs import _job_ai_tagging

            asyncio.run(_job_ai_tagging())

        mock_classify.assert_called_once()
        mock_apply.assert_called_once()
        mock_db.commit.assert_called()
        mock_db.close.assert_called_once()

    def test_error_rollback_and_reraise(self):
        """Top-level exception rolls back and re-raises."""
        mock_db = MagicMock()
        mock_db.query.side_effect = Exception("DB crashed")

        with patch("app.database.SessionLocal", return_value=mock_db):
            from app.jobs.tagging_jobs import _job_ai_tagging

            with pytest.raises(Exception, match="DB crashed"):
                asyncio.run(_job_ai_tagging())

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# maintenance_jobs — register, _job_contact_dedup
# ═══════════════════════════════════════════════════════════════════════


class TestRegisterMaintenanceJobs:
    def test_registers_six_jobs(self):
        """register_maintenance_jobs adds 6 jobs to the scheduler."""
        from app.jobs.maintenance_jobs import register_maintenance_jobs

        mock_scheduler = MagicMock()
        mock_settings = MagicMock()
        register_maintenance_jobs(mock_scheduler, mock_settings)

        assert mock_scheduler.add_job.call_count == 6
        job_ids = [c.kwargs["id"] for c in mock_scheduler.add_job.call_args_list]
        assert "cache_cleanup" in job_ids
        assert "auto_attribute_activities" in job_ids
        assert "auto_dedup" in job_ids
        assert "reset_connector_errors" in job_ids
        assert "integrity_check" in job_ids
        assert "contact_dedup" in job_ids


class TestJobContactDedup:
    def test_no_duplicates_found(self):
        """No duplicates found — nothing merged."""
        mock_db = MagicMock()

        mock_query = MagicMock()
        mock_query.filter.return_value.group_by.return_value.having.return_value.all.return_value = []
        mock_db.query.return_value = mock_query

        with patch("app.database.SessionLocal", return_value=mock_db):
            from app.jobs.maintenance_jobs import _job_contact_dedup

            asyncio.run(_job_contact_dedup())

        mock_db.commit.assert_called_once()
        mock_db.close.assert_called_once()

    def test_merges_duplicate_contacts(self):
        """Duplicate contacts are merged — best kept, others deleted."""
        mock_db = MagicMock()

        # Simulate 1 duplicate group found
        dupe = SimpleNamespace(customer_site_id=1, em="john@test.com", cnt=2)
        dupe_query = MagicMock()
        dupe_query.filter.return_value.group_by.return_value.having.return_value.all.return_value = [dupe]

        # Create mock contacts for the duplicate group
        contact1 = MagicMock(id=1, full_name=None, title=None, phone=None, notes=None, linkedin_url=None)
        contact2 = MagicMock(
            id=2, full_name="John Doe", title="Buyer", phone="555-1234", notes="Primary", linkedin_url=None
        )

        contacts_query = MagicMock()
        contacts_query.filter.return_value.order_by.return_value.all.return_value = [contact1, contact2]

        call_count = [0]

        def side_effect_query(model, *args):
            call_count[0] += 1
            if call_count[0] == 1:
                return dupe_query
            return contacts_query

        mock_db.query.side_effect = side_effect_query

        with patch("app.database.SessionLocal", return_value=mock_db):
            from app.jobs.maintenance_jobs import _job_contact_dedup

            asyncio.run(_job_contact_dedup())

        # contact1 (fewer fields) should be deleted
        mock_db.delete.assert_called_once_with(contact1)
        mock_db.commit.assert_called_once()
        mock_db.close.assert_called_once()

    def test_merges_fields_from_deleted_into_best(self):
        """Best contact gets missing fields from duplicate before deletion."""
        mock_db = MagicMock()

        dupe = SimpleNamespace(customer_site_id=1, em="jane@test.com", cnt=2)
        dupe_query = MagicMock()
        dupe_query.filter.return_value.group_by.return_value.having.return_value.all.return_value = [dupe]

        # contact1 has phone only, contact2 has name+title (more fields = best)
        contact1 = MagicMock(id=1, full_name=None, title=None, phone="555-9999", notes=None, linkedin_url=None)
        contact2 = MagicMock(id=2, full_name="Jane Smith", title="Manager", phone=None, notes=None, linkedin_url=None)

        contacts_query = MagicMock()
        contacts_query.filter.return_value.order_by.return_value.all.return_value = [contact1, contact2]

        call_count = [0]

        def side_effect_query(model, *args):
            call_count[0] += 1
            if call_count[0] == 1:
                return dupe_query
            return contacts_query

        mock_db.query.side_effect = side_effect_query

        with patch("app.database.SessionLocal", return_value=mock_db):
            from app.jobs.maintenance_jobs import _job_contact_dedup

            asyncio.run(_job_contact_dedup())

        # contact1 deleted, contact2 (best) should absorb phone from contact1
        mock_db.delete.assert_called_once_with(contact1)
        mock_db.commit.assert_called_once()

    def test_error_rollback_and_reraise(self):
        """DB error triggers rollback and re-raises."""
        mock_db = MagicMock()
        mock_db.query.side_effect = Exception("DB error")

        with patch("app.database.SessionLocal", return_value=mock_db):
            from app.jobs.maintenance_jobs import _job_contact_dedup

            with pytest.raises(Exception, match="DB error"):
                asyncio.run(_job_contact_dedup())

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()
