"""Tests for the shared QueueManager atomic-claim + stuck-reclaim hardening.

- claim_next_queued_item: selects the next 'queued' row and marks it 'searching'
  in one transaction (FOR UPDATE SKIP LOCKED on Postgres; plain on SQLite).
- reclaim_stuck_searches: resets items stuck in 'searching' past the timeout so a
  crashed worker's in-flight item is recovered without a restart.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.models import IcsSearchQueue, Requirement
from app.models.sourcing import Requisition
from app.services.ics_worker.queue_manager import (
    claim_next_queued_item,
    reclaim_stuck_searches,
)


@pytest.fixture
def requisition(db_session, test_user):
    r = Requisition(
        name="wq-req",
        customer_name="Acme",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(UTC),
    )
    db_session.add(r)
    db_session.flush()
    return r


@pytest.fixture
def requirement(db_session, requisition):
    req = Requirement(
        requisition_id=requisition.id,
        primary_mpn="LM317",
        target_qty=100,
        created_at=datetime.now(UTC),
    )
    db_session.add(req)
    db_session.commit()
    return req


def _row(db, requirement, requisition, *, status="queued", priority=3, mpn="LM317", updated_at=None):
    row = IcsSearchQueue(
        requirement_id=requirement.id,
        requisition_id=requisition.id,
        mpn=mpn,
        normalized_mpn=mpn,
        status=status,
        priority=priority,
        created_at=datetime.now(UTC),
        updated_at=updated_at or datetime.now(UTC),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_claim_marks_searching_and_returns(db_session, requirement, requisition):
    row = _row(db_session, requirement, requisition)
    claimed = claim_next_queued_item(db_session)
    assert claimed is not None
    assert claimed.id == row.id
    assert claimed.status == "searching"


def test_claim_returns_none_when_empty(db_session, requirement, requisition):
    _row(db_session, requirement, requisition, status="completed")
    assert claim_next_queued_item(db_session) is None


def test_claim_respects_priority(db_session, requirement, requisition):
    _row(db_session, requirement, requisition, priority=3, mpn="LOWPRI")
    _row(db_session, requirement, requisition, priority=1, mpn="HIGHPRI")
    claimed = claim_next_queued_item(db_session)
    assert claimed.normalized_mpn == "HIGHPRI"  # priority 1 wins


def test_reclaim_resets_stuck_but_keeps_fresh(db_session, requirement, requisition):
    old = _row(
        db_session,
        requirement,
        requisition,
        status="searching",
        mpn="STUCK",
        updated_at=datetime.now(UTC) - timedelta(minutes=45),
    )
    fresh = _row(db_session, requirement, requisition, status="searching", mpn="FRESH")
    n = reclaim_stuck_searches(db_session)  # default 30m timeout
    assert n == 1
    db_session.refresh(old)
    db_session.refresh(fresh)
    assert old.status == "queued"
    assert fresh.status == "searching"


def test_claim_auto_reclaims_then_claims(db_session, requirement, requisition):
    # Only a stuck 'searching' item exists — claim should reclaim then grab it.
    stuck = _row(
        db_session,
        requirement,
        requisition,
        status="searching",
        mpn="STUCK",
        updated_at=datetime.now(UTC) - timedelta(minutes=60),
    )
    claimed = claim_next_queued_item(db_session)
    assert claimed is not None
    assert claimed.id == stuck.id
    assert claimed.status == "searching"


def test_reclaim_honors_custom_timeout(db_session, requirement, requisition):
    row = _row(
        db_session,
        requirement,
        requisition,
        status="searching",
        updated_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    assert reclaim_stuck_searches(db_session, max_age_minutes=30) == 0  # 10m < 30m
    assert reclaim_stuck_searches(db_session, max_age_minutes=5) == 1  # 10m > 5m
    db_session.refresh(row)
    assert row.status == "queued"
