"""Tests for QueueManager override_mpn / resolved_via_spec_code support.

Verifies the (requirement_id, normalized_mpn) dedup re-keying and that the
spec-code lineage column is populated on the queue row per spec §6.4.

Called by: pytest auto-discovery.
Depends on: app/services/{ics,nc}_worker/queue_manager.py.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import IcsSearchQueue, NcSearchQueue, Requirement
from app.models.sourcing import Requisition
from app.services.ics_worker.queue_manager import enqueue_for_ics_search
from app.services.nc_worker.queue_manager import enqueue_for_nc_search


@pytest.fixture
def requisition(db_session, test_user):
    rset = Requisition(
        name="test-req",
        customer_name="Acme",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(UTC),
    )
    db_session.add(rset)
    db_session.flush()
    return rset


@pytest.fixture
def requirement(db_session, requisition):
    req = Requirement(
        requisition_id=requisition.id,
        primary_mpn="SPREJ",
        target_qty=100,
        created_at=datetime.now(UTC),
    )
    db_session.add(req)
    db_session.commit()
    return req


def test_default_uses_requirement_primary_mpn(db_session, requirement):
    """No override → queue row uses req.primary_mpn and lineage is null."""
    item = enqueue_for_ics_search(requirement.id, db_session)
    assert item is not None
    assert item.normalized_mpn == "SPREJ"
    assert item.resolved_via_spec_code is None


def test_override_mpn_used_when_provided(db_session, requirement):
    """override_mpn → queue row uses that MPN with lineage set."""
    item = enqueue_for_ics_search(
        requirement.id,
        db_session,
        override_mpn="GRM188R71H103KA01D",
        resolved_via_spec_code="SPREJ",
    )
    assert item is not None
    assert item.normalized_mpn == "GRM188R71H103KA01D"
    assert item.mpn == "GRM188R71H103KA01D"
    assert item.resolved_via_spec_code == "SPREJ"


def test_primary_and_override_coexist_for_same_requirement(db_session, requirement):
    """Critical: (requirement_id, normalized_mpn) dedup key must allow BOTH
    the primary MPN and a resolved AVL MPN to coexist on one requirement.
    """
    enqueue_for_ics_search(requirement.id, db_session)
    enqueue_for_ics_search(
        requirement.id,
        db_session,
        override_mpn="GRM188R71H103KA01D",
        resolved_via_spec_code="SPREJ",
    )
    items = db_session.query(IcsSearchQueue).filter_by(requirement_id=requirement.id).all()
    mpns = sorted(i.normalized_mpn for i in items)
    assert mpns == ["GRM188R71H103KA01D", "SPREJ"]


def test_repeat_override_same_mpn_dedups(db_session, requirement):
    """Same (requirement, override_mpn) enqueued twice returns the same row."""
    a = enqueue_for_ics_search(
        requirement.id,
        db_session,
        override_mpn="GRM188R71H103KA01D",
        resolved_via_spec_code="SPREJ",
    )
    b = enqueue_for_ics_search(
        requirement.id,
        db_session,
        override_mpn="GRM188R71H103KA01D",
        resolved_via_spec_code="SPREJ",
    )
    assert a is not None
    assert b is not None
    assert a.id == b.id
    items = (
        db_session.query(IcsSearchQueue)
        .filter_by(requirement_id=requirement.id, normalized_mpn="GRM188R71H103KA01D")
        .all()
    )
    assert len(items) == 1


def test_nc_wrapper_propagates_kwargs(db_session, requirement):
    """The NC wrapper accepts the same kwargs and propagates lineage."""
    item = enqueue_for_nc_search(
        requirement.id,
        db_session,
        override_mpn="ALT-MPN",
        resolved_via_spec_code="SPREJ",
    )
    assert item is not None
    assert item.normalized_mpn == "ALT-MPN"
    assert item.resolved_via_spec_code == "SPREJ"
    # And NC table row is independent from ICS rows
    nc_rows = db_session.query(NcSearchQueue).filter_by(requirement_id=requirement.id).all()
    assert len(nc_rows) == 1


@pytest.mark.parametrize("model", [IcsSearchQueue, NcSearchQueue])
def test_duplicate_requirement_mpn_pair_is_rejected_at_db_level(db_session, requirement, model):
    """The (requirement_id, normalized_mpn) dedup is backed by a DB UNIQUE constraint,
    not just an application-level check — two concurrent enqueues that both pass the in-
    Python lookup still cannot create duplicate rows."""
    first = model(
        requirement_id=requirement.id,
        requisition_id=requirement.requisition_id,
        mpn="SPREJ",
        normalized_mpn="SPREJ",
        status="pending",
    )
    db_session.add(first)
    db_session.commit()

    dup = model(
        requirement_id=requirement.id,
        requisition_id=requirement.requisition_id,
        mpn="SPREJ",
        normalized_mpn="SPREJ",
        status="pending",
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
