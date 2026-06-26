"""tests/test_api_manufacturer_validation.py — API validation for manufacturer field.

What it does: Verifies that the POST /api/requisitions/{id}/requirements endpoint
              requires a non-blank manufacturer field on creation.
What calls it: pytest
Depends on: app/routers/requisitions/requirements.py, app/schemas/requisitions.py, conftest fixtures
"""

import pytest


def _make_requisition(db_session, test_user):
    from app.models.sourcing import Requisition

    req = Requisition(name="Test", status="open", created_by=test_user.id, claimed_by_id=test_user.id)
    db_session.add(req)
    db_session.flush()
    return req


@pytest.mark.parametrize(
    "body,expected_statuses",
    [
        ({"primary_mpn": "LM317T", "target_qty": 100}, (400, 422)),  # missing manufacturer
        (
            {"primary_mpn": "LM317T", "manufacturer": "Texas Instruments", "target_qty": 100},
            (200, 201),
        ),  # valid manufacturer
        (
            {"primary_mpn": "LM317T", "manufacturer": "   ", "target_qty": 100},
            (400, 422),
        ),  # blank manufacturer rejected
    ],
    ids=["requires_manufacturer", "with_manufacturer", "blank_manufacturer_rejected"],
)
def test_api_create_requirement_manufacturer_validation(client, db_session, test_user, body, expected_statuses):
    req = _make_requisition(db_session, test_user)
    db_session.commit()
    resp = client.post(f"/api/requisitions/{req.id}/requirements", json=body)
    assert resp.status_code in expected_statuses


def test_api_update_requirement_manufacturer_optional(client, db_session, test_user):
    """PUT /api/requirements/{id} should accept updates without manufacturer (field is
    optional on update)."""
    from app.models.sourcing import Requirement

    req = _make_requisition(db_session, test_user)
    r = Requirement(requisition_id=req.id, primary_mpn="LM317T", manufacturer="Texas Instruments")
    db_session.add(r)
    db_session.commit()

    resp = client.put(f"/api/requirements/{r.id}", json={"target_qty": 50})
    assert resp.status_code == 200


def test_api_update_requirement_sets_manufacturer(client, db_session, test_user):
    """PUT /api/requirements/{id} should update manufacturer when provided."""
    from app.models.sourcing import Requirement

    req = _make_requisition(db_session, test_user)
    r = Requirement(requisition_id=req.id, primary_mpn="LM317T", manufacturer="Texas Instruments")
    db_session.add(r)
    db_session.commit()

    rid = r.id
    resp = client.put(f"/api/requirements/{rid}", json={"manufacturer": "ON Semiconductor"})
    assert resp.status_code == 200
    db_session.expire_all()
    updated = db_session.get(type(r), rid)
    assert updated.manufacturer == "ON Semiconductor"
