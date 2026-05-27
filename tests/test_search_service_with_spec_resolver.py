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
