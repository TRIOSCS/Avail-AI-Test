"""test_jobs_knowledge.py — Tests for knowledge ledger background jobs.

Covers: _job_refresh_insights, _job_deliver_question_batches,
_job_send_knowledge_digests, _job_expire_stale.

All jobs use SessionLocal() internally, so we patch app.database.SessionLocal
to return a mock or test DB session with close() disabled.
"""

import asyncio
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


# ── _job_deliver_question_batches() ──────────────────────────────────


def test_deliver_question_batches_no_op():
    """_job_deliver_question_batches is a no-op (Teams removed) — must not raise."""
    from app.jobs.knowledge_jobs import _job_deliver_question_batches

    asyncio.run(_job_deliver_question_batches())


# ── _job_send_knowledge_digests() ────────────────────────────────────


def test_send_knowledge_digests_no_op():
    """_job_send_knowledge_digests is a no-op (Teams removed) — must not raise."""
    from app.jobs.knowledge_jobs import _job_send_knowledge_digests

    asyncio.run(_job_send_knowledge_digests())


# ── _job_expire_stale() ─────────────────────────────────────────────


def test_expire_stale_counts_entries(scheduler_db):
    """_job_expire_stale queries total and expired counts without error."""
    from app.jobs.knowledge_jobs import _job_expire_stale

    asyncio.run(_job_expire_stale())


def test_expire_stale_handles_error():
    """_job_expire_stale re-raises on DB error and closes session."""
    mock_db = MagicMock()
    mock_db.query.side_effect = Exception("DB error")

    with patch("app.database.SessionLocal", return_value=mock_db):
        from app.jobs.knowledge_jobs import _job_expire_stale

        with pytest.raises(Exception, match="DB error"):
            asyncio.run(_job_expire_stale())

    mock_db.close.assert_called_once()


# ── _job_refresh_insights() ─────────────────────────────────────────


def test_refresh_insights_happy_path():
    """_job_refresh_insights processes active reqs, vendors, companies, MPNs,
    pipeline."""
    mock_db = MagicMock()
    # Requisition query returns 2 active reqs
    req_query = MagicMock()
    req_query.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
        (1,),
        (2,),
    ]

    # Vendor query returns 1 vendor
    vendor_query = MagicMock()
    vendor_query.filter.return_value.group_by.return_value.order_by.return_value.limit.return_value.all.return_value = [
        (10,),
    ]

    # Company query returns 1 company
    company_query = MagicMock()
    company_query.join.return_value.filter.return_value.group_by.return_value.order_by.return_value.limit.return_value.all.return_value = [
        (20,),
    ]

    # MPN query returns 1 MPN
    mpn_query = MagicMock()
    mpn_query.filter.return_value.group_by.return_value.order_by.return_value.limit.return_value.all.return_value = [
        ("LM317T",),
    ]

    # Route db.query() calls to the right mock based on the model
    call_count = {"n": 0}

    def route_query(model):
        call_count["n"] += 1
        n = call_count["n"]
        if n == 1:
            return req_query
        elif n == 2:
            return vendor_query
        elif n == 3:
            return company_query
        elif n == 4:
            return mpn_query
        return MagicMock()

    mock_db.query.side_effect = route_query

    mock_generate = AsyncMock(return_value=[{"id": 1}])
    mock_pipeline = AsyncMock(return_value=[{"id": 2}])
    mock_vendor = AsyncMock(return_value=[{"id": 3}])
    mock_company = AsyncMock(return_value=[{"id": 4}])
    mock_mpn = AsyncMock(return_value=[{"id": 5}])

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.services.knowledge_service.generate_insights", mock_generate),
        patch("app.services.knowledge_service.generate_pipeline_insights", mock_pipeline),
        patch("app.services.knowledge_service.generate_vendor_insights", mock_vendor),
        patch("app.services.knowledge_service.generate_company_insights", mock_company),
        patch("app.services.knowledge_service.generate_mpn_insights", mock_mpn),
    ):
        from app.jobs.knowledge_jobs import _job_refresh_insights

        asyncio.run(_job_refresh_insights())

    mock_generate.assert_called()
    mock_pipeline.assert_awaited_once()
    mock_vendor.assert_called()
    mock_company.assert_called()
    mock_mpn.assert_called()
    mock_db.close.assert_called_once()


def test_refresh_insights_inner_section_errors_logged_not_raised():
    """Per-section exceptions are caught and logged — the job completes without
    raising."""
    mock_db = MagicMock()
    # All db.query calls fail, but each section catches its own exception
    mock_db.query.side_effect = Exception("DB failure")

    with patch("app.database.SessionLocal", return_value=mock_db):
        from app.jobs.knowledge_jobs import _job_refresh_insights

        # Should NOT raise — each section catches its own error
        asyncio.run(_job_refresh_insights())

    mock_db.close.assert_called_once()


def test_refresh_insights_session_local_failure_rollback():
    """If SessionLocal() itself raises, the outer handler catches it."""
    with patch("app.database.SessionLocal", side_effect=Exception("Pool exhausted")):
        from app.jobs.knowledge_jobs import _job_refresh_insights

        with pytest.raises(Exception, match="Pool exhausted"):
            asyncio.run(_job_refresh_insights())


def test_refresh_insights_individual_req_failure_continues():
    """If one requisition's insight generation fails, processing continues."""
    mock_db = MagicMock()
    # Requisition query returns 2 reqs
    req_query = MagicMock()
    req_query.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
        (1,),
        (2,),
    ]

    # All other queries return empty lists
    empty_query = MagicMock()
    empty_query.filter.return_value.group_by.return_value.order_by.return_value.limit.return_value.all.return_value = []
    empty_query.join.return_value.filter.return_value.group_by.return_value.order_by.return_value.limit.return_value.all.return_value = []

    call_count = {"n": 0}

    def route_query(model):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return req_query
        return empty_query

    mock_db.query.side_effect = route_query

    # First req fails, second succeeds
    mock_generate = AsyncMock(side_effect=[Exception("AI timeout"), [{"id": 1}]])
    mock_pipeline = AsyncMock(return_value=[])

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.services.knowledge_service.generate_insights", mock_generate),
        patch("app.services.knowledge_service.generate_pipeline_insights", mock_pipeline),
        patch("app.services.knowledge_service.generate_vendor_insights", AsyncMock(return_value=[])),
        patch("app.services.knowledge_service.generate_company_insights", AsyncMock(return_value=[])),
        patch("app.services.knowledge_service.generate_mpn_insights", AsyncMock(return_value=[])),
    ):
        from app.jobs.knowledge_jobs import _job_refresh_insights

        asyncio.run(_job_refresh_insights())

    # Both calls attempted (first failed, second succeeded)
    assert mock_generate.call_count == 2
    mock_db.close.assert_called_once()
