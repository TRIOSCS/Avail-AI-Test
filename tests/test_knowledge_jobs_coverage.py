"""Tests for app/jobs/knowledge_jobs.py — targeting missing coverage.

Covers register_knowledge_jobs, _job_refresh_insights, _job_expire_stale.

Called by: pytest
Depends on: conftest fixtures, knowledge_jobs
"""

import os

os.environ["TESTING"] = "1"

from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from loguru import logger
from sqlalchemy.orm import Session

from app.models import User
from app.models.knowledge import KnowledgeEntry
from app.models.offers import Offer
from app.models.sourcing import Requisition
from app.models.vendors import VendorCard


@pytest.fixture
def loguru_info():
    """Capture loguru INFO+ messages into a list — loguru isn't bridged to stdlib
    logging here, so pytest's ``caplog`` won't see ``logger.info(...)``."""
    captured: list[str] = []
    sink_id = logger.add(lambda msg: captured.append(str(msg)), level="INFO")
    yield captured
    logger.remove(sink_id)


# The five knowledge_service insight generators _job_refresh_insights calls.
_INSIGHT_GENERATORS = (
    "generate_insights",
    "generate_pipeline_insights",
    "generate_vendor_insights",
    "generate_company_insights",
    "generate_mpn_insights",
)


def _patch_insight_generators(stack: ExitStack, **overrides):
    """Patch every knowledge_service insight generator on the given ExitStack.

    Each generator defaults to an AsyncMock returning []. Pass a generator name as a
    keyword (e.g. generate_insights=mock) to override one with a specific mock.
    """
    for name in _INSIGHT_GENERATORS:
        mock = overrides.get(name, AsyncMock(return_value=[]))
        stack.enter_context(patch(f"app.services.knowledge_service.{name}", new=mock))


@pytest.fixture()
def active_req(db_session: Session, test_user: User) -> Requisition:
    req = Requisition(
        name="KJ-TEST-REQ",
        customer_name="Test Co",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)
    return req


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


def _seed_requisition(db: Session, user: User, name: str, *, updated_at=None) -> Requisition:
    req = Requisition(
        name=name,
        customer_name="Test Co",
        status="open",
        created_by=user.id,
        created_at=datetime.now(UTC),
        updated_at=updated_at or datetime.now(UTC),
    )
    db.add(req)
    db.flush()
    return req


def _seed_offer_with_vendor(db: Session, vendor_card_id: int, *, created_at=None) -> Offer:
    vendor = db.get(VendorCard, vendor_card_id)
    if vendor is None:
        vendor = VendorCard(
            id=vendor_card_id, normalized_name=f"vendor{vendor_card_id}", display_name=f"V{vendor_card_id}"
        )
        db.add(vendor)
        db.flush()
    offer = Offer(
        vendor_card_id=vendor_card_id,
        vendor_name=vendor.display_name,
        mpn="LM317T",
        created_at=created_at or datetime.now(UTC),
    )
    db.add(offer)
    db.flush()
    return offer


class TestJobRefreshInsights:
    """P6.3: the req/vendor-id-fetching tests below seed real Requisition/Offer rows on
    ``db_session`` instead of a rotating call-count mock that handed back canned
    ``(id,)`` tuples regardless of the real ``updated_at``/``created_at`` filter, join,
    or group-by — so the actual SQL (not a stand-in) is what's exercised."""

    async def test_refresh_insights_empty_db(self, db_session: Session):
        """With empty DB (no rows), job runs without errors."""
        from app.jobs.knowledge_jobs import _job_refresh_insights

        with ExitStack() as stack:
            stack.enter_context(patch("app.database.SessionLocal", lambda: db_session))
            _patch_insight_generators(stack)
            await _job_refresh_insights()

    async def test_refresh_insights_with_req_ids(self, db_session: Session, test_user: User):
        """generate_insights is called once per recently-active requisition, with the
        REAL req id from a real row (not a mock-supplied id)."""
        from app.jobs.knowledge_jobs import _job_refresh_insights

        reqs = [_seed_requisition(db_session, test_user, f"KJ-REQ-{i}") for i in range(3)]
        db_session.commit()
        expected_ids = {r.id for r in reqs}  # captured before the job closes the session
        seen_ids = []

        async def _capture(db, req_id):
            seen_ids.append(req_id)
            return [MagicMock()]

        mock_generate = AsyncMock(side_effect=_capture)

        with ExitStack() as stack:
            stack.enter_context(patch("app.database.SessionLocal", lambda: db_session))
            _patch_insight_generators(stack, generate_insights=mock_generate)
            await _job_refresh_insights()

        assert mock_generate.call_count == 3
        assert set(seen_ids) == expected_ids

    async def test_refresh_insights_req_exception_continues(self, db_session: Session, test_user: User):
        """If generate_insights raises for a req, job continues."""
        from app.jobs.knowledge_jobs import _job_refresh_insights

        _seed_requisition(db_session, test_user, "KJ-REQ-CRASH")
        db_session.commit()
        mock_generate = AsyncMock(side_effect=Exception("AI failed"))

        with ExitStack() as stack:
            stack.enter_context(patch("app.database.SessionLocal", lambda: db_session))
            _patch_insight_generators(stack, generate_insights=mock_generate)
            await _job_refresh_insights()  # Should not raise

        mock_generate.assert_awaited_once()

    async def test_refresh_pipeline_exception_continues(self, db_session: Session):
        """If pipeline insights fail, rest of job continues."""
        from app.jobs.knowledge_jobs import _job_refresh_insights

        with ExitStack() as stack:
            stack.enter_context(patch("app.database.SessionLocal", lambda: db_session))
            _patch_insight_generators(
                stack,
                generate_pipeline_insights=AsyncMock(side_effect=Exception("pipeline fail")),
            )
            await _job_refresh_insights()  # Should not raise

    async def test_refresh_insights_db_error_logs_and_continues(self):
        """DB errors within each section are caught and logged; job completes.

        P6.3 disposition: KEPT as a whole-session MagicMock — simulates every section's
        own query itself raising, which (like ``test_expire_stale_db_error_raises``)
        can't be forced cleanly against a real SQLite session; the assertion is on the
        catch-and-continue control flow, not on any hidden query result.
        """
        from app.jobs.knowledge_jobs import _job_refresh_insights

        mock_session = MagicMock()
        mock_session.query.side_effect = RuntimeError("Section DB failure")
        mock_session.close = MagicMock()
        mock_session.rollback = MagicMock()

        with patch("app.database.SessionLocal", return_value=mock_session):
            # Each section catches its own error, so the job should complete without raising
            await _job_refresh_insights()

        mock_session.close.assert_called_once()

    async def test_refresh_vendor_insights_called(self, db_session: Session):
        """generate_vendor_insights is called once per vendor with recent offers, with
        the REAL vendor_card_id from a real Offer row."""
        from app.jobs.knowledge_jobs import _job_refresh_insights

        _seed_offer_with_vendor(db_session, 501)
        _seed_offer_with_vendor(db_session, 502)
        db_session.commit()
        seen_ids = []

        async def _capture(db, vendor_card_id):
            seen_ids.append(vendor_card_id)
            return [MagicMock()]

        mock_vendor = AsyncMock(side_effect=_capture)

        with ExitStack() as stack:
            stack.enter_context(patch("app.database.SessionLocal", lambda: db_session))
            _patch_insight_generators(stack, generate_vendor_insights=mock_vendor)
            await _job_refresh_insights()

        assert mock_vendor.call_count == 2
        assert set(seen_ids) == {501, 502}
