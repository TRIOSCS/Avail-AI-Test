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
