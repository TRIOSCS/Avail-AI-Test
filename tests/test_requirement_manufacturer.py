"""Tests for the manufacturer column on the Requirement model.

What it does: Verifies that Requirement.manufacturer stores values and defaults to "".
What calls it: pytest
Depends on: app/models/sourcing.py, tests/conftest.py
"""

from app.models.sourcing import Requirement, Requisition


def test_requirement_has_manufacturer(db_session, test_user):
    req = Requisition(name="Test", status="active", created_by=test_user.id)
    db_session.add(req)
    db_session.flush()
    r = Requirement(requisition_id=req.id, primary_mpn="LM317T", manufacturer="Texas Instruments")
    db_session.add(r)
    db_session.commit()
    assert r.manufacturer == "Texas Instruments"


def test_requirement_manufacturer_defaults_empty(db_session, test_user):
    req = Requisition(name="Test", status="active", created_by=test_user.id)
    db_session.add(req)
    db_session.flush()
    r = Requirement(requisition_id=req.id, primary_mpn="LM317T")
    db_session.add(r)
    db_session.commit()
    assert r.manufacturer == ""
