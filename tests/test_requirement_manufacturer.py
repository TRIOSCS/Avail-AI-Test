"""Tests for the manufacturer column on the Requirement model.

What it does: Verifies that Requirement.manufacturer stores values and defaults to "".
What calls it: pytest
Depends on: app/models/sourcing.py, tests/conftest.py
"""

import pytest

from app.models.sourcing import Requirement, Requisition


def _make_requisition(db_session, test_user) -> Requisition:
    req = Requisition(name="Test", status="open", created_by=test_user.id)
    db_session.add(req)
    db_session.flush()
    return req


@pytest.mark.parametrize(
    "manufacturer_kwargs, expected",
    [
        ({"manufacturer": "Texas Instruments"}, "Texas Instruments"),
        ({}, ""),
    ],
    ids=["stores_value", "defaults_empty"],
)
def test_requirement_manufacturer(db_session, test_user, manufacturer_kwargs, expected):
    req = _make_requisition(db_session, test_user)
    r = Requirement(requisition_id=req.id, primary_mpn="LM317T", **manufacturer_kwargs)
    db_session.add(r)
    db_session.commit()
    assert r.manufacturer == expected
