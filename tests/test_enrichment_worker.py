"""Tests for the enrichment worker status model (Task 7) and worker config + circuit
breaker (Task 8).

The singleton row is seeded by the Alembic migration in Postgres. In SQLite tests,
create_all builds the table but does not run migrations, so the row may be absent — the
test tolerates None (row is None or id==1).
"""


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
    assert "id" in cols
    assert "is_running" in cols
    assert "last_heartbeat" in cols
    assert "last_enriched_at" in cols
    assert "enriched_today" in cols
    assert "web_sourced_today" in cols
    assert "ai_inferred_today" in cols
    assert "not_found_today" in cols
    assert "circuit_breaker_open" in cols
    assert "circuit_breaker_reason" in cols
    assert "daily_stats_json" in cols
    assert "updated_at" in cols
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


def test_select_batch_ordering(db_session):
    """Cards with higher search_count should appear first."""
    from datetime import datetime, timezone

    from app.models import MaterialCard
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig
    from app.services.enrichment_worker.worker import select_batch

    now = datetime.now(timezone.utc)
    for mpn, sc in [("low_sc", 1), ("high_sc", 99), ("mid_sc", 10)]:
        db_session.add(
            MaterialCard(
                normalized_mpn=mpn,
                display_mpn=mpn.upper(),
                enrichment_status="unenriched",
                search_count=sc,
                created_at=now,
            )
        )
    db_session.flush()

    cfg = EnrichmentWorkerConfig(batch_size=5)
    results = select_batch(db_session, cfg)
    search_counts = [c.search_count for c in results]
    assert search_counts == sorted(search_counts, reverse=True)


def test_select_batch_freshness_tiebreaker(db_session):
    """Among equal demand (search_count=0), the most-recently-created card wins.

    This is the fast-lane guarantee: a just-added part heads the next batch.
    """
    from datetime import datetime, timedelta, timezone

    from app.models import MaterialCard
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig
    from app.services.enrichment_worker.worker import select_batch

    now = datetime.now(timezone.utc)
    for mpn, created in [
        ("oldest", now - timedelta(hours=2)),
        ("middle", now - timedelta(hours=1)),
        ("newest", now),
    ]:
        db_session.add(
            MaterialCard(
                normalized_mpn=mpn,
                display_mpn=mpn.upper(),
                enrichment_status="unenriched",
                search_count=0,
                created_at=created,
            )
        )
    db_session.flush()

    cfg = EnrichmentWorkerConfig(batch_size=5)
    order = [c.normalized_mpn for c in select_batch(db_session, cfg)]
    assert order == ["newest", "middle", "oldest"]


def test_select_batch_demand_beats_freshness(db_session):
    """Demand is primary: a high-search_count old card outranks a brand-new card.

    Freshness only breaks ties; it never overrides demand.
    """
    from datetime import datetime, timedelta, timezone

    from app.models import MaterialCard
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig
    from app.services.enrichment_worker.worker import select_batch

    now = datetime.now(timezone.utc)
    db_session.add(
        MaterialCard(
            normalized_mpn="old_hot",
            display_mpn="OLD_HOT",
            enrichment_status="unenriched",
            search_count=50,
            created_at=now - timedelta(days=7),
        )
    )
    db_session.add(
        MaterialCard(
            normalized_mpn="new_cold",
            display_mpn="NEW_COLD",
            enrichment_status="unenriched",
            search_count=0,
            created_at=now,
        )
    )
    db_session.flush()

    cfg = EnrichmentWorkerConfig(batch_size=5)
    order = [c.normalized_mpn for c in select_batch(db_session, cfg)]
    assert order[0] == "old_hot"


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


def test_run_one_batch_web_cap_disables_web_tier(db_session, monkeypatch):
    """When web daily cap is reached, 'web_search' is added to disabled set."""
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
            normalized_mpn="testpart",
            display_mpn="TESTPART",
            enrichment_status="unenriched",
            created_at=now,
        )
    )
    db_session.flush()

    captured_disabled: list[set] = []

    async def fake_enrich_card(card, db, disabled=None, **kw):
        captured_disabled.append(set(disabled) if disabled else set())
        return "not_found"

    cfg = EnrichmentWorkerConfig(batch_size=5, web_daily_cap=10)
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
            return_value={"count": 10},  # at cap
        ),
        patch("app.services.enrichment_worker.worker.intel_cache.set_cached"),
    ):
        asyncio.run(run_one_batch(db_session, cfg, {}, breaker))

    assert len(captured_disabled) == 1
    assert "web_search" in captured_disabled[0]


def test_run_one_batch_below_web_cap_does_not_disable(db_session):
    """When below web daily cap, 'web_search' is NOT in the disabled set."""
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
            normalized_mpn="testpart2",
            display_mpn="TESTPART2",
            enrichment_status="unenriched",
            created_at=now,
        )
    )
    db_session.flush()

    captured_disabled: list[set] = []

    async def fake_enrich_card(card, db, disabled=None, **kw):
        captured_disabled.append(set(disabled) if disabled else set())
        return "not_found"

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
            return_value={"count": 5},  # well below cap
        ),
        patch("app.services.enrichment_worker.worker.intel_cache.set_cached"),
    ):
        asyncio.run(run_one_batch(db_session, cfg, {}, breaker))

    assert len(captured_disabled) == 1
    assert "web_search" not in captured_disabled[0]


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
            web_meter["web_calls"] = web_meter.get("web_calls", 0) + 1
            web_meter["claude_ok"] = True
        return st

    set_counts: list[int] = []

    def fake_set(key, data, **kw):
        set_counts.append(data["count"])

    cfg = EnrichmentWorkerConfig(batch_size=10, web_daily_cap=80)
    breaker = EnrichmentCircuitBreaker(cfg)
    web_state = {"web_calls": 0}

    with (
        patch("app.services.enrichment_worker.worker.enrich_card", side_effect=fake_enrich_card),
        patch("app.services.enrichment_worker.worker._connectors_in_order", return_value=[]),
        patch("app.services.enrichment_worker.worker.intel_cache.get_cached", return_value=None),
        patch("app.services.enrichment_worker.worker.intel_cache.set_cached", side_effect=fake_set),
    ):
        asyncio.run(run_one_batch(db_session, cfg, {}, breaker, set(), web_state))

    # 3 billable (web_sourced, ai_inferred, not_found); verified does not charge.
    assert set_counts == [1, 2, 3]
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
            web_meter["web_calls"] = web_meter.get("web_calls", 0) + 1
            web_meter["claude_ok"] = True
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
