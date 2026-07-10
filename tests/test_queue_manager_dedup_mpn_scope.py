"""Regression test: cross-requirement dedup must clone only the deduped MPN's sightings.

A requirement can own multiple queue rows (primary + resolved-AVL MPNs), so its
sightings span several normalized MPNs. When requirement B dedups against a recent
completed search of requirement A for MPN X, only A's X-MPN sightings may be cloned
onto B — never A's other MPNs' sightings (which B never requested).

Called by: pytest auto-discovery.
Depends on: app/services/search_worker_base/queue_manager.py (QueueManager dedup).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models import IcsSearchQueue, Requirement, Sighting
from app.models.intelligence import MaterialCard
from app.models.sourcing import Requisition
from app.services.ics_worker.queue_manager import enqueue_for_ics_search

_DEDUP_MPN = "SPREJ"
_OTHER_MPN = "OTHERMPN"
_SOURCE_TYPE = "icsource"


@pytest.fixture
def requisition(db_session, test_user):
    rset = Requisition(
        name="dedup-req",
        customer_name="Acme",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(UTC),
    )
    db_session.add(rset)
    db_session.flush()
    return rset


def _make_sighting(requirement_id: int, normalized_mpn: str, vendor: str) -> Sighting:
    return Sighting(
        requirement_id=requirement_id,
        vendor_name=vendor,
        mpn_matched=normalized_mpn,
        normalized_mpn=normalized_mpn,
        source_type=_SOURCE_TYPE,
        qty_available=10,
        created_at=datetime.now(UTC),
    )


def test_dedup_clones_only_matching_mpn_sightings(db_session, requisition):
    """Source requirement A has sightings for TWO MPNs; deduping requirement B against
    A's completed X-MPN search must clone only the X-MPN sightings."""
    # Source requirement A — searched for two MPNs, holds sightings for both.
    req_a = Requirement(
        requisition_id=requisition.id,
        primary_mpn=_DEDUP_MPN,
        target_qty=100,
        created_at=datetime.now(UTC),
    )
    db_session.add(req_a)
    db_session.flush()

    # A completed ICS search of the deduped MPN, inside the dedup window.
    completed = IcsSearchQueue(
        requirement_id=req_a.id,
        requisition_id=requisition.id,
        mpn=_DEDUP_MPN,
        normalized_mpn=_DEDUP_MPN,
        status="completed",
        last_searched_at=datetime.now(UTC),
    )
    db_session.add(completed)

    # A's sightings span BOTH MPNs.
    db_session.add(_make_sighting(req_a.id, _DEDUP_MPN, "VendorMatch"))
    db_session.add(_make_sighting(req_a.id, _OTHER_MPN, "VendorOther"))

    # Target requirement B with a material card so dedup will clone.
    card = MaterialCard(normalized_mpn=_DEDUP_MPN.lower(), display_mpn=_DEDUP_MPN)
    db_session.add(card)
    db_session.flush()
    req_b = Requirement(
        requisition_id=requisition.id,
        primary_mpn=_DEDUP_MPN,
        material_card_id=card.id,
        target_qty=50,
        created_at=datetime.now(UTC),
    )
    db_session.add(req_b)
    db_session.commit()

    # Enqueue B for the same MPN → dedups against A's completed search.
    result = enqueue_for_ics_search(req_b.id, db_session)
    assert result is None  # deduped, no new queue row

    cloned = db_session.query(Sighting).filter(Sighting.requirement_id == req_b.id).all()
    # Only the deduped-MPN sighting is cloned; the OTHER-MPN one must NOT leak.
    assert len(cloned) == 1
    assert cloned[0].normalized_mpn == _DEDUP_MPN
    assert {s.normalized_mpn for s in cloned} == {_DEDUP_MPN}


def test_dedup_clones_punctuation_variant_of_searched_mpn(db_session, requisition):
    """A vendor who listed the SAME part with internal punctuation ("ABC-123") for a
    search of "ABC123" must still clone onto the deduped requirement — matched by the
    canonical MPN key, not raw equality (sightings store the vendor's typed PN, which
    keeps the dash).

    A genuinely different MPN stays excluded.
    """
    search_mpn = "ABC123"
    variant_mpn = "ABC-123"  # strip_packaging_suffixes preserves the internal dash

    req_a = Requirement(
        requisition_id=requisition.id,
        primary_mpn=search_mpn,
        target_qty=100,
        created_at=datetime.now(UTC),
    )
    db_session.add(req_a)
    db_session.flush()

    completed = IcsSearchQueue(
        requirement_id=req_a.id,
        requisition_id=requisition.id,
        mpn=search_mpn,
        normalized_mpn=search_mpn,
        status="completed",
        last_searched_at=datetime.now(UTC),
    )
    db_session.add(completed)

    # A's sightings: the same part typed with a dash, plus a genuinely unrelated MPN.
    db_session.add(_make_sighting(req_a.id, variant_mpn, "VendorVariant"))
    db_session.add(_make_sighting(req_a.id, _OTHER_MPN, "VendorOther"))

    card = MaterialCard(normalized_mpn=search_mpn.lower(), display_mpn=search_mpn)
    db_session.add(card)
    db_session.flush()
    req_b = Requirement(
        requisition_id=requisition.id,
        primary_mpn=search_mpn,
        material_card_id=card.id,
        target_qty=50,
        created_at=datetime.now(UTC),
    )
    db_session.add(req_b)
    db_session.commit()

    result = enqueue_for_ics_search(req_b.id, db_session)
    assert result is None  # deduped, no new queue row

    cloned = db_session.query(Sighting).filter(Sighting.requirement_id == req_b.id).all()
    # The punctuation variant IS the same part → cloned; the unrelated MPN stays out.
    assert {s.normalized_mpn for s in cloned} == {variant_mpn}
