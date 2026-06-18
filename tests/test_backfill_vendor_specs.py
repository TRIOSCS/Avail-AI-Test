"""tests/test_backfill_vendor_specs.py — the paced vendor-API parametric-spec backfill.

Drives ``app.management.backfill_vendor_specs.run``: select uncategorized cards
demand-first (sourced_qty_90d DESC), search Mouser for each within a per-day call cap,
and enrich via ``enrich_card_from_mouser`` (category + facets through the F1 ladder).
Dry-run writes nothing; ``--apply`` writes + bills a date-keyed call counter.

Depends on: conftest.py (db_session), seed_commodity_schemas, MaterialCard +
MaterialSpecFacet, the desc grammar (capacitors registered). No live network.
"""

import asyncio
from unittest.mock import patch

from sqlalchemy.orm import Session

from app.management import backfill_vendor_specs as cli
from app.models import MaterialCard, MaterialSpecFacet
from app.services.commodity_registry import seed_commodity_schemas

# Canned Mouser capacitor result (rich description, category string, no structured fields).
_CAP_RESULT = {
    "manufacturer": "Murata",
    "category": "Multilayer Ceramic Capacitors MLCC - SMD/SMT",
    "description": "Multilayer Ceramic Capacitors MLCC - SMD/SMT 16V 0.1uF X7R 0402 10%",
    "source_type": "mouser",
}


class _NoCloseSession:
    """Hand the test session to run() while neutering its finally-close."""

    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        if name == "close":
            return lambda: None
        return getattr(self._db, name)


class _FakeMouser:
    """A stand-in Mouser connector recording the MPNs searched, in order."""

    source_name = "mouser"

    def __init__(self, results_by_mpn: dict | None = None, default=None):
        self.results_by_mpn = results_by_mpn or {}
        self.default = default if default is not None else [_CAP_RESULT]
        self.searched: list[str] = []

    async def search(self, mpn: str):
        self.searched.append(mpn)
        return self.results_by_mpn.get(mpn, self.default)


def _card(db: Session, mpn: str, sourced_qty_90d=None, category=None) -> MaterialCard:
    card = MaterialCard(
        normalized_mpn=mpn.lower(),
        display_mpn=mpn,
        category=category,
        sourced_qty_90d=sourced_qty_90d,
    )
    db.add(card)
    db.flush()
    return card


def _run_cli(db_session, connector, *, apply=False, limit=None, daily_cap=800, cache_counts=None):
    """Drive run() with the connector, counter cache, session and sleep mocked."""
    counters = dict(cache_counts or {})

    def get_count(key):
        return counters.get(key, 0)

    def incr_count(key, amount=1, ttl_days=1.0):
        counters[key] = counters.get(key, 0) + amount
        return counters[key]

    with (
        patch("app.database.SessionLocal", return_value=_NoCloseSession(db_session)),
        patch("app.management.backfill_vendor_specs._build_connectors", return_value=([connector], {}, set())),
        patch("app.management.backfill_vendor_specs.intel_cache.get_count", side_effect=get_count),
        patch("app.management.backfill_vendor_specs.intel_cache.incr_count", side_effect=incr_count),
    ):
        summary = asyncio.run(run_with_args(apply=apply, limit=limit, daily_cap=daily_cap))
    return summary, counters


async def run_with_args(*, apply, limit, daily_cap, source="mouser"):
    return await cli.run(source=source, limit=limit, daily_cap=daily_cap, apply=apply)


def _facets(db: Session, card_id: int) -> dict:
    rows = db.query(MaterialSpecFacet).filter_by(material_card_id=card_id).all()
    return {r.spec_key: r for r in rows}


def test_dry_run_writes_nothing_but_reports_counts(db_session: Session):
    seed_commodity_schemas(db_session)
    c1 = _card(db_session, "CAP100", sourced_qty_90d=100)
    c2 = _card(db_session, "CAP50", sourced_qty_90d=50)
    conn = _FakeMouser()

    summary, counters = _run_cli(db_session, conn, apply=False)
    db_session.commit()

    # Dry-run: no search, no writes, no counter billing.
    assert conn.searched == []
    assert counters == {}
    db_session.refresh(c1)
    db_session.refresh(c2)
    assert c1.category is None and c2.category is None
    assert summary["seen"] == 2
    assert summary["would_enrich"] == 2
    assert summary["enriched"] == 0
    assert summary["calls"] == 0


def test_apply_enriches_and_bills_counter(db_session: Session):
    seed_commodity_schemas(db_session)
    card = _card(db_session, "CAP100", sourced_qty_90d=100)
    conn = _FakeMouser()

    summary, counters = _run_cli(db_session, conn, apply=True)
    db_session.commit()

    db_session.refresh(card)
    assert card.category == "capacitors"
    facets = _facets(db_session, card.id)
    assert "capacitance" in facets and "package" in facets

    # Exactly one date-keyed Mouser call was billed.
    assert conn.searched == ["CAP100"]
    assert sum(counters.values()) == 1
    assert any(k.startswith("vendor_api:mouser:calls:") for k in counters)

    assert summary["enriched"] == 1
    assert summary["categorized"] == 1
    assert summary["specs_written"] == len(facets)
    assert summary["calls"] == 1


def test_demand_ordered_processing(db_session: Session):
    seed_commodity_schemas(db_session)
    _card(db_session, "LOW", sourced_qty_90d=50)
    _card(db_session, "HIGH", sourced_qty_90d=100)
    _card(db_session, "NULLQ", sourced_qty_90d=None)
    conn = _FakeMouser()

    _run_cli(db_session, conn, apply=True)
    db_session.commit()

    # Demand-first: 100 before 50, NULL last.
    assert conn.searched == ["HIGH", "LOW", "NULLQ"]


def test_daily_cap_stops_at_cap(db_session: Session):
    seed_commodity_schemas(db_session)
    _card(db_session, "HIGH", sourced_qty_90d=100)
    _card(db_session, "MID", sourced_qty_90d=50)
    _card(db_session, "LOW", sourced_qty_90d=10)
    conn = _FakeMouser()

    summary, counters = _run_cli(db_session, conn, apply=True, daily_cap=2)
    db_session.commit()

    # Cap is checked BEFORE each call → exactly 2 searches, then stop.
    assert conn.searched == ["HIGH", "MID"]
    assert summary["calls"] == 2
    assert sum(counters.values()) == 2


def test_pre_existing_counter_respected(db_session: Session):
    # A cap already (nearly) exhausted by the worker leaves little headroom.
    seed_commodity_schemas(db_session)
    _card(db_session, "HIGH", sourced_qty_90d=100)
    _card(db_session, "MID", sourced_qty_90d=50)
    conn = _FakeMouser()

    from datetime import datetime, timezone

    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    summary, counters = _run_cli(
        db_session,
        conn,
        apply=True,
        daily_cap=3,
        cache_counts={f"vendor_api:mouser:calls:{date}": 2},
    )
    db_session.commit()

    # Only 1 of the 3-cap remained → 1 call.
    assert len(conn.searched) == 1
    assert summary["calls"] == 1


def test_limit_caps_candidate_selection(db_session: Session):
    seed_commodity_schemas(db_session)
    _card(db_session, "HIGH", sourced_qty_90d=100)
    _card(db_session, "MID", sourced_qty_90d=50)
    _card(db_session, "LOW", sourced_qty_90d=10)
    conn = _FakeMouser()

    summary, _ = _run_cli(db_session, conn, apply=True, limit=2)
    db_session.commit()

    assert conn.searched == ["HIGH", "MID"]
    assert summary["seen"] == 2


def test_no_limit_run_bounds_materialization_to_daily_cap(db_session: Session):
    # MEDIUM-6: a no-limit --apply run must not materialize the whole uncategorized
    # backlog (~737k rows) — at most daily_cap cards can be processed, so selection is
    # bounded to daily_cap when --limit is None. With 5 cards and a 3-cap, only 3 are
    # selected (seen), demand-first.
    seed_commodity_schemas(db_session)
    for i, q in enumerate([100, 90, 80, 70, 60]):
        _card(db_session, f"CAP{i}", sourced_qty_90d=q)
    conn = _FakeMouser()

    summary, _ = _run_cli(db_session, conn, apply=True, limit=None, daily_cap=3)
    db_session.commit()

    assert summary["seen"] == 3  # bounded to daily_cap, not all 5
    assert conn.searched == ["CAP0", "CAP1", "CAP2"]  # demand-first


def test_dry_run_candidate_logging_is_bounded(db_session: Session):
    # MEDIUM-6: the dry run must not log every one of ~737k candidate rows. With more
    # candidates than the log cap, the per-candidate "would enrich" lines are capped while
    # the would_enrich COUNT stays full (the dominant cost — full row materialization — is
    # bounded by the daily_cap selection limit covered above).
    from loguru import logger

    seed_commodity_schemas(db_session)
    for i in range(cli._DRY_RUN_LOG_LIMIT + 5):
        _card(db_session, f"DRY{i}", sourced_qty_90d=1000 - i)
    conn = _FakeMouser()

    lines: list[str] = []
    handler_id = logger.add(lines.append, level="INFO", format="{message}")
    try:
        summary, _ = _run_cli(db_session, conn, apply=False, daily_cap=10_000)
    finally:
        logger.remove(handler_id)

    would_enrich_lines = [ln for ln in lines if "would enrich" in ln]
    assert len(would_enrich_lines) == cli._DRY_RUN_LOG_LIMIT
    assert summary["would_enrich"] == cli._DRY_RUN_LOG_LIMIT + 5  # the COUNT is still full


def test_already_categorized_cards_skipped(db_session: Session):
    # v1 needs-enrichment predicate is category IS NULL.
    seed_commodity_schemas(db_session)
    _card(db_session, "DONE", sourced_qty_90d=100, category="capacitors")
    _card(db_session, "TODO", sourced_qty_90d=50)
    conn = _FakeMouser()

    summary, _ = _run_cli(db_session, conn, apply=True)
    db_session.commit()

    assert conn.searched == ["TODO"]
    assert summary["seen"] == 1


def test_no_hit_does_not_enrich(db_session: Session):
    seed_commodity_schemas(db_session)
    card = _card(db_session, "NOHIT", sourced_qty_90d=100)
    conn = _FakeMouser(default=[])  # Mouser returns no results

    summary, counters = _run_cli(db_session, conn, apply=True)
    db_session.commit()

    db_session.refresh(card)
    assert card.category is None
    assert summary["calls"] == 1  # the call was still made (and billed)
    assert summary["enriched"] == 0


def test_enrich_failure_is_isolated_and_run_continues(db_session: Session):
    # HIGH-5: enrich_card_from_mouser can re-raise (a DB flush inside the SAVEPOINT). One
    # bad card must NOT abort the loop or discard the chunk's prior committed work. The
    # failure is contained to that card (its category mutation is rolled back), counted in
    # a `failed` tally, and the run completes enriching the remaining cards.
    seed_commodity_schemas(db_session)
    _card(db_session, "GOODA", sourced_qty_90d=100)
    _card(db_session, "BAD", sourced_qty_90d=50)
    _card(db_session, "GOODB", sourced_qty_90d=10)
    conn = _FakeMouser()

    real_enrich = cli.enrich_card_from_mouser

    def flaky(db, card, results):
        if card.display_mpn == "BAD":
            raise RuntimeError("boom")
        return real_enrich(db, card, results)

    with patch("app.management.backfill_vendor_specs.enrich_card_from_mouser", side_effect=flaky):
        summary, _ = _run_cli(db_session, conn, apply=True)
    db_session.commit()

    # All three were searched (the failure did not abort the loop).
    assert conn.searched == ["GOODA", "BAD", "GOODB"]
    # The two good cards enriched; the bad one is tallied and left uncategorized.
    assert summary["failed"] == 1
    assert summary["enriched"] == 2
    good_a = db_session.query(MaterialCard).filter_by(normalized_mpn="gooda").one()
    good_b = db_session.query(MaterialCard).filter_by(normalized_mpn="goodb").one()
    bad = db_session.query(MaterialCard).filter_by(normalized_mpn="bad").one()
    assert good_a.category == "capacitors"
    assert good_b.category == "capacitors"
    assert bad.category is None  # the failed card's category mutation was rolled back


def test_two_run_idempotency(db_session: Session):
    # LOW-10(1): a second apply run sees the now-categorized card as no longer a
    # candidate (predicate is category IS NULL), so pass 2 has seen==0 / enriched==0.
    seed_commodity_schemas(db_session)
    _card(db_session, "CAP100", sourced_qty_90d=100)
    conn = _FakeMouser()

    summary1, _ = _run_cli(db_session, conn, apply=True)
    db_session.commit()
    assert summary1["enriched"] == 1

    conn2 = _FakeMouser()
    summary2, _ = _run_cli(db_session, conn2, apply=True)
    db_session.commit()
    assert summary2["seen"] == 0
    assert summary2["enriched"] == 0
    assert conn2.searched == []  # nothing left to enrich


def test_search_failure_continues_and_reports_zero_for_card(db_session: Session):
    # LOW-10(2): a connector.search() that raises is caught — the run continues and that
    # card simply does not enrich (enriched stays 0 for it).
    seed_commodity_schemas(db_session)
    card = _card(db_session, "BOOM", sourced_qty_90d=100)

    class _RaisingMouser:
        source_name = "mouser"

        def __init__(self):
            self.searched: list[str] = []

        async def search(self, mpn):
            self.searched.append(mpn)
            raise RuntimeError("upstream 500")

    conn = _RaisingMouser()
    summary, counters = _run_cli(db_session, conn, apply=True)
    db_session.commit()

    db_session.refresh(card)
    assert conn.searched == ["BOOM"]
    assert card.category is None
    assert summary["enriched"] == 0
    assert summary["calls"] == 1  # the call was billed before it raised


def test_no_connector_logs_error_and_returns_zero_summary(db_session: Session):
    # LOW-10(3): with no Mouser connector configured the run logs an error and returns a
    # zero summary without searching anything.
    seed_commodity_schemas(db_session)
    _card(db_session, "CAP100", sourced_qty_90d=100)
    counters: dict = {}

    with (
        patch("app.database.SessionLocal", return_value=_NoCloseSession(db_session)),
        patch("app.management.backfill_vendor_specs._build_connectors", return_value=([], {}, set())),
        patch("app.management.backfill_vendor_specs.intel_cache.get_count", side_effect=lambda k: counters.get(k, 0)),
        patch("app.management.backfill_vendor_specs.intel_cache.incr_count"),
    ):
        summary = asyncio.run(cli.run(source="mouser", limit=None, daily_cap=800, apply=True))
    db_session.commit()

    assert summary["enriched"] == 0
    assert summary["calls"] == 0
    assert counters == {}  # nothing billed — no connector to call
