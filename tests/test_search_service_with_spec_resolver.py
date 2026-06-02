"""Integration tests for the zero-hit spec-code fallback inside
``search_requirement()``. Stubs the connectors and the resolver's LLM call; leaves the
resolver wiring itself live so the pending-row writes happen.

Called by: pytest auto-discovery.
Depends on: app/search_service.py, app/services/spec_code_resolver.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app import search_service
from app.config import settings
from app.models import Requirement
from app.models.sourcing import (
    OemSpecCodePending,
    Requisition,
    Sighting,
)
from app.services.spec_code_resolver import ResolverResult


@pytest.fixture
def enable_flag(monkeypatch):
    monkeypatch.setattr(settings, "spec_resolver_enabled", True)
    yield


@pytest.fixture
def known_mpn_requirement(db_session, test_user):
    rset = Requisition(
        name="known-mpn",
        customer_name="Acme",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(rset)
    db_session.flush()
    req = Requirement(
        requisition_id=rset.id,
        primary_mpn="ABC123",
        target_qty=10,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.commit()
    return req


@pytest.fixture
def spec_code_requirement(db_session, test_user):
    rset = Requisition(
        name="spec-code",
        customer_name="Acme",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(rset)
    db_session.flush()
    req = Requirement(
        requisition_id=rset.id,
        primary_mpn="SPREJ",
        target_qty=700,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.commit()
    return req


def _hit_sighting_row(mpn: str = "ABC123") -> dict:
    """Build a connector-style result row with at least the fields needed for
    ``_save_sightings`` to persist it."""
    return {
        "vendor_name": "Mouser",
        "mpn_matched": mpn,
        "mpn": mpn,
        "manufacturer": "TI",
        "qty_available": 100,
        "unit_price": 1.0,
        "currency": "USD",
        "source_type": "mouser",
        "is_authorized": True,
        "confidence": 5,
    }


def _ok_stat(source: str = "mouser") -> dict:
    return {"source": source, "results": 1, "ms": 10, "error": None, "status": "ok"}


async def test_known_mpn_does_not_trigger_resolver(db_session, enable_flag, known_mpn_requirement, monkeypatch):
    """Sync fanout returns ≥1 sighting → resolver never runs."""

    async def fake_fetch_fresh(mpns, db):
        return ([_hit_sighting_row("ABC123")], [_ok_stat()])

    monkeypatch.setattr(search_service, "_fetch_fresh", fake_fetch_fresh)
    resolve_spy = AsyncMock()
    monkeypatch.setattr(
        "app.services.spec_code_resolver.SpecCodeResolver.resolve",
        resolve_spy,
    )

    await search_service.search_requirement(known_mpn_requirement, db_session)
    resolve_spy.assert_not_called()


async def test_zero_hit_triggers_resolver_and_re_fanout(db_session, enable_flag, spec_code_requirement, monkeypatch):
    """Zero sync hits → resolver fires → re-fanout writes tagged sightings."""

    fetch_calls: list[list[str]] = []

    async def fake_fetch_fresh(mpns, db):
        fetch_calls.append(list(mpns))
        if mpns == ["SPREJ"]:
            return ([], [_ok_stat("mouser")])
        # AVL re-fanout returns hits
        return (
            [
                {
                    "vendor_name": "OEMSecrets-Broker",
                    "mpn_matched": mpns[0],
                    "mpn": mpns[0],
                    "manufacturer": "Murata",
                    "qty_available": 1500,
                    "unit_price": 0.42,
                    "currency": "USD",
                    "source_type": "oemsecrets",
                    "is_authorized": False,
                    "confidence": 3,
                }
            ],
            [_ok_stat("oemsecrets")],
        )

    async def fake_resolve(self, spec_code, oem="IBM"):
        return ResolverResult(
            status="pending",
            avl=[
                {
                    "mpn": "GRM188R71H103KA01D",
                    "manufacturer": "Murata",
                    "rank": 1,
                    "notes": None,
                }
            ],
            confidence=0.8,
            citations=[{"url": "https://example.com", "snippet": "..."}],
            source="llm",
        )

    monkeypatch.setattr(search_service, "_fetch_fresh", fake_fetch_fresh)
    monkeypatch.setattr(
        "app.services.spec_code_resolver.SpecCodeResolver.resolve",
        fake_resolve,
    )

    req_id = spec_code_requirement.id
    await search_service.search_requirement(spec_code_requirement, db_session)

    # _fetch_fresh called twice: once for primary, once for AVL.
    assert fetch_calls[0] == ["SPREJ"]
    assert fetch_calls[1] == ["GRM188R71H103KA01D"]

    # Sighting persisted with both lineage columns.
    sightings = db_session.query(Sighting).filter_by(requirement_id=req_id).all()
    assert len(sightings) == 1
    s = sightings[0]
    assert s.resolved_via_spec_code == "SPREJ"
    assert s.source_mpn == "GRM188R71H103KA01D"


async def test_flag_off_skips_resolver_on_zero(db_session, spec_code_requirement, monkeypatch):
    """``spec_resolver_enabled=False`` → resolver never called even on zero hits."""
    monkeypatch.setattr(settings, "spec_resolver_enabled", False)

    async def fake_fetch_fresh(mpns, db):
        return ([], [_ok_stat()])

    monkeypatch.setattr(search_service, "_fetch_fresh", fake_fetch_fresh)
    resolve_spy = AsyncMock()
    monkeypatch.setattr(
        "app.services.spec_code_resolver.SpecCodeResolver.resolve",
        resolve_spy,
    )

    await search_service.search_requirement(spec_code_requirement, db_session)
    resolve_spy.assert_not_called()


async def test_resolver_pending_records_requirement_id(db_session, enable_flag, spec_code_requirement, monkeypatch):
    """When the resolver returns ``pending``, the req_id is appended to the pending
    row's ``used_in_requirement_ids``."""
    req_id = spec_code_requirement.id

    # Pre-seed a pending row so resolve() returns "pending" without an LLM call.
    pending = OemSpecCodePending(
        oem="IBM",
        spec_code="SPREJ",
        proposed_avl=[
            {
                "mpn": "GRM188R71H103KA01D",
                "manufacturer": "Murata",
                "rank": 1,
                "notes": None,
            }
        ],
        llm_confidence=0.8,
        citations=[],
        used_in_requirement_ids=[],
    )
    db_session.add(pending)
    db_session.commit()
    pending_id = pending.id

    async def fake_fetch_fresh(mpns, db):
        # Primary returns zero; AVL also returns zero (we only care about
        # the pending bookkeeping here).
        return ([], [_ok_stat("mouser")])

    monkeypatch.setattr(search_service, "_fetch_fresh", fake_fetch_fresh)

    await search_service.search_requirement(spec_code_requirement, db_session)

    # Re-read via a fresh query to bypass any session caching from the
    # resolver's separate write session.
    db_session.expire_all()
    refreshed = db_session.get(OemSpecCodePending, pending_id)
    assert refreshed is not None
    assert req_id in (refreshed.used_in_requirement_ids or [])


async def test_used_in_requirement_ids_is_idempotent_on_double_search(
    db_session, enable_flag, spec_code_requirement, monkeypatch
):
    """The same requirement searched twice should NOT duplicate its id in
    ``used_in_requirement_ids``.

    Guards the lost-update / read-modify-write fix on the JSONB column.
    """
    req_id = spec_code_requirement.id

    # Pre-seed a pending row so resolve() returns "pending" without an LLM call.
    pending = OemSpecCodePending(
        oem="IBM",
        spec_code="SPREJ",
        proposed_avl=[{"mpn": "GRM188R71H103KA01D", "manufacturer": "Murata", "rank": 1, "notes": None}],
        llm_confidence=0.8,
        citations=[],
        used_in_requirement_ids=[],
    )
    db_session.add(pending)
    db_session.commit()
    pending_id = pending.id

    async def fake_fetch_fresh(mpns, db):
        return ([], [_ok_stat("mouser")])

    monkeypatch.setattr(search_service, "_fetch_fresh", fake_fetch_fresh)

    # Two consecutive searches for the same requirement
    await search_service.search_requirement(spec_code_requirement, db_session)
    await search_service.search_requirement(spec_code_requirement, db_session)

    db_session.expire_all()
    refreshed = db_session.get(OemSpecCodePending, pending_id)
    assert refreshed is not None
    used = refreshed.used_in_requirement_ids or []
    # The req.id must appear at most once — not twice
    assert used.count(req_id) == 1


async def test_avl_cooldown_skips_fetch_within_window(db_session, enable_flag, spec_code_requirement, monkeypatch):
    """AVL MPNs within the 48h cooldown window should NOT trigger _fetch_fresh on the
    AVL re-fanout — the worker async pickup still happens but live connectors aren't re-
    burned."""
    fetch_calls: list[list[str]] = []

    async def fake_fetch_fresh(mpns, db):
        fetch_calls.append(list(mpns))
        return ([], [_ok_stat("mouser")])

    monkeypatch.setattr(search_service, "_fetch_fresh", fake_fetch_fresh)

    async def fake_resolve(self, spec_code, oem="IBM"):
        return ResolverResult(
            status="approved",
            avl=[{"mpn": "GRM188R71H103KA01D", "manufacturer": "Murata", "rank": 1, "notes": None}],
            confidence=1.0,
            source="table",
        )

    monkeypatch.setattr(
        "app.services.spec_code_resolver.SpecCodeResolver.resolve",
        fake_resolve,
    )

    # Force the cooldown partition to claim the AVL MPN is cached (not stale).
    # The primary "SPREJ" path still partitions normally — only override the AVL
    # list to ensure the resolver block sees an all-cached partition.
    real_partition = search_service._mpn_cooldown_partition

    def fake_cooldown_partition(db, mpns, now=None):
        if mpns == ["GRM188R71H103KA01D"]:
            return [], mpns  # to_search=[], cached=mpns → resolver-block skips fetch
        return real_partition(db, mpns, now=now)

    monkeypatch.setattr(search_service, "_mpn_cooldown_partition", fake_cooldown_partition)

    await search_service.search_requirement(spec_code_requirement, db_session)

    # _fetch_fresh called for the primary (SPREJ), but NOT for the AVL MPN.
    assert ["SPREJ"] in fetch_calls
    assert ["GRM188R71H103KA01D"] not in fetch_calls


async def test_avl_refanout_stamps_material_card_so_cooldown_engages(
    db_session, enable_flag, spec_code_requirement, monkeypatch
):
    """Regression: the AVL re-fanout must stamp a MaterialCard.last_searched_at for
    each searched AVL MPN. Without it ``_mpn_cooldown_partition`` always re-returns the
    AVL set as stale and every zero-hit click re-burns connector quota. Uses the REAL
    partition (no monkeypatch) and a zero-hit AVL fanout so the resolve_material_card
    fallback path is exercised."""

    async def fake_fetch_fresh(mpns, db):
        # Both the primary and the AVL fanout return zero hits.
        return ([], [_ok_stat("oemsecrets")])

    async def fake_resolve(self, spec_code, oem="IBM"):
        return ResolverResult(
            status="pending",
            avl=[{"mpn": "GRM188R71H103KA01D", "manufacturer": "Murata", "rank": 1, "notes": None}],
            confidence=0.8,
            citations=[{"url": "https://example.com", "snippet": "..."}],
            source="llm",
        )

    monkeypatch.setattr(search_service, "_fetch_fresh", fake_fetch_fresh)
    monkeypatch.setattr(
        "app.services.spec_code_resolver.SpecCodeResolver.resolve",
        fake_resolve,
    )

    await search_service.search_requirement(spec_code_requirement, db_session)

    from app.models import MaterialCard

    norm = search_service.normalize_mpn_key("GRM188R71H103KA01D")
    card = db_session.query(MaterialCard).filter_by(normalized_mpn=norm).one_or_none()
    assert card is not None, "AVL MPN must get a MaterialCard so the per-MPN cooldown can engage"
    assert card.last_searched_at is not None, "cooldown clock must be stamped on the searched AVL MPN's card"


async def test_avl_fetch_fresh_exception_logged_and_continues(
    db_session, enable_flag, spec_code_requirement, monkeypatch
):
    """If ``_fetch_fresh`` raises during AVL re-fanout, the error is swallowed (logged)
    and worker enqueues still happen — async workers are independent of the live
    connectors."""

    async def fake_fetch_fresh(mpns, db):
        if mpns == ["SPREJ"]:
            return ([], [_ok_stat("mouser")])
        raise RuntimeError("AVL fanout boom")

    monkeypatch.setattr(search_service, "_fetch_fresh", fake_fetch_fresh)

    async def fake_resolve(self, spec_code, oem="IBM"):
        return ResolverResult(
            status="approved",
            avl=[{"mpn": "GRM188R71H103KA01D", "manufacturer": "Murata", "rank": 1, "notes": None}],
            confidence=1.0,
            source="table",
        )

    monkeypatch.setattr(
        "app.services.spec_code_resolver.SpecCodeResolver.resolve",
        fake_resolve,
    )

    enqueue_calls: list[tuple[str, str | None]] = []

    def fake_enqueue_ics(req_id, db, override_mpn=None, resolved_via_spec_code=None):
        enqueue_calls.append(("ics", override_mpn))
        return None

    def fake_enqueue_nc(req_id, db, override_mpn=None, resolved_via_spec_code=None):
        enqueue_calls.append(("nc", override_mpn))
        return None

    monkeypatch.setattr(search_service, "enqueue_for_ics_search", fake_enqueue_ics)
    monkeypatch.setattr(search_service, "enqueue_for_nc_search", fake_enqueue_nc)

    # Should NOT raise even though _fetch_fresh crashed on the AVL fanout.
    await search_service.search_requirement(spec_code_requirement, db_session)

    # AVL enqueues still happened despite the connector failure.
    assert ("ics", "GRM188R71H103KA01D") in enqueue_calls
    assert ("nc", "GRM188R71H103KA01D") in enqueue_calls


async def test_avl_connector_crash_keeps_workers_and_does_not_abort_session(
    db_session, enable_flag, spec_code_requirement, monkeypatch
):
    """If the AVL re-fanout connectors crash, the surrounding write session must
    stay usable: the requirement's last_searched_at write committed by the
    primary path survives, and the async workers still enqueue.

    This pins the caller-owned-transaction / savepoint contract — a connector
    blow-up in the resolver block can't poison the outer transaction.
    """

    async def fake_fetch_fresh(mpns, db):
        if mpns == ["SPREJ"]:
            return ([], [_ok_stat("mouser")])
        raise RuntimeError("AVL connectors down")

    monkeypatch.setattr(search_service, "_fetch_fresh", fake_fetch_fresh)

    async def fake_resolve(self, spec_code, oem="IBM"):
        return ResolverResult(
            status="approved",
            avl=[{"mpn": "GRM188R71H103KA01D", "manufacturer": "Murata", "rank": 1, "notes": None}],
            confidence=1.0,
            source="table",
        )

    monkeypatch.setattr(
        "app.services.spec_code_resolver.SpecCodeResolver.resolve",
        fake_resolve,
    )

    enqueued: list[str] = []

    def fake_enqueue_ics(req_id, db, override_mpn=None, resolved_via_spec_code=None):
        enqueued.append(f"ics:{override_mpn}")
        return None

    def fake_enqueue_nc(req_id, db, override_mpn=None, resolved_via_spec_code=None):
        enqueued.append(f"nc:{override_mpn}")
        return None

    monkeypatch.setattr(search_service, "enqueue_for_ics_search", fake_enqueue_ics)
    monkeypatch.setattr(search_service, "enqueue_for_nc_search", fake_enqueue_nc)

    req_id = spec_code_requirement.id
    # Must not raise despite the AVL connector crash.
    await search_service.search_requirement(spec_code_requirement, db_session)

    # AVL workers still enqueued for the resolved MPN.
    assert "ics:GRM188R71H103KA01D" in enqueued
    assert "nc:GRM188R71H103KA01D" in enqueued

    # The outer transaction is intact and committed: the requirement is
    # readable and its searched timestamp was persisted by the primary path.
    db_session.expire_all()
    refreshed = db_session.get(Requirement, req_id)
    assert refreshed is not None
    assert refreshed.last_searched_at is not None


async def test_non_ibm_oem_hint_is_passed_to_resolver(db_session, enable_flag, test_user, monkeypatch):
    """A requirement with a non-IBM ``oem_hint`` must forward that OEM to the resolver
    rather than defaulting to IBM."""
    rset = Requisition(
        name="dell-spec",
        customer_name="Acme",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(rset)
    db_session.flush()
    req = Requirement(
        requisition_id=rset.id,
        primary_mpn="DELLSPEC1",
        oem_hint="DELL",
        target_qty=5,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.commit()

    async def fake_fetch_fresh(mpns, db):
        return ([], [_ok_stat("mouser")])

    monkeypatch.setattr(search_service, "_fetch_fresh", fake_fetch_fresh)

    captured: dict = {}

    async def fake_resolve(self, spec_code, oem="IBM"):
        captured["spec_code"] = spec_code
        captured["oem"] = oem
        return ResolverResult(status="unresolved")

    monkeypatch.setattr(
        "app.services.spec_code_resolver.SpecCodeResolver.resolve",
        fake_resolve,
    )

    await search_service.search_requirement(req, db_session)

    assert captured["oem"] == "DELL"
    assert captured["spec_code"] == "DELLSPEC1"
