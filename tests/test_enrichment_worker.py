"""Tests for the enrichment worker status model (Task 7) and worker config + circuit
breaker (Task 8).

The singleton row is seeded by the Alembic migration in Postgres. In SQLite tests,
create_all builds the table but does not run migrations, so the row may be absent — the
test tolerates None (row is None or id==1).
"""

import pytest


def test_worker_status_singleton(db_session):
    from app.models.enrichment_worker_status import EnrichmentWorkerStatus

    row = db_session.get(EnrichmentWorkerStatus, 1)
    # Migration seeds id=1 in Postgres; in SQLite tests the row is absent (None).
    assert row is None or row.id == 1


def test_worker_status_model_importable():
    """Smoke test: the model and helper are importable and have expected columns."""
    from app.models.enrichment_worker_status import (
        EnrichmentWorkerStatus,
        update_enrichment_worker_status,
    )

    cols = {c.key for c in EnrichmentWorkerStatus.__table__.columns}
    expected_cols = {
        "id",
        "is_running",
        "last_heartbeat",
        "last_enriched_at",
        "enriched_today",
        "web_sourced_today",
        "ai_inferred_today",
        "not_found_today",
        "circuit_breaker_open",
        "circuit_breaker_reason",
        "daily_stats_json",
        "updated_at",
    }
    assert expected_cols <= cols
    assert callable(update_enrichment_worker_status)


def test_worker_status_singleton_constraint():
    """The CheckConstraint name is correct."""
    from app.models.enrichment_worker_status import EnrichmentWorkerStatus

    constraints = {c.name for c in EnrichmentWorkerStatus.__table__.constraints}
    assert "ck_enrichment_worker_status_singleton" in constraints


def test_update_helper_noop_when_no_row(db_session):
    """update_enrichment_worker_status is a no-op when the singleton row is absent."""
    from app.models.enrichment_worker_status import update_enrichment_worker_status

    # Should not raise even if row doesn't exist
    update_enrichment_worker_status(db_session, is_running=True, enriched_today=5)


def test_update_helper_sets_fields(db_session):
    """update_enrichment_worker_status sets columns on the singleton row."""

    from app.models.enrichment_worker_status import (
        EnrichmentWorkerStatus,
        update_enrichment_worker_status,
    )

    # Seed the singleton row (mimicking the migration)
    row = EnrichmentWorkerStatus(id=1)
    db_session.add(row)
    db_session.commit()

    update_enrichment_worker_status(
        db_session,
        is_running=True,
        enriched_today=10,
        web_sourced_today=3,
        ai_inferred_today=4,
        not_found_today=3,
    )

    db_session.expire_all()
    refreshed = db_session.get(EnrichmentWorkerStatus, 1)
    assert refreshed is not None
    assert refreshed.is_running is True
    assert refreshed.enriched_today == 10
    assert refreshed.web_sourced_today == 3
    assert refreshed.ai_inferred_today == 4
    assert refreshed.not_found_today == 3
    assert refreshed.updated_at is not None


# ---------------------------------------------------------------------------
# _record_heartbeat — top-of-loop liveness write (every tick)
# ---------------------------------------------------------------------------


def test_record_heartbeat_advances_with_closed_breaker(db_session):
    """_record_heartbeat refreshes last_heartbeat, marks is_running, and clears the
    breaker flag when the breaker is CLOSED — and returns False."""
    from datetime import datetime, timedelta, timezone

    from app.models.enrichment_worker_status import EnrichmentWorkerStatus
    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig
    from app.services.enrichment_worker.worker import _record_heartbeat

    # Seed the singleton with a stale heartbeat + a stale-open breaker flag.
    stale = datetime.now(timezone.utc) - timedelta(hours=2)
    db_session.add(
        EnrichmentWorkerStatus(
            id=1,
            is_running=False,
            last_heartbeat=stale,
            circuit_breaker_open=True,
            circuit_breaker_reason="old reason",
        )
    )
    db_session.commit()

    before = datetime.now(timezone.utc) - timedelta(seconds=5)
    breaker = EnrichmentCircuitBreaker(EnrichmentWorkerConfig(circuit_breaker_errors=3))

    result = _record_heartbeat(db_session, breaker)

    assert result is False
    db_session.expire_all()
    row = db_session.get(EnrichmentWorkerStatus, 1)
    assert row.is_running is True
    assert row.last_heartbeat >= before  # advanced to ~now
    assert row.circuit_breaker_open is False  # stale True cleared
    assert row.circuit_breaker_reason is None


def test_record_heartbeat_persists_open_breaker_and_reason(db_session):
    """With a TRIPPED breaker, _record_heartbeat persists circuit_breaker_open=True with
    the trip reason — and returns True."""
    from datetime import datetime, timedelta, timezone

    from app.models.enrichment_worker_status import EnrichmentWorkerStatus
    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig
    from app.services.enrichment_worker.worker import _record_heartbeat

    db_session.add(EnrichmentWorkerStatus(id=1))
    db_session.commit()

    cfg = EnrichmentWorkerConfig(circuit_breaker_errors=2)
    breaker = EnrichmentCircuitBreaker(cfg)
    for _ in range(cfg.circuit_breaker_errors):
        breaker.record_claude_error()
    assert breaker.should_stop()  # tripped

    before = datetime.now(timezone.utc) - timedelta(seconds=5)
    result = _record_heartbeat(db_session, breaker)

    assert result is True
    db_session.expire_all()
    row = db_session.get(EnrichmentWorkerStatus, 1)
    assert row.is_running is True
    assert row.last_heartbeat >= before
    assert row.circuit_breaker_open is True
    assert row.circuit_breaker_reason  # non-empty trip reason set


# ---------------------------------------------------------------------------
# Task 8: EnrichmentWorkerConfig
# ---------------------------------------------------------------------------


def test_config_defaults():
    """EnrichmentWorkerConfig has the spec §5.5 defaults."""
    import os

    # Remove any stale overrides before testing defaults
    for key in [
        "ENRICHMENT_BATCH_SIZE",
        "ENRICHMENT_DAILY_CAP",
        "ENRICHMENT_WEB_DAILY_CAP",
        "ENRICHMENT_LOOP_SLEEP_SECONDS",
        "ENRICHMENT_IDLE_SLEEP_SECONDS",
        "ENRICHMENT_NOT_FOUND_RETRY_HOURS",
        "ENRICHMENT_CIRCUIT_BREAKER_ERRORS",
    ]:
        os.environ.pop(key, None)

    from app.services.enrichment_worker.config import EnrichmentWorkerConfig

    cfg = EnrichmentWorkerConfig.from_env()
    assert cfg.batch_size == 5
    assert cfg.daily_cap == 200
    assert cfg.web_daily_cap == 80
    assert cfg.loop_sleep_seconds == 30
    assert cfg.idle_sleep_seconds == 60
    assert cfg.not_found_retry_hours == 22
    assert cfg.circuit_breaker_errors == 5


def test_config_env_override(monkeypatch):
    """An env-var override is reflected in EnrichmentWorkerConfig.from_env()."""
    monkeypatch.setenv("ENRICHMENT_BATCH_SIZE", "10")
    monkeypatch.setenv("ENRICHMENT_DAILY_CAP", "500")

    from app.services.enrichment_worker.config import EnrichmentWorkerConfig

    cfg = EnrichmentWorkerConfig.from_env()
    assert cfg.batch_size == 10
    assert cfg.daily_cap == 500
    # Unchanged fields stay at default
    assert cfg.web_daily_cap == 80


def test_config_direct_kwargs():
    """EnrichmentWorkerConfig can be constructed with direct kwargs (for tests/Task
    9)."""
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig

    cfg = EnrichmentWorkerConfig(batch_size=10, not_found_retry_hours=22)
    assert cfg.batch_size == 10
    assert cfg.not_found_retry_hours == 22
    # Other fields stay at defaults
    assert cfg.daily_cap == 200
    assert cfg.web_daily_cap == 80


# ---------------------------------------------------------------------------
# Task 8: EnrichmentCircuitBreaker
# ---------------------------------------------------------------------------


def test_breaker_does_not_trip_below_threshold():
    """Fewer than N consecutive errors leaves the breaker closed."""
    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig

    cfg = EnrichmentWorkerConfig(circuit_breaker_errors=3)
    breaker = EnrichmentCircuitBreaker(cfg)
    breaker.record_claude_error()
    breaker.record_claude_error()
    assert not breaker.should_stop()


def test_breaker_trips_after_n_consecutive_errors():
    """N consecutive Claude errors trips the breaker (should_stop() → True)."""
    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig

    cfg = EnrichmentWorkerConfig(circuit_breaker_errors=3)
    breaker = EnrichmentCircuitBreaker(cfg)
    for _ in range(3):
        breaker.record_claude_error()
    assert breaker.should_stop()
    assert breaker.is_open


def test_breaker_success_resets_counter():
    """A success resets the consecutive-error counter so the breaker stays closed."""
    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig

    cfg = EnrichmentWorkerConfig(circuit_breaker_errors=3)
    breaker = EnrichmentCircuitBreaker(cfg)
    breaker.record_claude_error()
    breaker.record_claude_error()
    breaker.record_claude_success()  # resets counter
    breaker.record_claude_error()
    breaker.record_claude_error()
    # Only 2 consecutive errors after the reset — not tripped
    assert not breaker.should_stop()


def test_breaker_cooldown_resets_after_1h():
    """After a 1h cooldown, should_stop() returns False even if the breaker was open."""
    import time
    from unittest.mock import patch

    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig

    cfg = EnrichmentWorkerConfig(circuit_breaker_errors=2)
    breaker = EnrichmentCircuitBreaker(cfg)
    breaker.record_claude_error()
    breaker.record_claude_error()
    assert breaker.should_stop()

    # Simulate 1h + 1s elapsing since the breaker was tripped
    future = time.monotonic() + 3601
    with patch("time.monotonic", return_value=future):
        assert not breaker.should_stop()
        # is_open should also be cleared
        assert not breaker.is_open


def test_breaker_get_trip_info_includes_custom_fields():
    """get_trip_info() includes the base fields and trip_reason after trip."""
    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig

    cfg = EnrichmentWorkerConfig(circuit_breaker_errors=1)
    breaker = EnrichmentCircuitBreaker(cfg)
    breaker.record_claude_error()
    info = breaker.get_trip_info()
    assert info["is_open"] is True
    assert "consecutive_failures" in info
    assert info["trip_reason"] != ""


# ---------------------------------------------------------------------------
# Task 9: select_batch (anti-spin query)
# ---------------------------------------------------------------------------


def test_select_batch_anti_spin(db_session):
    """select_batch returns unenriched + old not_found; excludes recent not_found,
    verified, is_internal_part, and deleted cards."""
    from datetime import datetime, timedelta, timezone

    from app.models import MaterialCard
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig
    from app.services.enrichment_worker.worker import select_batch

    now = datetime.now(timezone.utc)

    def mk(mpn, status, enriched=None, sc=0, internal=False, deleted=None):
        c = MaterialCard(
            normalized_mpn=mpn,
            display_mpn=mpn.upper(),
            enrichment_status=status,
            enriched_at=enriched,
            search_count=sc,
            created_at=now,
            is_internal_part=internal,
            deleted_at=deleted,
        )
        db_session.add(c)
        return c

    mk("u1", "unenriched", sc=5)
    mk("nf_old", "not_found", enriched=now - timedelta(hours=30))
    mk("nf_recent", "not_found", enriched=now - timedelta(hours=1))
    mk("ver", "verified")
    mk("internal_u", "unenriched", internal=True)
    mk("deleted_u", "unenriched", deleted=now - timedelta(days=1))
    # not_found with enriched_at=None is also eligible
    mk("nf_none", "not_found", enriched=None)
    db_session.flush()

    cfg = EnrichmentWorkerConfig(batch_size=10, not_found_retry_hours=22)
    picked = {c.normalized_mpn for c in select_batch(db_session, cfg)}

    assert "u1" in picked
    assert "nf_old" in picked
    assert "nf_none" in picked
    assert "nf_recent" not in picked  # within retry window
    assert "ver" not in picked  # already verified
    assert "internal_u" not in picked  # is_internal_part
    assert "deleted_u" not in picked  # soft-deleted


def test_select_batch_prioritizes_unenriched_over_not_found_recheck(db_session):
    """A never-resolved `unenriched` card is selected before re-checking an
    already-`not_found` card — even when the not_found card is newer and equal demand.

    Guards the starvation bug where old, low-demand `unenriched` parts sink below
    the daily `not_found` re-check churn (both have search_count=0, so the
    created_at DESC tiebreaker picked the newer not_found card first and the
    200/day cap was exhausted before the old unenriched parts were ever reached).
    """
    from datetime import datetime, timedelta, timezone

    from app.models import MaterialCard
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig
    from app.services.enrichment_worker.worker import select_batch

    now = datetime.now(timezone.utc)
    # Old, never-resolved, no demand.
    db_session.add(
        MaterialCard(
            normalized_mpn="old_unenriched",
            display_mpn="OLD_UNENRICHED",
            enrichment_status="unenriched",
            search_count=0,
            created_at=now - timedelta(days=60),
        )
    )
    # Newer, re-eligible not_found (enriched_at past the retry window), same demand.
    db_session.add(
        MaterialCard(
            normalized_mpn="new_not_found",
            display_mpn="NEW_NOT_FOUND",
            enrichment_status="not_found",
            search_count=0,
            created_at=now - timedelta(days=1),
            enriched_at=now - timedelta(hours=30),
        )
    )
    db_session.flush()

    cfg = EnrichmentWorkerConfig(batch_size=1, not_found_retry_hours=22)
    picked = [c.normalized_mpn for c in select_batch(db_session, cfg)]

    # With a single slot, the never-resolved part must win over the not_found re-check.
    assert picked == ["old_unenriched"]


def test_select_batch_ordering(db_session):
    """Cards with higher demand telemetry (sourced_qty_90d) should appear first.

    Migration 105 replaced the old search_count demand key with TRIO's own SFDC sourcing
    volume (sourced_qty_90d DESC NULLS LAST) — see select_batch ORDER BY.
    """
    from datetime import datetime, timezone

    from app.models import MaterialCard
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig
    from app.services.enrichment_worker.worker import select_batch

    now = datetime.now(timezone.utc)
    for mpn, qty in [("low_q", 1), ("high_q", 99), ("mid_q", 10)]:
        db_session.add(
            MaterialCard(
                normalized_mpn=mpn,
                display_mpn=mpn.upper(),
                enrichment_status="unenriched",
                sourced_qty_90d=qty,
                created_at=now,
            )
        )
    db_session.flush()

    cfg = EnrichmentWorkerConfig(batch_size=5)
    results = select_batch(db_session, cfg)
    qtys = [c.sourced_qty_90d for c in results]
    assert qtys == sorted(qtys, reverse=True)


def test_select_batch_recency_tiebreaker(db_session):
    """Among equal demand (sourced_qty_90d), the most-recently-sourced card wins.

    last_sourced_at DESC NULLS LAST is the secondary demand-telemetry key (migration
    105) — a card TRIO sourced more recently heads the next batch over an equally-
    demanded staler one; the id tiebreak keeps the order deterministic.
    """
    from datetime import datetime, timedelta, timezone

    from app.models import MaterialCard
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig
    from app.services.enrichment_worker.worker import select_batch

    now = datetime.now(timezone.utc)
    for mpn, sourced in [
        ("oldest", now - timedelta(days=30)),
        ("middle", now - timedelta(days=15)),
        ("newest", now - timedelta(days=1)),
    ]:
        db_session.add(
            MaterialCard(
                normalized_mpn=mpn,
                display_mpn=mpn.upper(),
                enrichment_status="unenriched",
                sourced_qty_90d=10,
                last_sourced_at=sourced,
                created_at=now,
            )
        )
    db_session.flush()

    cfg = EnrichmentWorkerConfig(batch_size=5)
    order = [c.normalized_mpn for c in select_batch(db_session, cfg)]
    assert order == ["newest", "middle", "oldest"]


def test_select_batch_demand_beats_recency(db_session):
    """Demand is primary: a high-sourced_qty_90d card with no recency outranks a low-
    demand card sourced just yesterday.

    last_sourced_at only breaks ties within equal sourced_qty_90d; it never overrides
    the demand-volume key (migration 105).
    """
    from datetime import datetime, timedelta, timezone

    from app.models import MaterialCard
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig
    from app.services.enrichment_worker.worker import select_batch

    now = datetime.now(timezone.utc)
    db_session.add(
        MaterialCard(
            normalized_mpn="hot_stale",
            display_mpn="HOT_STALE",
            enrichment_status="unenriched",
            sourced_qty_90d=50,
            last_sourced_at=None,
            created_at=now - timedelta(days=7),
        )
    )
    db_session.add(
        MaterialCard(
            normalized_mpn="cold_fresh",
            display_mpn="COLD_FRESH",
            enrichment_status="unenriched",
            sourced_qty_90d=1,
            last_sourced_at=now,
            created_at=now,
        )
    )
    db_session.flush()

    cfg = EnrichmentWorkerConfig(batch_size=5)
    order = [c.normalized_mpn for c in select_batch(db_session, cfg)]
    assert order[0] == "hot_stale"


def test_new_cards_are_enrichable_by_worker(db_session):
    """Any new MaterialCard creation path feeds the worker.

    Mimics stock-import / email-attachment creation: construct without passing
    enrichment_status, flush, and assert it defaults to 'unenriched' and is selected by
    the worker. Guards the single-enrichment-authority invariant.
    """
    from app.models import MaterialCard
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig
    from app.services.enrichment_worker.worker import select_batch

    card = MaterialCard(
        normalized_mpn="fresh_part",
        display_mpn="FRESH_PART",
    )
    db_session.add(card)
    db_session.flush()

    assert card.enrichment_status == "unenriched"

    cfg = EnrichmentWorkerConfig(batch_size=10)
    picked = {c.normalized_mpn for c in select_batch(db_session, cfg)}
    assert "fresh_part" in picked


def test_select_batch_respects_batch_size(db_session):
    """select_batch returns at most batch_size cards."""
    from datetime import datetime, timezone

    from app.models import MaterialCard
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig
    from app.services.enrichment_worker.worker import select_batch

    now = datetime.now(timezone.utc)
    for i in range(10):
        db_session.add(
            MaterialCard(
                normalized_mpn=f"part{i}",
                display_mpn=f"PART{i}",
                enrichment_status="unenriched",
                created_at=now,
            )
        )
    db_session.flush()

    cfg = EnrichmentWorkerConfig(batch_size=3)
    assert len(select_batch(db_session, cfg)) == 3


# ---------------------------------------------------------------------------
# Task 9: run_one_batch
# ---------------------------------------------------------------------------


def test_run_one_batch_empty_returns_empty(db_session):
    """run_one_batch returns {} when there are no eligible cards."""
    import asyncio

    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig
    from app.services.enrichment_worker.worker import run_one_batch

    cfg = EnrichmentWorkerConfig(batch_size=5)
    breaker = EnrichmentCircuitBreaker(cfg)
    result = asyncio.run(run_one_batch(db_session, cfg, {}, breaker))
    assert result == {}


def test_run_one_batch_stamps_enriched_at_and_returns_counts(db_session, monkeypatch):
    """run_one_batch calls enrich_card for each card, stamps enriched_at, accumulates
    per-tier counts, and calls db.commit()."""
    import asyncio
    from datetime import datetime, timezone
    from unittest.mock import patch

    from app.constants import MaterialEnrichmentStatus
    from app.models import MaterialCard
    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig
    from app.services.enrichment_worker.worker import run_one_batch

    now = datetime.now(timezone.utc)
    cards = []
    statuses_to_return = [
        MaterialEnrichmentStatus.WEB_SOURCED,
        MaterialEnrichmentStatus.AI_INFERRED,
        MaterialEnrichmentStatus.NOT_FOUND,
    ]
    for i, st in enumerate(statuses_to_return):
        c = MaterialCard(
            normalized_mpn=f"p{i}",
            display_mpn=f"P{i}",
            enrichment_status="unenriched",
            created_at=now,
        )
        db_session.add(c)
        cards.append(c)
    db_session.flush()

    call_idx = [0]

    async def fake_enrich_card(card, db, **kw):
        status = statuses_to_return[call_idx[0]]
        call_idx[0] += 1
        card.enrichment_status = status
        return status

    cfg = EnrichmentWorkerConfig(batch_size=5, web_daily_cap=80)
    breaker = EnrichmentCircuitBreaker(cfg)

    with (
        patch(
            "app.services.enrichment_worker.worker.enrich_card",
            side_effect=fake_enrich_card,
        ),
        patch(
            "app.services.enrichment_worker.worker._connectors_in_order",
            return_value=[],
        ),
        patch(
            "app.services.enrichment_worker.worker.intel_cache.get_cached",
            return_value=None,
        ),
        patch(
            "app.services.enrichment_worker.worker.intel_cache.set_cached",
        ),
    ):
        counts = asyncio.run(run_one_batch(db_session, cfg, {}, breaker))

    assert counts.get(MaterialEnrichmentStatus.WEB_SOURCED, 0) == 1
    assert counts.get(MaterialEnrichmentStatus.AI_INFERRED, 0) == 1
    assert counts.get(MaterialEnrichmentStatus.NOT_FOUND, 0) == 1

    # enriched_at should be stamped on all cards
    for c in cards:
        assert c.enriched_at is not None


@pytest.mark.parametrize(
    ("mpn", "web_daily_cap", "cached_count", "web_disabled"),
    [
        ("testpart", 10, 10, True),  # at cap → web tier disabled
        ("testpart2", 80, 5, False),  # well below cap → web tier enabled
    ],
    ids=["at_cap", "below_cap"],
)
def test_run_one_batch_web_cap_gating(db_session, mpn, web_daily_cap, cached_count, web_disabled):
    """The web tier is disabled iff the cached daily count has reached web_daily_cap."""
    import asyncio
    from datetime import datetime, timezone
    from unittest.mock import patch

    from app.models import MaterialCard
    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig
    from app.services.enrichment_worker.worker import run_one_batch

    now = datetime.now(timezone.utc)
    db_session.add(
        MaterialCard(
            normalized_mpn=mpn,
            display_mpn=mpn.upper(),
            enrichment_status="unenriched",
            created_at=now,
        )
    )
    db_session.flush()

    captured_disabled: list[set] = []

    async def fake_enrich_card(card, db, disabled=None, **kw):
        captured_disabled.append(set(disabled) if disabled else set())
        return "not_found"

    cfg = EnrichmentWorkerConfig(batch_size=5, web_daily_cap=web_daily_cap)
    breaker = EnrichmentCircuitBreaker(cfg)

    with (
        patch(
            "app.services.enrichment_worker.worker.enrich_card",
            side_effect=fake_enrich_card,
        ),
        patch(
            "app.services.enrichment_worker.worker._connectors_in_order",
            return_value=[],
        ),
        patch(
            "app.services.enrichment_worker.worker.intel_cache.get_cached",
            return_value={"count": cached_count},
        ),
        patch("app.services.enrichment_worker.worker.intel_cache.set_cached"),
    ):
        asyncio.run(run_one_batch(db_session, cfg, {}, breaker))

    assert len(captured_disabled) == 1
    assert ("web_search" in captured_disabled[0]) is web_disabled


# ---------------------------------------------------------------------------
# run_one_batch — web-budget accounting, circuit breaker, persistence (review fixes)
# ---------------------------------------------------------------------------


def test_run_one_batch_charges_budget_per_billable_attempt(db_session):
    """Every non-verified card charges the web budget exactly once; a verified hit (a
    connector match, no Claude/web call) does NOT charge.

    The in-process tally in web_state carries the running count forward.
    """
    import asyncio
    from datetime import datetime, timezone
    from unittest.mock import patch

    from app.constants import MaterialEnrichmentStatus
    from app.models import MaterialCard
    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig
    from app.services.enrichment_worker.worker import run_one_batch

    now = datetime.now(timezone.utc)
    returns = [
        MaterialEnrichmentStatus.VERIFIED,  # no charge (connector hit)
        MaterialEnrichmentStatus.WEB_SOURCED,  # charge
        MaterialEnrichmentStatus.AI_INFERRED,  # charge
        MaterialEnrichmentStatus.NOT_FOUND,  # charge (gate-fail-then-fall-through still billed)
    ]
    for i in range(len(returns)):
        db_session.add(
            MaterialCard(normalized_mpn=f"b{i}", display_mpn=f"B{i}", enrichment_status="unenriched", created_at=now)
        )
    db_session.flush()

    idx = [0]

    async def fake_enrich_card(card, db, web_meter=None, **kw):
        st = returns[idx[0]]
        idx[0] += 1
        # Simulate real enrich_card: non-verified results make at least one web call.
        if web_meter is not None and st != MaterialEnrichmentStatus.VERIFIED:
            web_meter.reserve_web_call()
            web_meter.mark_claude_ok()
        return st

    # Stateful shared-counter fake (what Redis INCRBY provides in prod): each
    # billable card advances it atomically by its dispatched-call count.
    counter = {"value": 0}
    incr_amounts: list[int] = []

    def fake_incr(key, amount=1, ttl_days=1.0):
        incr_amounts.append(amount)
        counter["value"] += amount
        return counter["value"]

    cfg = EnrichmentWorkerConfig(batch_size=10, web_daily_cap=80)
    breaker = EnrichmentCircuitBreaker(cfg)
    web_state = {"web_calls": 0}

    with (
        patch("app.services.enrichment_worker.worker.enrich_card", side_effect=fake_enrich_card),
        patch("app.services.enrichment_worker.worker._connectors_in_order", return_value=[]),
        patch("app.services.enrichment_worker.worker.intel_cache.get_count", return_value=0),
        patch("app.services.enrichment_worker.worker.intel_cache.incr_count", side_effect=fake_incr),
    ):
        asyncio.run(run_one_batch(db_session, cfg, {}, breaker, set(), web_state))

    # 3 billable (web_sourced, ai_inferred, not_found); verified does not charge.
    assert incr_amounts == [1, 1, 1]
    assert counter["value"] == 3  # the shared counter advanced once per billable card
    assert web_state["web_calls"] == 3


def test_run_one_batch_in_process_budget_backstop_when_cache_down(db_session):
    """If the cache is unavailable (get_cached -> None) but the in-process web_state
    tally already meets the cap, the web tier is still disabled — WEB_DAILY_CAP is not
    bypassed."""
    import asyncio
    from datetime import datetime, timezone
    from unittest.mock import patch

    from app.constants import MaterialEnrichmentStatus
    from app.models import MaterialCard
    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig
    from app.services.enrichment_worker.worker import run_one_batch

    now = datetime.now(timezone.utc)
    db_session.add(MaterialCard(normalized_mpn="bk", display_mpn="BK", enrichment_status="unenriched", created_at=now))
    db_session.flush()

    captured: list[bool] = []

    async def fake_enrich_card(card, db, disabled=None, **kw):
        captured.append("web_search" in (disabled or set()))
        return MaterialEnrichmentStatus.NOT_FOUND

    cfg = EnrichmentWorkerConfig(batch_size=5, web_daily_cap=10)
    breaker = EnrichmentCircuitBreaker(cfg)

    with (
        patch("app.services.enrichment_worker.worker.enrich_card", side_effect=fake_enrich_card),
        patch("app.services.enrichment_worker.worker._connectors_in_order", return_value=[]),
        patch("app.services.enrichment_worker.worker.intel_cache.get_cached", return_value=None),  # cache down
        patch("app.services.enrichment_worker.worker.intel_cache.set_cached"),
    ):
        # web_state already at cap even though the cache reports nothing
        asyncio.run(run_one_batch(db_session, cfg, {}, breaker, set(), {"web_calls": 10}))

    assert captured == [True]


def test_run_one_batch_trips_breaker_on_claude_errors(db_session):
    """A Claude outage (enrich_card raising ClaudeError) feeds the circuit breaker so it
    trips after the threshold — instead of silently marking the whole queue
    not_found."""
    import asyncio
    from datetime import datetime, timezone
    from unittest.mock import patch

    from app.models import MaterialCard
    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig
    from app.services.enrichment_worker.worker import run_one_batch
    from app.utils.claude_errors import ClaudeRateLimitError

    now = datetime.now(timezone.utc)
    for i in range(3):
        db_session.add(
            MaterialCard(normalized_mpn=f"c{i}", display_mpn=f"C{i}", enrichment_status="unenriched", created_at=now)
        )
    db_session.flush()

    async def boom(card, db, **kw):
        raise ClaudeRateLimitError("429")

    cfg = EnrichmentWorkerConfig(batch_size=10, circuit_breaker_errors=3)
    breaker = EnrichmentCircuitBreaker(cfg)

    with (
        patch("app.services.enrichment_worker.worker.enrich_card", side_effect=boom),
        patch("app.services.enrichment_worker.worker._connectors_in_order", return_value=[]),
        patch("app.services.enrichment_worker.worker.intel_cache.get_cached", return_value=None),
        patch("app.services.enrichment_worker.worker.intel_cache.set_cached"),
    ):
        asyncio.run(run_one_batch(db_session, cfg, {}, breaker, set(), {"web_calls": 0}))

    assert breaker.should_stop()  # 3 consecutive Claude errors >= threshold


def test_run_one_batch_non_claude_exception_does_not_trip_breaker(db_session):
    """A non-Claude exception is logged but must NOT trip the Claude-specific breaker,
    and must not charge the web budget."""
    import asyncio
    from datetime import datetime, timezone
    from unittest.mock import patch

    from app.models import MaterialCard
    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig
    from app.services.enrichment_worker.worker import run_one_batch

    now = datetime.now(timezone.utc)
    for i in range(3):
        db_session.add(
            MaterialCard(normalized_mpn=f"d{i}", display_mpn=f"D{i}", enrichment_status="unenriched", created_at=now)
        )
    db_session.flush()

    async def boom(card, db, **kw):
        raise ValueError("a bug, not Claude")

    charged: list[int] = []

    cfg = EnrichmentWorkerConfig(batch_size=10, circuit_breaker_errors=3)
    breaker = EnrichmentCircuitBreaker(cfg)
    web_state = {"web_calls": 0}

    with (
        patch("app.services.enrichment_worker.worker.enrich_card", side_effect=boom),
        patch("app.services.enrichment_worker.worker._connectors_in_order", return_value=[]),
        patch("app.services.enrichment_worker.worker.intel_cache.get_cached", return_value=None),
        patch(
            "app.services.enrichment_worker.worker.intel_cache.set_cached",
            side_effect=lambda *a, **k: charged.append(1),
        ),
    ):
        asyncio.run(run_one_batch(db_session, cfg, {}, breaker, set(), web_state))

    assert not breaker.should_stop()  # non-Claude errors don't trip the Claude breaker
    assert charged == []  # no billable web call recorded on a hard failure
    assert web_state["web_calls"] == 0


def test_run_one_batch_disabled_set_persists_across_calls(db_session):
    """The caller-owned disabled set persists: a connector disabled by a prior batch stays
    disabled (passed through to enrich_card) instead of being re-tried every loop."""
    import asyncio
    from datetime import datetime, timezone
    from unittest.mock import patch

    from app.constants import MaterialEnrichmentStatus
    from app.models import MaterialCard
    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig
    from app.services.enrichment_worker.worker import run_one_batch

    now = datetime.now(timezone.utc)
    db_session.add(MaterialCard(normalized_mpn="dp", display_mpn="DP", enrichment_status="unenriched", created_at=now))
    db_session.flush()

    seen: list[set] = []

    async def fake_enrich_card(card, db, disabled=None, **kw):
        seen.append(set(disabled or set()))
        return MaterialEnrichmentStatus.NOT_FOUND

    cfg = EnrichmentWorkerConfig(batch_size=5, web_daily_cap=80)
    breaker = EnrichmentCircuitBreaker(cfg)
    disabled = {"digikey"}  # disabled by an earlier batch (quota/auth wall)

    with (
        patch("app.services.enrichment_worker.worker.enrich_card", side_effect=fake_enrich_card),
        patch("app.services.enrichment_worker.worker._connectors_in_order", return_value=[]),
        patch("app.services.enrichment_worker.worker.intel_cache.get_cached", return_value={"count": 0}),
        patch("app.services.enrichment_worker.worker.intel_cache.set_cached"),
    ):
        asyncio.run(run_one_batch(db_session, cfg, {}, breaker, disabled, {"web_calls": 0}))

    assert "digikey" in seen[0]  # passed through to enrich_card this batch
    assert "digikey" in disabled  # and still present afterward (same persistent object)


def test_run_one_batch_does_not_overshoot_cap_mid_batch(db_session):
    """The per-card gate prevents overshooting WEB_DAILY_CAP within a single batch: with
    cap=2 and 5 cards, exactly the first 2 fire a web call and the rest see the web tier
    disabled; web_state ends at exactly the cap (no batch_size-1 overshoot)."""
    import asyncio
    from datetime import datetime, timedelta, timezone
    from unittest.mock import patch

    from app.constants import MaterialEnrichmentStatus
    from app.models import MaterialCard
    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig
    from app.services.enrichment_worker.worker import run_one_batch

    now = datetime.now(timezone.utc)
    # Distinct created_at so the selection order is deterministic (fast-lane: newest first).
    for i in range(5):
        db_session.add(
            MaterialCard(
                normalized_mpn=f"mc{i}",
                display_mpn=f"MC{i}",
                enrichment_status="unenriched",
                created_at=now - timedelta(seconds=i),
            )
        )
    db_session.flush()

    web_enabled_seen: list[bool] = []

    async def fake_enrich_card(card, db, disabled=None, web_meter=None, **kw):
        enabled = "web_search" not in (disabled or set())
        web_enabled_seen.append(enabled)
        # Simulate real enrich_card: a web call is made only when web is enabled.
        if web_meter is not None and enabled:
            web_meter.reserve_web_call()
            web_meter.mark_claude_ok()
        return MaterialEnrichmentStatus.NOT_FOUND

    cfg = EnrichmentWorkerConfig(batch_size=5, web_daily_cap=2)
    breaker = EnrichmentCircuitBreaker(cfg)
    web_state = {"web_calls": 0}

    with (
        patch("app.services.enrichment_worker.worker.enrich_card", side_effect=fake_enrich_card),
        patch("app.services.enrichment_worker.worker._connectors_in_order", return_value=[]),
        patch("app.services.enrichment_worker.worker.intel_cache.get_cached", return_value=None),
        patch("app.services.enrichment_worker.worker.intel_cache.set_cached"),
    ):
        asyncio.run(run_one_batch(db_session, cfg, {}, breaker, set(), web_state))

    # Exactly the first 2 cards web-enabled (charge); remaining 3 disabled — no overshoot.
    assert web_enabled_seen == [True, True, False, False, False]
    assert web_state["web_calls"] == 2


# ---------------------------------------------------------------------------
# Task 6: not_catalogued config field + select_batch eligibility
# ---------------------------------------------------------------------------


from datetime import datetime, timedelta, timezone

from app.constants import MaterialEnrichmentStatus
from app.services.enrichment_worker.config import EnrichmentWorkerConfig


def test_config_has_not_catalogued_retry_days():
    c = EnrichmentWorkerConfig()
    assert c.not_catalogued_retry_days == 30


def test_select_batch_not_catalogued_eligibility(db_session):
    from app.models import MaterialCard
    from app.services.enrichment_worker.worker import select_batch

    now = datetime.now(timezone.utc)
    cfg = EnrichmentWorkerConfig(batch_size=10, not_catalogued_retry_days=30)
    fresh = MaterialCard(
        display_mpn="A1",
        normalized_mpn="a1",
        enrichment_status=MaterialEnrichmentStatus.NOT_CATALOGUED,
        enriched_at=now - timedelta(days=1),
    )
    stale = MaterialCard(
        display_mpn="A2",
        normalized_mpn="a2",
        enrichment_status=MaterialEnrichmentStatus.NOT_CATALOGUED,
        enriched_at=now - timedelta(days=40),
    )
    db_session.add_all([fresh, stale])
    db_session.commit()
    picked = {c.normalized_mpn for c in select_batch(db_session, cfg)}
    assert "a2" in picked  # past 30-day backoff → eligible
    assert "a1" not in picked  # within backoff → not yet


def test_breaker_resets_on_claude_ok_without_web(db_session):
    """A Claude call that returns OK with zero web calls (e.g. infer_part) still resets
    the breaker and does NOT charge the web budget."""
    import asyncio
    from datetime import datetime, timezone
    from unittest.mock import patch

    from app.models import MaterialCard
    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig
    from app.services.enrichment_worker.worker import run_one_batch

    now = datetime.now(timezone.utc)
    db_session.add(MaterialCard(normalized_mpn="e0", display_mpn="E0", enrichment_status="unenriched", created_at=now))
    db_session.flush()

    successes: list[int] = []

    class SpyBreaker(EnrichmentCircuitBreaker):
        def record_claude_success(self):
            successes.append(1)
            super().record_claude_success()

    async def fake_enrich_card(card, db, web_meter=None, **kw):
        if web_meter is not None:
            web_meter.mark_claude_ok()  # infer_part returned OK, no web call
        return MaterialEnrichmentStatus.AI_INFERRED

    charged: list[int] = []
    cfg = EnrichmentWorkerConfig(batch_size=10, web_daily_cap=80)
    web_state = {"web_calls": 0}

    with (
        patch("app.services.enrichment_worker.worker.enrich_card", side_effect=fake_enrich_card),
        patch("app.services.enrichment_worker.worker._connectors_in_order", return_value=[]),
        patch("app.services.enrichment_worker.worker.intel_cache.get_cached", return_value=None),
        patch(
            "app.services.enrichment_worker.worker.intel_cache.set_cached",
            side_effect=lambda *a, **k: charged.append(1),
        ),
    ):
        asyncio.run(run_one_batch(db_session, cfg, {}, SpyBreaker(cfg), set(), web_state))

    assert successes == [1]  # claude_ok latched → breaker reset
    assert charged == []  # no web call → budget untouched
    assert web_state["web_calls"] == 0


def test_verified_only_does_not_reset_breaker(db_session):
    """A VERIFIED-via-connector result (no Claude call, claude_ok False, web_calls 0)
    must NOT reset the breaker — only an actual Claude success should."""
    import asyncio
    from datetime import datetime, timezone
    from unittest.mock import patch

    from app.models import MaterialCard
    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig
    from app.services.enrichment_worker.worker import run_one_batch

    now = datetime.now(timezone.utc)
    db_session.add(MaterialCard(normalized_mpn="f0", display_mpn="F0", enrichment_status="unenriched", created_at=now))
    db_session.flush()

    successes: list[int] = []

    class SpyBreaker(EnrichmentCircuitBreaker):
        def record_claude_success(self):
            successes.append(1)
            super().record_claude_success()

    async def fake_enrich_card(card, db, web_meter=None, **kw):
        return MaterialEnrichmentStatus.VERIFIED  # connector hit; no Claude, no web

    cfg = EnrichmentWorkerConfig(batch_size=10, web_daily_cap=80)
    web_state = {"web_calls": 0}

    with (
        patch("app.services.enrichment_worker.worker.enrich_card", side_effect=fake_enrich_card),
        patch("app.services.enrichment_worker.worker._connectors_in_order", return_value=[]),
        patch("app.services.enrichment_worker.worker.intel_cache.get_cached", return_value=None),
        patch("app.services.enrichment_worker.worker.intel_cache.set_cached"),
    ):
        asyncio.run(run_one_batch(db_session, cfg, {}, SpyBreaker(cfg), set(), web_state))

    assert successes == []  # claude_ok False → breaker NOT reset
    assert web_state["web_calls"] == 0


# ---------------------------------------------------------------------------
# run_one_batch — second-pass parametric spec extraction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mpns", "real_status", "miss_status"),
    [
        # ONLY the verified card gets a spec pass; the not_found one is excluded.
        (("sv", "snf"), "VERIFIED", "NOT_FOUND"),
        # Merge-integration guard: oem_sourced (a real category) is INCLUDED;
        # not_catalogued (a terminal miss, like not_found) is EXCLUDED.
        (("soem", "snc"), "OEM_SOURCED", "NOT_CATALOGUED"),
    ],
    ids=["verified_vs_not_found", "oem_sourced_vs_not_catalogued"],
)
def test_run_one_batch_spec_extraction_only_real_categories(db_session, mpns, real_status, miss_status):
    """After core enrichment, run_one_batch triggers a single spec-extraction pass for
    ONLY the cards that landed a real category (verified/web_sourced/ai_inferred/
    oem_sourced) — never the terminal-miss ones (not_found/not_catalogued)."""
    import asyncio
    from datetime import datetime, timedelta, timezone
    from unittest.mock import AsyncMock, patch

    from app.constants import MaterialEnrichmentStatus
    from app.models import MaterialCard
    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig
    from app.services.enrichment_worker.worker import run_one_batch

    now = datetime.now(timezone.utc)
    # Distinct created_at so selection order is deterministic (newest first):
    # the real-category card is processed before the terminal-miss one.
    real_mpn, miss_mpn = mpns
    real_card = MaterialCard(
        normalized_mpn=real_mpn, display_mpn=real_mpn.upper(), enrichment_status="unenriched", created_at=now
    )
    miss_card = MaterialCard(
        normalized_mpn=miss_mpn,
        display_mpn=miss_mpn.upper(),
        enrichment_status="unenriched",
        created_at=now - timedelta(seconds=1),
    )
    db_session.add(real_card)
    db_session.add(miss_card)
    db_session.flush()
    real_id = real_card.id

    returns = [getattr(MaterialEnrichmentStatus, real_status), getattr(MaterialEnrichmentStatus, miss_status)]
    idx = [0]

    async def fake_enrich_card(card, db, **kw):
        st = returns[idx[0]]
        idx[0] += 1
        card.enrichment_status = st
        return st

    cfg = EnrichmentWorkerConfig(batch_size=5, web_daily_cap=80)
    breaker = EnrichmentCircuitBreaker(cfg)
    spec_mock = AsyncMock(return_value={"cards_processed": 1, "specs_written": 2})

    with (
        patch("app.services.enrichment_worker.worker.enrich_card", side_effect=fake_enrich_card),
        patch("app.services.enrichment_worker.worker._connectors_in_order", return_value=[]),
        patch("app.services.enrichment_worker.worker.intel_cache.get_cached", return_value=None),
        patch("app.services.enrichment_worker.worker.intel_cache.set_cached"),
        patch("app.services.spec_enrichment_service.enrich_card_specs", spec_mock),
    ):
        asyncio.run(run_one_batch(db_session, cfg, {}, breaker, set(), {"web_calls": 0}))

    spec_mock.assert_awaited_once()
    passed_ids = spec_mock.await_args.args[0]
    assert passed_ids == [real_id]  # ONLY the real-category card; not the terminal-miss one


def test_run_one_batch_spec_extraction_claude_error_feeds_breaker(db_session):
    """If the spec-extraction pass raises a ClaudeError, it feeds the circuit breaker
    and the batch still commits (no crash)."""
    import asyncio
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock, patch

    from app.constants import MaterialEnrichmentStatus
    from app.models import MaterialCard
    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig
    from app.services.enrichment_worker.worker import run_one_batch
    from app.utils.claude_errors import ClaudeRateLimitError

    now = datetime.now(timezone.utc)
    db_session.add(
        MaterialCard(normalized_mpn="sce", display_mpn="SCE", enrichment_status="unenriched", created_at=now)
    )
    db_session.flush()

    async def fake_enrich_card(card, db, **kw):
        card.enrichment_status = MaterialEnrichmentStatus.VERIFIED
        return MaterialEnrichmentStatus.VERIFIED

    spec_mock = AsyncMock(side_effect=ClaudeRateLimitError("429"))

    cfg = EnrichmentWorkerConfig(batch_size=5, web_daily_cap=80, circuit_breaker_errors=1)
    breaker = EnrichmentCircuitBreaker(cfg)

    with (
        patch("app.services.enrichment_worker.worker.enrich_card", side_effect=fake_enrich_card),
        patch("app.services.enrichment_worker.worker._connectors_in_order", return_value=[]),
        patch("app.services.enrichment_worker.worker.intel_cache.get_cached", return_value=None),
        patch("app.services.enrichment_worker.worker.intel_cache.set_cached"),
        patch("app.services.spec_enrichment_service.enrich_card_specs", spec_mock),
    ):
        counts = asyncio.run(run_one_batch(db_session, cfg, {}, breaker, set(), {"web_calls": 0}))

    spec_mock.assert_awaited_once()
    # A connector-verified hit does not reset the breaker, so the single recorded spec
    # Claude error stands → with circuit_breaker_errors=1 the breaker is open.
    assert breaker.should_stop()
    # Batch still produced its counts (committed cleanly, no crash).
    assert counts.get(MaterialEnrichmentStatus.VERIFIED, 0) == 1


def test_run_one_batch_no_spec_extraction_when_all_not_found(db_session):
    """When every card is not_found, no spec-extraction pass is triggered."""
    import asyncio
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock, patch

    from app.constants import MaterialEnrichmentStatus
    from app.models import MaterialCard
    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig
    from app.services.enrichment_worker.worker import run_one_batch

    now = datetime.now(timezone.utc)
    for i in range(3):
        db_session.add(
            MaterialCard(
                normalized_mpn=f"nfe{i}", display_mpn=f"NFE{i}", enrichment_status="unenriched", created_at=now
            )
        )
    db_session.flush()

    async def fake_enrich_card(card, db, **kw):
        card.enrichment_status = MaterialEnrichmentStatus.NOT_FOUND
        return MaterialEnrichmentStatus.NOT_FOUND

    spec_mock = AsyncMock(return_value={})

    cfg = EnrichmentWorkerConfig(batch_size=5, web_daily_cap=80)
    breaker = EnrichmentCircuitBreaker(cfg)

    with (
        patch("app.services.enrichment_worker.worker.enrich_card", side_effect=fake_enrich_card),
        patch("app.services.enrichment_worker.worker._connectors_in_order", return_value=[]),
        patch("app.services.enrichment_worker.worker.intel_cache.get_cached", return_value=None),
        patch("app.services.enrichment_worker.worker.intel_cache.set_cached"),
        patch("app.services.spec_enrichment_service.enrich_card_specs", spec_mock),
    ):
        asyncio.run(run_one_batch(db_session, cfg, {}, breaker, set(), {"web_calls": 0}))

    spec_mock.assert_not_awaited()
