"""Requisition default status + outcome_reason column.

Replaces the removed is_archived model test (requisition archiving was dropped —
a requisition ends in WON or LOST with a required outcome_reason).

Called by: pytest. Depends on: app.models, conftest db_session.
"""

from app.models import Requisition


def test_default_status_is_open(db_session):
    r = Requisition(name="R1")
    db_session.add(r)
    db_session.commit()
    assert r.status == "open"


def test_outcome_reason_defaults_none_and_is_settable(db_session):
    r = Requisition(name="R2")
    db_session.add(r)
    db_session.commit()
    assert r.outcome_reason is None

    r.outcome_reason = "Customer chose a competitor on price"
    db_session.commit()
    db_session.refresh(r)
    assert r.outcome_reason == "Customer chose a competitor on price"
