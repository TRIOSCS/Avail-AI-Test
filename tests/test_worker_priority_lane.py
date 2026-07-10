"""tests/test_worker_priority_lane.py — enrichment-worker priority lane + demand order.

Covers: select_batch's ORDER BY (enrich_requested_at ASC NULLS LAST — stamped cards
beat the whole backlog, FIFO among themselves; then status=unenriched DESC; then the
demand-telemetry tiebreak sourced_qty_90d DESC NULLS LAST, last_sourced_at DESC NULLS
LAST, id — migration 105, plan 1.4) and run_one_batch clearing the stamp on EVERY batch
card (including not_found outcomes) before the first await.
Depends on: conftest.py (db_session), enrichment worker config/breaker.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from app.constants import MaterialEnrichmentStatus
from app.models import MaterialCard
from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
from app.services.enrichment_worker.config import EnrichmentWorkerConfig
from app.services.enrichment_worker.worker import run_one_batch, select_batch

NOW = datetime.now(UTC)


def _mk(db, mpn, *, sc=0, requested=None, status="unenriched", created=NOW, enriched=None, qty=None, sourced=None):
    card = MaterialCard(
        normalized_mpn=mpn,
        display_mpn=mpn.upper(),
        enrichment_status=status,
        search_count=sc,
        created_at=created,
        enriched_at=enriched,
        enrich_requested_at=requested,
        sourced_qty_90d=qty,
        last_sourced_at=sourced,
    )
    db.add(card)
    return card


def test_stamped_card_beats_high_demand_backlog(db_session):
    """A freshly stamped single-add card jumps a much higher-search_count backlog."""
    _mk(db_session, "backlog_hot", sc=999, created=NOW - timedelta(days=1))
    _mk(db_session, "user_added", sc=0, requested=NOW)
    db_session.flush()

    cfg = EnrichmentWorkerConfig(batch_size=1)
    picked = [c.normalized_mpn for c in select_batch(db_session, cfg)]
    assert picked == ["user_added"]


def test_fifo_among_stamped_cards(db_session):
    """Stamped cards drain oldest-stamp-first — no user's add starves another's."""
    _mk(db_session, "second", sc=500, requested=NOW)
    _mk(db_session, "first", sc=0, requested=NOW - timedelta(minutes=5))
    _mk(db_session, "unstamped", sc=999)
    db_session.flush()

    cfg = EnrichmentWorkerConfig(batch_size=3)
    picked = [c.normalized_mpn for c in select_batch(db_session, cfg)]
    assert picked == ["first", "second", "unstamped"]


def test_unstamped_ordering_unchanged_behind_lane(db_session):
    """Behind the lane the existing order holds: unenriched before re-checks, then
    demand, then freshness."""
    _mk(db_session, "stamped", requested=NOW)
    _mk(db_session, "old_unenriched", created=NOW - timedelta(days=60))
    _mk(
        db_session,
        "nf_recheck",
        status="not_found",
        created=NOW - timedelta(days=1),
        enriched=NOW - timedelta(hours=30),
    )
    db_session.flush()

    cfg = EnrichmentWorkerConfig(batch_size=3, not_found_retry_hours=22)
    picked = [c.normalized_mpn for c in select_batch(db_session, cfg)]
    assert picked == ["stamped", "old_unenriched", "nf_recheck"]


def test_demand_telemetry_orders_unstamped_backlog(db_session):
    """Among equal-status unstamped cards, higher sourced_qty_90d wins; NULL demand
    drains LAST (NULLS LAST) — migration 105, plan 1.4."""
    _mk(db_session, "no_demand", qty=None)
    _mk(db_session, "low_demand", qty=3)
    _mk(db_session, "high_demand", qty=500)
    db_session.flush()

    cfg = EnrichmentWorkerConfig(batch_size=3)
    picked = [c.normalized_mpn for c in select_batch(db_session, cfg)]
    assert picked == ["high_demand", "low_demand", "no_demand"]


def test_last_sourced_at_breaks_equal_demand(db_session):
    """Equal sourced_qty_90d → most-recently-sourced first (last_sourced_at DESC NULLS
    LAST); a card with no recency drains after one that has it."""
    older = NOW - timedelta(days=30)
    newer = NOW - timedelta(days=1)
    _mk(db_session, "stale", qty=10, sourced=older)
    _mk(db_session, "fresh", qty=10, sourced=newer)
    _mk(db_session, "no_ts", qty=10, sourced=None)
    db_session.flush()

    cfg = EnrichmentWorkerConfig(batch_size=3)
    picked = [c.normalized_mpn for c in select_batch(db_session, cfg)]
    assert picked == ["fresh", "stale", "no_ts"]


def test_priority_lane_beats_demand_then_status_then_demand(db_session):
    """Full ORDER BY precedence: a stamped zero-demand card still beats a high-demand
    unstamped one (lane first); among unstamped, unenriched beats a not_found re-check
    even when the re-check has higher demand (status before demand)."""
    _mk(db_session, "stamped_zero", qty=0, requested=NOW)
    _mk(
        db_session,
        "nf_high_demand",
        status="not_found",
        qty=999,
        enriched=NOW - timedelta(hours=30),
    )
    _mk(db_session, "unenriched_mid", qty=50)
    db_session.flush()

    cfg = EnrichmentWorkerConfig(batch_size=3, not_found_retry_hours=22)
    picked = [c.normalized_mpn for c in select_batch(db_session, cfg)]
    assert picked == ["stamped_zero", "unenriched_mid", "nf_high_demand"]


def test_run_one_batch_clears_stamp_on_every_card_incl_not_found(db_session):
    """run_one_batch clears enrich_requested_at on EVERY batch card — including ones
    that finish terminal not_found — so a dead MPN cannot pin the priority lane."""
    ok_card = _mk(db_session, "lane_ok", requested=NOW - timedelta(minutes=2))
    nf_card = _mk(db_session, "lane_nf", requested=NOW - timedelta(minutes=1))
    db_session.flush()

    outcomes = {
        "lane_ok": MaterialEnrichmentStatus.VERIFIED,
        "lane_nf": MaterialEnrichmentStatus.NOT_FOUND,
    }

    async def fake_enrich_card(card, db, **kw):
        status = outcomes[card.normalized_mpn]
        card.enrichment_status = status
        return status

    cfg = EnrichmentWorkerConfig(batch_size=5, web_daily_cap=80)
    breaker = EnrichmentCircuitBreaker(cfg)

    with (
        patch("app.services.enrichment_worker.worker.enrich_card", side_effect=fake_enrich_card),
        patch("app.services.enrichment_worker.worker._connectors_in_order", return_value=[]),
        patch("app.services.enrichment_worker.worker.intel_cache.get_cached", return_value=None),
        patch("app.services.enrichment_worker.worker.intel_cache.set_cached"),
    ):
        counts = asyncio.run(run_one_batch(db_session, cfg, {}, breaker))

    assert counts  # both cards processed
    assert ok_card.enrich_requested_at is None
    assert nf_card.enrich_requested_at is None
    # The not_found card is now outside the lane AND inside its retry backoff window —
    # the next batch must not re-select it ahead of anything.
    db_session.flush()
    picked = {c.normalized_mpn for c in select_batch(db_session, cfg)}
    assert "lane_nf" not in picked


def test_run_one_batch_clears_stamp_even_when_enrich_card_raises(db_session):
    """A poison-pill stamped card (enrich_card raises) is quarantined AND unstamped — it
    cannot spin at the front of the lane."""

    poison = _mk(db_session, "lane_poison", requested=NOW)
    db_session.flush()

    async def exploding_enrich_card(card, db, **kw):
        raise RuntimeError("boom")

    cfg = EnrichmentWorkerConfig(batch_size=5, web_daily_cap=80)
    breaker = EnrichmentCircuitBreaker(cfg)

    with (
        patch("app.services.enrichment_worker.worker.enrich_card", side_effect=exploding_enrich_card),
        patch("app.services.enrichment_worker.worker._connectors_in_order", return_value=[]),
        patch("app.services.enrichment_worker.worker.intel_cache.get_cached", return_value=None),
        patch("app.services.enrichment_worker.worker.intel_cache.set_cached"),
    ):
        asyncio.run(run_one_batch(db_session, cfg, {}, breaker))

    assert poison.enrich_requested_at is None
    assert poison.enrichment_status == MaterialEnrichmentStatus.NOT_FOUND
