"""Requisition.is_archived + default status.

Called by: pytest. Depends on: app.models, conftest db_session.
"""

from app.models import Requisition


def test_default_status_is_open(db_session):
    r = Requisition(name="R1")
    db_session.add(r)
    db_session.commit()
    assert r.status == "open"
    assert r.is_archived is False


def test_is_archived_settable(db_session):
    r = Requisition(name="R2", is_archived=True)
    db_session.add(r)
    db_session.commit()
    assert r.is_archived is True
