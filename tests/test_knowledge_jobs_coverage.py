"""Tests for app/jobs/knowledge_jobs.py — targeting missing coverage.

Covers register_knowledge_jobs and _job_expire_stale.

Called by: pytest
Depends on: conftest fixtures, knowledge_jobs
"""

import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from loguru import logger
from sqlalchemy.orm import Session

from app.models.knowledge import KnowledgeEntry


@pytest.fixture
def loguru_info():
    """Capture loguru INFO+ messages into a list — loguru isn't bridged to stdlib
    logging here, so pytest's ``caplog`` won't see ``logger.info(...)``."""
    captured: list[str] = []
    sink_id = logger.add(lambda msg: captured.append(str(msg)), level="INFO")
    yield captured
    logger.remove(sink_id)


class TestRegisterKnowledgeJobs:
    def test_registers_at_least_one_job(self):
        from app.jobs.knowledge_jobs import register_knowledge_jobs

        mock_scheduler = MagicMock()
        mock_settings = MagicMock()
        register_knowledge_jobs(mock_scheduler, mock_settings)
        assert mock_scheduler.add_job.call_count >= 1

    def test_registers_expire_job(self):
        from app.jobs.knowledge_jobs import register_knowledge_jobs

        mock_scheduler = MagicMock()
        register_knowledge_jobs(mock_scheduler, MagicMock())
        all_kwargs = [c[1] for c in mock_scheduler.add_job.call_args_list]
        ids = [kw.get("id") for kw in all_kwargs]
        assert "knowledge_expire_stale" in ids


def _seed_knowledge_entry(db: Session, *, expires_at=None) -> KnowledgeEntry:
    entry = KnowledgeEntry(
        entry_type="note",
        content="test entry",
        source="manual",
        expires_at=expires_at,
        created_at=datetime.now(UTC),
    )
    db.add(entry)
    db.flush()
    return entry


class TestJobExpireStale:
    """P6.3: converted from a whole-session MagicMock (``query().filter().count()``
    stubbed to a fixed number, never running the real ``expires_at`` predicate) to real
    ``KnowledgeEntry`` rows on ``db_session`` — the counts asserted via the
    ``loguru_info`` sink fixture are now the REAL result of the query, not whatever the
    mock was told to return."""

    async def test_expire_stale_empty_db(self, db_session: Session, loguru_info):
        """With no KnowledgeEntry rows, both counts log as zero."""
        from app.jobs.knowledge_jobs import _job_expire_stale

        with patch("app.database.SessionLocal", lambda: db_session):
            await _job_expire_stale()

        assert any("0 total, 0 expired" in msg for msg in loguru_info)

    async def test_expire_stale_with_entries(self, db_session: Session, loguru_info):
        """Expire stale logs the REAL total vs.

        expired-only count.
        """
        from app.jobs.knowledge_jobs import _job_expire_stale

        now = datetime.now(UTC)
        _seed_knowledge_entry(db_session, expires_at=now - timedelta(days=1))  # expired
        _seed_knowledge_entry(db_session, expires_at=now - timedelta(hours=1))  # expired
        _seed_knowledge_entry(db_session, expires_at=now + timedelta(days=30))  # not yet
        _seed_knowledge_entry(db_session, expires_at=None)  # never expires
        db_session.commit()

        with patch("app.database.SessionLocal", lambda: db_session):
            await _job_expire_stale()

        assert any("4 total, 2 expired" in msg for msg in loguru_info)

    async def test_expire_stale_db_error_raises(self):
        """If DB query fails, exception is re-raised.

        P6.3 disposition: KEPT as a whole-session MagicMock — this forces the query
        itself to raise, a hard-failure path a real SQLite session can't be coerced into
        cleanly (would need to drop the table mid-test); the assertion is on the re-
        raise behavior, not on any query result the mock could be hiding.
        """
        from app.jobs.knowledge_jobs import _job_expire_stale

        mock_session = MagicMock()
        mock_session.query.side_effect = RuntimeError("DB error")
        mock_session.close = MagicMock()

        with patch("app.database.SessionLocal", return_value=mock_session):
            with pytest.raises(RuntimeError):
                await _job_expire_stale()
