"""Tests for missing coverage in enrichment_worker/worker.py and __main__.py.

Covers:
- _handle_shutdown signal handler (lines 43-44)
- run_one_batch spec extraction generic exception (lines 265-266)
- run_one_batch commit failure (lines 270-272)
- main() function (lines 294-493): startup, batch, daily cap, circuit breaker, daily reset
- __main__.py entry point (lines 3-7)

Called by: pytest autodiscovery
Depends on: app.services.enrichment_worker.worker, app.database
"""

import asyncio
import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

os.environ["TESTING"] = "1"


# ---------------------------------------------------------------------------
# _handle_shutdown (lines 43-44)
# ---------------------------------------------------------------------------


def test_handle_shutdown_sets_global_flag():
    """_handle_shutdown sets _shutdown_requested to True."""
    import app.services.enrichment_worker.worker as w

    original = w._shutdown_requested
    try:
        w._shutdown_requested = False
        w._handle_shutdown(15, None)
        assert w._shutdown_requested is True
    finally:
        w._shutdown_requested = original


# ---------------------------------------------------------------------------
# run_one_batch — spec extraction generic exception (lines 265-266)
# ---------------------------------------------------------------------------


def test_run_one_batch_spec_extraction_generic_exception(db_session):
    """A non-Claude exception in spec extraction is logged; breaker is NOT tripped."""
    from app.constants import MaterialEnrichmentStatus
    from app.models import MaterialCard
    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig
    from app.services.enrichment_worker.worker import run_one_batch

    now = datetime.now(timezone.utc)
    db_session.add(
        MaterialCard(normalized_mpn="gex1", display_mpn="GEX1", enrichment_status="unenriched", created_at=now)
    )
    db_session.flush()

    async def fake_enrich(card, db, **kw):
        card.enrichment_status = MaterialEnrichmentStatus.VERIFIED
        return MaterialEnrichmentStatus.VERIFIED

    spec_mock = AsyncMock(side_effect=RuntimeError("db hiccup"))
    cfg = EnrichmentWorkerConfig(batch_size=5, web_daily_cap=80, circuit_breaker_errors=5)
    breaker = EnrichmentCircuitBreaker(cfg)

    with (
        patch("app.services.enrichment_worker.worker.enrich_card", side_effect=fake_enrich),
        patch("app.services.enrichment_worker.worker._connectors_in_order", return_value=[]),
        patch("app.services.enrichment_worker.worker.intel_cache.get_cached", return_value=None),
        patch("app.services.enrichment_worker.worker.intel_cache.set_cached"),
        patch("app.services.spec_enrichment_service.enrich_card_specs", spec_mock),
    ):
        counts = asyncio.run(run_one_batch(db_session, cfg, {}, breaker, set(), {"web_calls": 0}))

    assert not breaker.should_stop()
    assert counts.get(MaterialEnrichmentStatus.VERIFIED, 0) == 1


# ---------------------------------------------------------------------------
# run_one_batch — commit failure (lines 270-272)
# ---------------------------------------------------------------------------


def test_run_one_batch_commit_failure_triggers_rollback(db_session):
    """When db.commit() raises, the batch rolls back without crashing."""
    from app.constants import MaterialEnrichmentStatus
    from app.models import MaterialCard
    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig
    from app.services.enrichment_worker.worker import run_one_batch

    now = datetime.now(timezone.utc)
    db_session.add(
        MaterialCard(normalized_mpn="cf01", display_mpn="CF01", enrichment_status="unenriched", created_at=now)
    )
    db_session.flush()

    async def fake_enrich(card, db, **kw):
        return MaterialEnrichmentStatus.VERIFIED

    cfg = EnrichmentWorkerConfig(batch_size=5, web_daily_cap=80)
    breaker = EnrichmentCircuitBreaker(cfg)

    commit_count = [0]
    rollback_count = [0]
    original_commit = db_session.commit
    original_rollback = db_session.rollback

    def bad_commit():
        commit_count[0] += 1
        raise Exception("simulated commit failure")

    def fake_rollback():
        rollback_count[0] += 1

    db_session.commit = bad_commit
    db_session.rollback = fake_rollback

    try:
        with (
            patch("app.services.enrichment_worker.worker.enrich_card", side_effect=fake_enrich),
            patch("app.services.enrichment_worker.worker._connectors_in_order", return_value=[]),
            patch("app.services.enrichment_worker.worker.intel_cache.get_cached", return_value=None),
            patch("app.services.enrichment_worker.worker.intel_cache.set_cached"),
        ):
            asyncio.run(run_one_batch(db_session, cfg, {}, breaker, set(), {"web_calls": 0}))
    finally:
        db_session.commit = original_commit
        db_session.rollback = original_rollback

    assert commit_count[0] == 1
    assert rollback_count[0] == 1


# ---------------------------------------------------------------------------
# main() helpers
# ---------------------------------------------------------------------------


def _mock_db():
    db = MagicMock()
    db.close = MagicMock()
    return db


# ---------------------------------------------------------------------------
# main() — basic: one batch then shutdown (lines 294-493)
# ---------------------------------------------------------------------------


async def test_main_runs_one_batch_then_shuts_down():
    """Main() does startup heartbeat, runs one batch, exits on shutdown."""
    import app.services.enrichment_worker.worker as w

    original = w._shutdown_requested
    w._shutdown_requested = False
    batch_count = [0]

    async def fake_batch(*args, **kwargs):
        batch_count[0] += 1
        w._shutdown_requested = True
        return {"verified": 2}

    try:
        with (
            patch("app.database.SessionLocal", return_value=_mock_db()),
            patch("app.models.enrichment_worker_status.update_enrichment_worker_status"),
            patch("app.services.enrichment_worker.worker.run_one_batch", side_effect=fake_batch),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await w.main()
    finally:
        w._shutdown_requested = original

    assert batch_count[0] == 1


# ---------------------------------------------------------------------------
# main() — empty batch uses idle sleep
# ---------------------------------------------------------------------------


async def test_main_empty_batch_uses_idle_sleep():
    """Empty batch causes main() to sleep with idle_sleep_seconds (default 60)."""
    import app.services.enrichment_worker.worker as w

    original = w._shutdown_requested
    w._shutdown_requested = False
    sleep_calls = []

    async def fake_sleep(secs):
        sleep_calls.append(secs)

    async def fake_batch(*args, **kwargs):
        w._shutdown_requested = True
        return {}

    try:
        with (
            patch("app.database.SessionLocal", return_value=_mock_db()),
            patch("app.models.enrichment_worker_status.update_enrichment_worker_status"),
            patch("app.services.enrichment_worker.worker.run_one_batch", side_effect=fake_batch),
            patch("asyncio.sleep", side_effect=fake_sleep),
        ):
            await w.main()
    finally:
        w._shutdown_requested = original

    assert any(s == 60 for s in sleep_calls)


# ---------------------------------------------------------------------------
# main() — daily cap reached (lines 396-403)
# ---------------------------------------------------------------------------


async def test_main_daily_cap_sleeps_one_hour():
    """When daily_cap is reached, main() sleeps 1h before the next batch."""
    import app.services.enrichment_worker.worker as w

    original = w._shutdown_requested
    w._shutdown_requested = False
    sleep_calls = []

    async def fake_sleep(secs):
        sleep_calls.append(secs)
        if secs == 3600:
            w._shutdown_requested = True

    async def fake_batch(*args, **kwargs):
        return {"verified": 200}  # sum == daily_cap (200) → cap hits next iteration

    try:
        with (
            patch("app.database.SessionLocal", return_value=_mock_db()),
            patch("app.models.enrichment_worker_status.update_enrichment_worker_status"),
            patch("app.services.enrichment_worker.worker.run_one_batch", side_effect=fake_batch),
            patch("asyncio.sleep", side_effect=fake_sleep),
        ):
            await w.main()
    finally:
        w._shutdown_requested = original

    assert 3600 in sleep_calls


# ---------------------------------------------------------------------------
# main() — circuit breaker open (lines 406-422)
# ---------------------------------------------------------------------------


async def test_main_circuit_breaker_open_sleeps_one_hour():
    """When circuit breaker is open, main() sleeps 1h and updates CB status."""
    import app.services.enrichment_worker.worker as w
    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig

    original = w._shutdown_requested
    w._shutdown_requested = False
    sleep_calls = []

    async def fake_sleep(secs):
        sleep_calls.append(secs)
        if secs == 3600:
            w._shutdown_requested = True

    cfg = EnrichmentWorkerConfig(circuit_breaker_errors=1)
    tripped = EnrichmentCircuitBreaker(cfg)
    tripped.record_claude_error()  # trips immediately with threshold=1

    try:
        with (
            patch("app.database.SessionLocal", return_value=_mock_db()),
            patch("app.models.enrichment_worker_status.update_enrichment_worker_status"),
            patch("app.services.enrichment_worker.worker.run_one_batch"),
            patch("asyncio.sleep", side_effect=fake_sleep),
            patch(
                "app.services.enrichment_worker.circuit_breaker.EnrichmentCircuitBreaker",
                return_value=tripped,
            ),
        ):
            await w.main()
    finally:
        w._shutdown_requested = original

    assert 3600 in sleep_calls


# ---------------------------------------------------------------------------
# main() — batch exception is caught (lines 463-464)
# ---------------------------------------------------------------------------


async def test_main_batch_exception_caught_loop_continues():
    """An exception in run_one_batch is caught; the loop continues."""
    import app.services.enrichment_worker.worker as w

    original = w._shutdown_requested
    w._shutdown_requested = False
    batch_calls = [0]

    async def fake_batch(*args, **kwargs):
        batch_calls[0] += 1
        if batch_calls[0] == 1:
            raise RuntimeError("batch explodes")
        w._shutdown_requested = True
        return {}

    try:
        with (
            patch("app.database.SessionLocal", return_value=_mock_db()),
            patch("app.models.enrichment_worker_status.update_enrichment_worker_status"),
            patch("app.services.enrichment_worker.worker.run_one_batch", side_effect=fake_batch),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await w.main()
    finally:
        w._shutdown_requested = original

    assert batch_calls[0] == 2


# ---------------------------------------------------------------------------
# main() — daily reset archives previous day (lines 349-393)
# ---------------------------------------------------------------------------


async def test_main_daily_reset_archives_previous_day_stats():
    """On a day rollover, main() archives yesterday's stats and resets counters."""
    import app.services.enrichment_worker.worker as w

    original = w._shutdown_requested
    w._shutdown_requested = False

    day1 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    day2 = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
    call_n = [0]

    class FakeDatetime:
        @staticmethod
        def now(tz=None):
            call_n[0] += 1
            # Calls 1-4: startup heartbeat + iter-1 date + iter-1 heartbeats
            # Call 5+: iter-2 date check → day2 triggers the reset
            return day1 if call_n[0] <= 4 else day2

    update_calls = []

    def fake_update(db, **kwargs):
        update_calls.append(dict(kwargs))

    batch_count = [0]

    async def fake_batch(*args, **kwargs):
        batch_count[0] += 1
        if batch_count[0] >= 2:
            w._shutdown_requested = True
        return {"verified": 1}

    try:
        with (
            patch("app.database.SessionLocal", return_value=_mock_db()),
            patch(
                "app.models.enrichment_worker_status.update_enrichment_worker_status",
                side_effect=fake_update,
            ),
            patch("app.services.enrichment_worker.worker.run_one_batch", side_effect=fake_batch),
            patch("app.services.enrichment_worker.worker.datetime", FakeDatetime),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await w.main()
    finally:
        w._shutdown_requested = original

    assert batch_count[0] == 2
    archive = [c for c in update_calls if "daily_stats_json" in c]
    assert len(archive) >= 1
    assert archive[0]["daily_stats_json"]["date"] == "2026-01-01"


# ---------------------------------------------------------------------------
# __main__.py entry point (lines 3-7)
# ---------------------------------------------------------------------------


def test_main_module_entry_point_calls_asyncio_run():
    """app.services.enrichment_worker.__main__ calls asyncio.run(main())."""
    mod_key = "app.services.enrichment_worker.__main__"
    saved = sys.modules.pop(mod_key, None)
    try:

        def close_coro(coro):
            try:
                coro.close()
            except Exception:
                pass

        with patch("asyncio.run", side_effect=close_coro) as mock_run:
            import app.services.enrichment_worker.__main__  # noqa: F401

        mock_run.assert_called_once()
    finally:
        if saved is not None:
            sys.modules[mod_key] = saved
        elif mod_key in sys.modules:
            del sys.modules[mod_key]
