"""Tests for app/services/ics_worker/worker.py — ICS search worker main loop.

Called by: pytest
Depends on: conftest.py fixtures (db_session)
"""

import signal
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models.ics_worker_status import IcsWorkerStatus
from app.services.ics_worker.worker import (
    EASTERN,
    _handle_shutdown,
    update_worker_status,
)

# ── Helpers ──────────────────────────────────────────────────────


def _seed_worker_status(db: Session) -> IcsWorkerStatus:
    status = IcsWorkerStatus(
        id=1,
        is_running=False,
        searches_today=0,
        sightings_today=0,
    )
    db.add(status)
    db.commit()
    return status


# ── Tests: update_worker_status ─────────────────────────────────


class TestUpdateWorkerStatus:
    def test_update_existing_status(self, db_session):
        _seed_worker_status(db_session)
        update_worker_status(db_session, is_running=True, searches_today=5)
        status = db_session.query(IcsWorkerStatus).filter_by(id=1).first()
        assert status.is_running is True
        assert status.searches_today == 5

    def test_update_multiple_fields(self, db_session):
        _seed_worker_status(db_session)
        now = datetime.now(timezone.utc)
        update_worker_status(
            db_session,
            is_running=True,
            last_heartbeat=now,
            sightings_today=42,
        )
        status = db_session.query(IcsWorkerStatus).filter_by(id=1).first()
        assert status.is_running is True
        assert status.sightings_today == 42

    def test_update_nonexistent_row_noop(self, db_session):
        """If no status row (id=1) exists, does nothing."""
        update_worker_status(db_session, is_running=True)
        # Should not raise

    def test_ignores_unknown_fields(self, db_session):
        _seed_worker_status(db_session)
        update_worker_status(db_session, nonexistent_field="value")
        # Should not raise, unknown field silently ignored

    def test_sets_updated_at(self, db_session):
        ws = _seed_worker_status(db_session)
        old_updated = ws.updated_at
        update_worker_status(db_session, searches_today=1)
        db_session.refresh(ws)
        assert ws.updated_at is not None


# ── Tests: _handle_shutdown ──────────────────────────────────────


class TestHandleShutdown:
    def test_sets_shutdown_flag(self):
        import app.services.ics_worker.worker as worker_mod

        original = worker_mod._shutdown_requested
        try:
            worker_mod._shutdown_requested = False
            _handle_shutdown(signal.SIGTERM, None)
            assert worker_mod._shutdown_requested is True
        finally:
            worker_mod._shutdown_requested = original

    def test_handles_sigint(self):
        import app.services.ics_worker.worker as worker_mod

        original = worker_mod._shutdown_requested
        try:
            worker_mod._shutdown_requested = False
            _handle_shutdown(signal.SIGINT, None)
            assert worker_mod._shutdown_requested is True
        finally:
            worker_mod._shutdown_requested = original


# ── Tests: main() ──────────────────────────────────────────────


class TestMainLoop:
    @pytest.mark.asyncio
    async def test_main_browser_start_failure(self, db_session):
        """If browser session fails to start, worker exits gracefully."""
        _seed_worker_status(db_session)

        mock_session = MagicMock()
        mock_session.start = AsyncMock(side_effect=Exception("Browser failed"))
        mock_session.stop = AsyncMock()

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch("app.services.ics_worker.queue_manager.recover_stale_searches"),
            patch("app.services.ics_worker.session_manager.IcsSessionManager", return_value=mock_session),
            patch("app.services.ics_worker.config.IcsConfig"),
            patch("app.services.ics_worker.scheduler.SearchScheduler"),
            patch("app.services.ics_worker.circuit_breaker.CircuitBreaker"),
            patch("app.services.ics_worker.ai_gate.process_ai_gate"),
            patch("app.services.ics_worker.queue_manager.get_next_queued_item"),
        ):
            from app.services.ics_worker.worker import main

            await main()

        status = db_session.query(IcsWorkerStatus).filter_by(id=1).first()
        assert status.is_running is False

    @pytest.mark.asyncio
    async def test_main_login_failure(self, db_session):
        """If login fails, worker exits gracefully."""
        _seed_worker_status(db_session)

        mock_session = MagicMock()
        mock_session.start = AsyncMock()
        mock_session.stop = AsyncMock()
        mock_session.is_logged_in = False
        mock_session.login = AsyncMock(return_value=False)

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch("app.services.ics_worker.queue_manager.recover_stale_searches"),
            patch("app.services.ics_worker.session_manager.IcsSessionManager", return_value=mock_session),
            patch("app.services.ics_worker.config.IcsConfig"),
            patch("app.services.ics_worker.scheduler.SearchScheduler"),
            patch("app.services.ics_worker.circuit_breaker.CircuitBreaker"),
            patch("app.services.ics_worker.ai_gate.process_ai_gate"),
            patch("app.services.ics_worker.queue_manager.get_next_queued_item"),
        ):
            from app.services.ics_worker.worker import main

            await main()

        mock_session.stop.assert_awaited_once()
        status = db_session.query(IcsWorkerStatus).filter_by(id=1).first()
        assert status.is_running is False

    @pytest.mark.asyncio
    async def test_main_shutdown_requested(self, db_session):
        """Shutdown flag causes immediate exit from main loop."""
        _seed_worker_status(db_session)
        import app.services.ics_worker.worker as worker_mod

        mock_session = MagicMock()
        mock_session.start = AsyncMock()
        mock_session.stop = AsyncMock()
        mock_session.is_logged_in = True

        original = worker_mod._shutdown_requested
        try:
            worker_mod._shutdown_requested = True

            with (
                patch("app.database.SessionLocal", return_value=db_session),
                patch("app.services.ics_worker.queue_manager.recover_stale_searches"),
                patch("app.services.ics_worker.session_manager.IcsSessionManager", return_value=mock_session),
                patch("app.services.ics_worker.config.IcsConfig"),
                patch("app.services.ics_worker.scheduler.SearchScheduler"),
                patch("app.services.ics_worker.circuit_breaker.CircuitBreaker"),
                patch("app.services.ics_worker.ai_gate.process_ai_gate"),
                patch("app.services.ics_worker.queue_manager.get_next_queued_item"),
            ):
                from app.services.ics_worker.worker import main

                await main()

            mock_session.stop.assert_awaited_once()
        finally:
            worker_mod._shutdown_requested = original

    @pytest.mark.asyncio
    async def test_main_outside_business_hours(self, db_session):
        """Worker sleeps when outside business hours and exits on shutdown."""
        _seed_worker_status(db_session)
        import app.services.ics_worker.worker as worker_mod

        mock_session = MagicMock()
        mock_session.start = AsyncMock()
        mock_session.stop = AsyncMock()
        mock_session.is_logged_in = True

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = False
        mock_scheduler.next_delay.return_value = 0

        call_count = 0

        async def mock_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                worker_mod._shutdown_requested = True

        original = worker_mod._shutdown_requested
        try:
            worker_mod._shutdown_requested = False

            with (
                patch("app.database.SessionLocal", return_value=db_session),
                patch("app.services.ics_worker.queue_manager.recover_stale_searches"),
                patch("app.services.ics_worker.session_manager.IcsSessionManager", return_value=mock_session),
                patch("app.services.ics_worker.config.IcsConfig"),
                patch("app.services.ics_worker.scheduler.SearchScheduler", return_value=mock_scheduler),
                patch("app.services.ics_worker.circuit_breaker.CircuitBreaker"),
                patch("app.services.ics_worker.ai_gate.process_ai_gate"),
                patch("app.services.ics_worker.queue_manager.get_next_queued_item"),
                patch("asyncio.sleep", side_effect=mock_sleep),
            ):
                from app.services.ics_worker.worker import main

                await main()

            assert call_count >= 1
        finally:
            worker_mod._shutdown_requested = original

    @pytest.mark.asyncio
    async def test_main_daily_limit_reached(self, db_session):
        """Worker sleeps when daily limit is reached."""
        _seed_worker_status(db_session)
        import app.services.ics_worker.worker as worker_mod

        mock_session = MagicMock()
        mock_session.start = AsyncMock()
        mock_session.stop = AsyncMock()
        mock_session.is_logged_in = True

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = True
        mock_scheduler.next_delay.return_value = 0

        mock_config = MagicMock()
        mock_config.ICS_MAX_DAILY_SEARCHES = 0  # Already exceeded

        call_count = 0

        async def mock_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                worker_mod._shutdown_requested = True

        original = worker_mod._shutdown_requested
        try:
            worker_mod._shutdown_requested = False

            with (
                patch("app.database.SessionLocal", return_value=db_session),
                patch("app.services.ics_worker.queue_manager.recover_stale_searches"),
                patch("app.services.ics_worker.session_manager.IcsSessionManager", return_value=mock_session),
                patch("app.services.ics_worker.config.IcsConfig", return_value=mock_config),
                patch("app.services.ics_worker.scheduler.SearchScheduler", return_value=mock_scheduler),
                patch("app.services.ics_worker.circuit_breaker.CircuitBreaker"),
                patch("app.services.ics_worker.ai_gate.process_ai_gate"),
                patch("app.services.ics_worker.queue_manager.get_next_queued_item"),
                patch("asyncio.sleep", side_effect=mock_sleep),
            ):
                from app.services.ics_worker.worker import main

                await main()

        finally:
            worker_mod._shutdown_requested = original

    @pytest.mark.asyncio
    async def test_main_circuit_breaker_open(self, db_session):
        """Worker sleeps when circuit breaker is open."""
        _seed_worker_status(db_session)
        import app.services.ics_worker.worker as worker_mod

        mock_session = MagicMock()
        mock_session.start = AsyncMock()
        mock_session.stop = AsyncMock()
        mock_session.is_logged_in = True

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = True
        mock_scheduler.next_delay.return_value = 0

        mock_config = MagicMock()
        mock_config.ICS_MAX_DAILY_SEARCHES = 1000

        mock_breaker = MagicMock()
        mock_breaker.should_stop.return_value = True
        mock_breaker.get_trip_info.return_value = {"trip_reason": "Too many errors"}

        call_count = 0

        async def mock_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                worker_mod._shutdown_requested = True

        original = worker_mod._shutdown_requested
        try:
            worker_mod._shutdown_requested = False

            with (
                patch("app.database.SessionLocal", return_value=db_session),
                patch("app.services.ics_worker.queue_manager.recover_stale_searches"),
                patch("app.services.ics_worker.session_manager.IcsSessionManager", return_value=mock_session),
                patch("app.services.ics_worker.config.IcsConfig", return_value=mock_config),
                patch("app.services.ics_worker.scheduler.SearchScheduler", return_value=mock_scheduler),
                patch("app.services.ics_worker.circuit_breaker.CircuitBreaker", return_value=mock_breaker),
                patch("app.services.ics_worker.ai_gate.process_ai_gate"),
                patch("app.services.ics_worker.queue_manager.get_next_queued_item"),
                patch("asyncio.sleep", side_effect=mock_sleep),
            ):
                from app.services.ics_worker.worker import main

                await main()

        finally:
            worker_mod._shutdown_requested = original

    @pytest.mark.asyncio
    async def test_main_empty_queue(self, db_session):
        """Worker sleeps when queue is empty."""
        _seed_worker_status(db_session)
        import app.services.ics_worker.worker as worker_mod

        mock_session = MagicMock()
        mock_session.start = AsyncMock()
        mock_session.stop = AsyncMock()
        mock_session.is_logged_in = True

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = True
        mock_scheduler.time_for_break.return_value = False
        mock_scheduler.next_delay.return_value = 0

        mock_config = MagicMock()
        mock_config.ICS_MAX_DAILY_SEARCHES = 1000

        mock_breaker = MagicMock()
        mock_breaker.should_stop.return_value = False

        call_count = 0

        async def mock_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                worker_mod._shutdown_requested = True

        original = worker_mod._shutdown_requested
        try:
            worker_mod._shutdown_requested = False

            with (
                patch("app.database.SessionLocal", return_value=db_session),
                patch("app.services.ics_worker.queue_manager.recover_stale_searches"),
                patch("app.services.ics_worker.session_manager.IcsSessionManager", return_value=mock_session),
                patch("app.services.ics_worker.config.IcsConfig", return_value=mock_config),
                patch("app.services.ics_worker.scheduler.SearchScheduler", return_value=mock_scheduler),
                patch("app.services.ics_worker.circuit_breaker.CircuitBreaker", return_value=mock_breaker),
                patch("app.services.ics_worker.ai_gate.process_ai_gate", new_callable=AsyncMock),
                patch("app.services.ics_worker.queue_manager.get_next_queued_item", return_value=None),
                patch("asyncio.sleep", side_effect=mock_sleep),
            ):
                from app.services.ics_worker.worker import main

                await main()

        finally:
            worker_mod._shutdown_requested = original

    @pytest.mark.asyncio
    async def test_main_successful_search(self, db_session):
        """Worker performs a complete search cycle."""
        _seed_worker_status(db_session)
        import app.services.ics_worker.worker as worker_mod

        mock_session = MagicMock()
        mock_session.start = AsyncMock()
        mock_session.stop = AsyncMock()
        mock_session.is_logged_in = True
        mock_session.ensure_session = AsyncMock(return_value=True)
        mock_session.page = MagicMock()

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = True
        mock_scheduler.time_for_break.return_value = False
        mock_scheduler.next_delay.return_value = 0

        mock_config = MagicMock()
        mock_config.ICS_MAX_DAILY_SEARCHES = 1000

        mock_breaker = MagicMock()
        mock_breaker.should_stop.return_value = False
        mock_breaker.check_page_health = AsyncMock(return_value="HEALTHY")

        mock_item = MagicMock()
        mock_item.id = 1
        mock_item.mpn = "LM317T"

        search_result = {"html": "<html>results</html>", "duration_ms": 500}
        parsed_sightings = [{"vendor": "Arrow", "mpn": "LM317T", "qty": 100}]

        call_count = 0
        item_returned = False

        def get_next(db):
            nonlocal item_returned
            if not item_returned:
                item_returned = True
                return mock_item
            return None

        async def mock_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                worker_mod._shutdown_requested = True

        original = worker_mod._shutdown_requested
        try:
            worker_mod._shutdown_requested = False

            with (
                patch("app.database.SessionLocal", return_value=db_session),
                patch("app.services.ics_worker.queue_manager.recover_stale_searches"),
                patch("app.services.ics_worker.session_manager.IcsSessionManager", return_value=mock_session),
                patch("app.services.ics_worker.config.IcsConfig", return_value=mock_config),
                patch("app.services.ics_worker.scheduler.SearchScheduler", return_value=mock_scheduler),
                patch("app.services.ics_worker.circuit_breaker.CircuitBreaker", return_value=mock_breaker),
                patch("app.services.ics_worker.ai_gate.process_ai_gate", new_callable=AsyncMock),
                patch("app.services.ics_worker.queue_manager.get_next_queued_item", side_effect=get_next),
                patch("app.services.ics_worker.queue_manager.mark_status"),
                patch("app.services.ics_worker.queue_manager.mark_completed") as mock_complete,
                patch(
                    "app.services.ics_worker.search_engine.search_part",
                    new_callable=AsyncMock,
                    return_value=search_result,
                    create=True,
                ),
                patch("app.services.ics_worker.result_parser.parse_results_html", return_value=parsed_sightings),
                patch("app.services.ics_worker.sighting_writer.save_ics_sightings", return_value=1),
                patch("asyncio.sleep", side_effect=mock_sleep),
            ):
                from app.services.ics_worker.worker import main

                await main()

            mock_complete.assert_called_once()
        finally:
            worker_mod._shutdown_requested = original

    @pytest.mark.asyncio
    async def test_main_session_reauth_failure(self, db_session):
        """When session re-auth fails, item is marked failed and continues."""
        _seed_worker_status(db_session)
        import app.services.ics_worker.worker as worker_mod

        mock_session = MagicMock()
        mock_session.start = AsyncMock()
        mock_session.stop = AsyncMock()
        mock_session.is_logged_in = True
        mock_session.ensure_session = AsyncMock(return_value=False)

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = True
        mock_scheduler.time_for_break.return_value = False
        mock_scheduler.next_delay.return_value = 0

        mock_config = MagicMock()
        mock_config.ICS_MAX_DAILY_SEARCHES = 1000

        mock_breaker = MagicMock()
        mock_breaker.should_stop.return_value = False

        mock_item = MagicMock()
        mock_item.id = 1
        mock_item.mpn = "TEST"

        call_count = 0
        item_returned = False

        def get_next(db):
            nonlocal item_returned
            if not item_returned:
                item_returned = True
                return mock_item
            return None

        async def mock_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                worker_mod._shutdown_requested = True

        original = worker_mod._shutdown_requested
        try:
            worker_mod._shutdown_requested = False

            with (
                patch("app.database.SessionLocal", return_value=db_session),
                patch("app.services.ics_worker.queue_manager.recover_stale_searches"),
                patch("app.services.ics_worker.session_manager.IcsSessionManager", return_value=mock_session),
                patch("app.services.ics_worker.config.IcsConfig", return_value=mock_config),
                patch("app.services.ics_worker.scheduler.SearchScheduler", return_value=mock_scheduler),
                patch("app.services.ics_worker.circuit_breaker.CircuitBreaker", return_value=mock_breaker),
                patch("app.services.ics_worker.ai_gate.process_ai_gate", new_callable=AsyncMock),
                patch("app.services.ics_worker.queue_manager.get_next_queued_item", side_effect=get_next),
                patch("app.services.ics_worker.queue_manager.mark_status") as mock_mark,
                patch("asyncio.sleep", side_effect=mock_sleep),
            ):
                from app.services.ics_worker.worker import main

                await main()

            # mark_status should have been called with "failed"
            mock_mark.assert_any_call(db_session, mock_item, "failed", error="Session authentication failed")
        finally:
            worker_mod._shutdown_requested = original

    @pytest.mark.asyncio
    async def test_main_break_time(self, db_session):
        """Worker takes a break when scheduler says so."""
        _seed_worker_status(db_session)
        import app.services.ics_worker.worker as worker_mod

        mock_session = MagicMock()
        mock_session.start = AsyncMock()
        mock_session.stop = AsyncMock()
        mock_session.is_logged_in = True

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = True
        mock_scheduler.time_for_break.return_value = True
        mock_scheduler.get_break_duration.return_value = 300
        mock_scheduler.next_delay.return_value = 0

        mock_config = MagicMock()
        mock_config.ICS_MAX_DAILY_SEARCHES = 1000

        mock_breaker = MagicMock()
        mock_breaker.should_stop.return_value = False

        call_count = 0

        async def mock_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                worker_mod._shutdown_requested = True

        original = worker_mod._shutdown_requested
        try:
            worker_mod._shutdown_requested = False

            with (
                patch("app.database.SessionLocal", return_value=db_session),
                patch("app.services.ics_worker.queue_manager.recover_stale_searches"),
                patch("app.services.ics_worker.session_manager.IcsSessionManager", return_value=mock_session),
                patch("app.services.ics_worker.config.IcsConfig", return_value=mock_config),
                patch("app.services.ics_worker.scheduler.SearchScheduler", return_value=mock_scheduler),
                patch("app.services.ics_worker.circuit_breaker.CircuitBreaker", return_value=mock_breaker),
                patch("app.services.ics_worker.ai_gate.process_ai_gate", new_callable=AsyncMock),
                patch("app.services.ics_worker.queue_manager.get_next_queued_item"),
                patch("asyncio.sleep", side_effect=mock_sleep),
            ):
                from app.services.ics_worker.worker import main

                await main()

            mock_scheduler.reset_break_counter.assert_called_once()
        finally:
            worker_mod._shutdown_requested = original

    @pytest.mark.asyncio
    async def test_main_session_expired_during_search(self, db_session):
        """When page health returns SESSION_EXPIRED, item is requeued."""
        _seed_worker_status(db_session)
        import app.services.ics_worker.worker as worker_mod

        mock_session = MagicMock()
        mock_session.start = AsyncMock()
        mock_session.stop = AsyncMock()
        mock_session.is_logged_in = True
        mock_session.ensure_session = AsyncMock(return_value=True)
        mock_session.page = MagicMock()

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = True
        mock_scheduler.time_for_break.return_value = False
        mock_scheduler.next_delay.return_value = 0

        mock_config = MagicMock()
        mock_config.ICS_MAX_DAILY_SEARCHES = 1000

        mock_breaker = MagicMock()
        mock_breaker.should_stop.return_value = False
        mock_breaker.check_page_health = AsyncMock(return_value="SESSION_EXPIRED")

        mock_item = MagicMock()
        mock_item.id = 1
        mock_item.mpn = "TEST-EXP"

        item_returned = False
        call_count = 0

        def get_next(db):
            nonlocal item_returned
            if not item_returned:
                item_returned = True
                return mock_item
            return None

        async def mock_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                worker_mod._shutdown_requested = True

        original = worker_mod._shutdown_requested
        try:
            worker_mod._shutdown_requested = False

            with (
                patch("app.database.SessionLocal", return_value=db_session),
                patch("app.services.ics_worker.queue_manager.recover_stale_searches"),
                patch("app.services.ics_worker.session_manager.IcsSessionManager", return_value=mock_session),
                patch("app.services.ics_worker.config.IcsConfig", return_value=mock_config),
                patch("app.services.ics_worker.scheduler.SearchScheduler", return_value=mock_scheduler),
                patch("app.services.ics_worker.circuit_breaker.CircuitBreaker", return_value=mock_breaker),
                patch("app.services.ics_worker.ai_gate.process_ai_gate", new_callable=AsyncMock),
                patch("app.services.ics_worker.queue_manager.get_next_queued_item", side_effect=get_next),
                patch("app.services.ics_worker.queue_manager.mark_status") as mock_mark,
                patch(
                    "app.services.ics_worker.search_engine.search_part",
                    new_callable=AsyncMock,
                    return_value={"html": "", "duration_ms": 100},
                    create=True,
                ),
                patch("asyncio.sleep", side_effect=mock_sleep),
            ):
                from app.services.ics_worker.worker import main

                await main()

            # Item should be requeued
            mock_mark.assert_any_call(db_session, mock_item, "queued")
        finally:
            worker_mod._shutdown_requested = original

    @pytest.mark.asyncio
    async def test_main_search_exception(self, db_session):
        """When search raises an exception, item is marked failed."""
        _seed_worker_status(db_session)
        import app.services.ics_worker.worker as worker_mod

        mock_session = MagicMock()
        mock_session.start = AsyncMock()
        mock_session.stop = AsyncMock()
        mock_session.is_logged_in = True
        mock_session.ensure_session = AsyncMock(return_value=True)
        mock_session.page = MagicMock()

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = True
        mock_scheduler.time_for_break.return_value = False
        mock_scheduler.next_delay.return_value = 0

        mock_config = MagicMock()
        mock_config.ICS_MAX_DAILY_SEARCHES = 1000

        mock_breaker = MagicMock()
        mock_breaker.should_stop.return_value = False

        mock_item = MagicMock()
        mock_item.id = 1
        mock_item.mpn = "ERR-PART"

        item_returned = False
        call_count = 0

        def get_next(db):
            nonlocal item_returned
            if not item_returned:
                item_returned = True
                return mock_item
            return None

        async def mock_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                worker_mod._shutdown_requested = True

        original = worker_mod._shutdown_requested
        try:
            worker_mod._shutdown_requested = False

            with (
                patch("app.database.SessionLocal", return_value=db_session),
                patch("app.services.ics_worker.queue_manager.recover_stale_searches"),
                patch("app.services.ics_worker.session_manager.IcsSessionManager", return_value=mock_session),
                patch("app.services.ics_worker.config.IcsConfig", return_value=mock_config),
                patch("app.services.ics_worker.scheduler.SearchScheduler", return_value=mock_scheduler),
                patch("app.services.ics_worker.circuit_breaker.CircuitBreaker", return_value=mock_breaker),
                patch("app.services.ics_worker.ai_gate.process_ai_gate", new_callable=AsyncMock),
                patch("app.services.ics_worker.queue_manager.get_next_queued_item", side_effect=get_next),
                patch("app.services.ics_worker.queue_manager.mark_status") as mock_mark,
                patch(
                    "app.services.ics_worker.search_engine.search_part",
                    new_callable=AsyncMock,
                    side_effect=Exception("Network timeout"),
                    create=True,
                ),
                patch("asyncio.sleep", side_effect=mock_sleep),
            ):
                from app.services.ics_worker.worker import main

                await main()

            # Mark failed should have been called
            calls = [str(c) for c in mock_mark.call_args_list]
            assert any("failed" in c for c in calls)
        finally:
            worker_mod._shutdown_requested = original

    @pytest.mark.asyncio
    async def test_main_empty_sightings_records_empty(self, db_session):
        """When search returns no sightings, breaker.record_empty_results is called."""
        _seed_worker_status(db_session)
        import app.services.ics_worker.worker as worker_mod

        mock_session = MagicMock()
        mock_session.start = AsyncMock()
        mock_session.stop = AsyncMock()
        mock_session.is_logged_in = True
        mock_session.ensure_session = AsyncMock(return_value=True)
        mock_session.page = MagicMock()

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = True
        mock_scheduler.time_for_break.return_value = False
        mock_scheduler.next_delay.return_value = 0

        mock_config = MagicMock()
        mock_config.ICS_MAX_DAILY_SEARCHES = 1000

        mock_breaker = MagicMock()
        mock_breaker.should_stop.return_value = False
        mock_breaker.check_page_health = AsyncMock(return_value="HEALTHY")

        mock_item = MagicMock()
        mock_item.id = 1
        mock_item.mpn = "EMPTY-PART"

        item_returned = False
        call_count = 0

        def get_next(db):
            nonlocal item_returned
            if not item_returned:
                item_returned = True
                return mock_item
            return None

        async def mock_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                worker_mod._shutdown_requested = True

        original = worker_mod._shutdown_requested
        try:
            worker_mod._shutdown_requested = False

            with (
                patch("app.database.SessionLocal", return_value=db_session),
                patch("app.services.ics_worker.queue_manager.recover_stale_searches"),
                patch("app.services.ics_worker.session_manager.IcsSessionManager", return_value=mock_session),
                patch("app.services.ics_worker.config.IcsConfig", return_value=mock_config),
                patch("app.services.ics_worker.scheduler.SearchScheduler", return_value=mock_scheduler),
                patch("app.services.ics_worker.circuit_breaker.CircuitBreaker", return_value=mock_breaker),
                patch("app.services.ics_worker.ai_gate.process_ai_gate", new_callable=AsyncMock),
                patch("app.services.ics_worker.queue_manager.get_next_queued_item", side_effect=get_next),
                patch("app.services.ics_worker.queue_manager.mark_status"),
                patch("app.services.ics_worker.queue_manager.mark_completed"),
                patch(
                    "app.services.ics_worker.search_engine.search_part",
                    new_callable=AsyncMock,
                    return_value={"html": "<html></html>", "duration_ms": 200},
                    create=True,
                ),
                patch("app.services.ics_worker.result_parser.parse_results_html", return_value=[]),
                patch("app.services.ics_worker.sighting_writer.save_ics_sightings", return_value=0),
                patch("asyncio.sleep", side_effect=mock_sleep),
            ):
                from app.services.ics_worker.worker import main

                await main()

            mock_breaker.record_empty_results.assert_called_once()
        finally:
            worker_mod._shutdown_requested = original


# ── Tests: EASTERN timezone ──────────────────────────────────────


class TestTimezone:
    def test_eastern_timezone_defined(self):
        assert EASTERN is not None
        assert str(EASTERN) == "America/New_York"
