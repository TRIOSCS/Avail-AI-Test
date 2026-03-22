"""tests/test_api_manufacturer_validation.py — API validation for manufacturer field.

What it does: Verifies that the POST /api/requisitions/{id}/requirements endpoint
              requires a non-blank manufacturer field on creation.
What calls it: pytest
Depends on: app/routers/requisitions/requirements.py, app/schemas/requisitions.py, conftest fixtures
"""


def test_api_create_requirement_requires_manufacturer(client, db_session, test_user):
    from app.models.sourcing import Requisition

    req = Requisition(name="Test", status="active", created_by=test_user.id, claimed_by_id=test_user.id)
    db_session.add(req)
    db_session.commit()
    resp = client.post(
        f"/api/requisitions/{req.id}/requirements",
        json={
            "primary_mpn": "LM317T",
            "target_qty": 100,
        },
    )
    assert resp.status_code in (400, 422)


def test_api_create_requirement_with_manufacturer(client, db_session, test_user):
    from app.models.sourcing import Requisition

    req = Requisition(name="Test", status="active", created_by=test_user.id, claimed_by_id=test_user.id)
    db_session.add(req)
    db_session.commit()
    resp = client.post(
        f"/api/requisitions/{req.id}/requirements",
        json={
            "primary_mpn": "LM317T",
            "manufacturer": "Texas Instruments",
            "target_qty": 100,
        },
    )
    assert resp.status_code in (200, 201)


def test_api_create_requirement_blank_manufacturer_rejected(client, db_session, test_user):
    from app.models.sourcing import Requisition

    req = Requisition(name="Test", status="active", created_by=test_user.id, claimed_by_id=test_user.id)
    db_session.add(req)
    db_session.commit()
    resp = client.post(
        f"/api/requisitions/{req.id}/requirements",
        json={
            "primary_mpn": "LM317T",
            "manufacturer": "   ",
            "target_qty": 100,
        },
    )
    assert resp.status_code in (400, 422)


def test_api_update_requirement_manufacturer_optional(client, db_session, test_user):
    """PUT /api/requirements/{id} should accept updates without manufacturer (field is
    optional on update)."""
    from app.models.sourcing import Requirement, Requisition

    req = Requisition(name="Test", status="active", created_by=test_user.id, claimed_by_id=test_user.id)
    db_session.add(req)
    db_session.flush()
    r = Requirement(requisition_id=req.id, primary_mpn="LM317T", manufacturer="Texas Instruments")
    db_session.add(r)
    db_session.commit()

    resp = client.put(f"/api/requirements/{r.id}", json={"target_qty": 50})
    assert resp.status_code == 200


def test_api_update_requirement_sets_manufacturer(client, db_session, test_user):
    """PUT /api/requirements/{id} should update manufacturer when provided."""
    from app.models.sourcing import Requirement, Requisition

    req = Requisition(name="Test", status="active", created_by=test_user.id, claimed_by_id=test_user.id)
    db_session.add(req)
    db_session.flush()
    r = Requirement(requisition_id=req.id, primary_mpn="LM317T", manufacturer="Texas Instruments")
    db_session.add(r)
    db_session.commit()

    rid = r.id
    resp = client.put(f"/api/requirements/{rid}", json={"manufacturer": "ON Semiconductor"})
    assert resp.status_code == 200
    db_session.expire_all()
    updated = db_session.get(type(r), rid)
    assert updated.manufacturer == "ON Semiconductor"
