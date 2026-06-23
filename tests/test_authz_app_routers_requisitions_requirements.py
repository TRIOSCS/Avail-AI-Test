"""Requisition-ownership IDOR regression tests for
app/routers/requisitions/requirements.py.

Covers the mutating requirement-scoped endpoints that load a Requirement by path id and
previously mutated it with no ownership guard. A restricted (SALES/TRADER) non-owner
must get 404 (existence not leaked); buyer/manager/admin happy path must stay 200/OK.
"""

from app.constants import UserRole
from app.models import Requirement


def _requirement_id(db_session, test_requisition):
    return db_session.query(Requirement).filter(Requirement.requisition_id == test_requisition.id).first().id


# ── POST /api/requirements/{requirement_id}/notes ────────────────────────


def test_add_requirement_note_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    test_user.role = UserRole.SALES
    test_requisition.created_by = admin_user.id  # owned by someone else
    db_session.commit()
    rid = _requirement_id(db_session, test_requisition)

    resp = client.post(f"/api/requirements/{rid}/notes", json={"text": "sneaky note"})
    assert resp.status_code == 404


def test_add_requirement_note_blocks_non_owner_trader(client, db_session, test_requisition, test_user, admin_user):
    test_user.role = UserRole.TRADER
    test_requisition.created_by = admin_user.id
    db_session.commit()
    rid = _requirement_id(db_session, test_requisition)

    resp = client.post(f"/api/requirements/{rid}/notes", json={"text": "sneaky note"})
    assert resp.status_code == 404


def test_add_requirement_note_allows_buyer(client, db_session, test_requisition, test_user):
    # test_user is a buyer and owns test_requisition (default fixture wiring)
    assert test_user.role == "buyer"
    rid = _requirement_id(db_session, test_requisition)

    resp = client.post(f"/api/requirements/{rid}/notes", json={"text": "legit note"})
    assert resp.status_code == 200
    assert "legit note" in resp.json()["notes"]


def test_add_requirement_note_allows_owning_sales(client, db_session, test_requisition, test_user):
    # SALES user who OWNS the requisition is allowed through.
    test_user.role = UserRole.SALES
    test_requisition.created_by = test_user.id
    db_session.commit()
    rid = _requirement_id(db_session, test_requisition)

    resp = client.post(f"/api/requirements/{rid}/notes", json={"text": "my own note"})
    assert resp.status_code == 200


# ── POST /api/requirements/{requirement_id}/tasks ────────────────────────


def test_create_requirement_task_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    test_user.role = UserRole.SALES
    test_requisition.created_by = admin_user.id
    db_session.commit()
    rid = _requirement_id(db_session, test_requisition)

    resp = client.post(f"/api/requirements/{rid}/tasks", json={"title": "sneaky task"})
    assert resp.status_code == 404


def test_create_requirement_task_blocks_non_owner_trader(client, db_session, test_requisition, test_user, admin_user):
    test_user.role = UserRole.TRADER
    test_requisition.created_by = admin_user.id
    db_session.commit()
    rid = _requirement_id(db_session, test_requisition)

    resp = client.post(f"/api/requirements/{rid}/tasks", json={"title": "sneaky task"})
    assert resp.status_code == 404


def test_create_requirement_task_allows_buyer(client, db_session, test_requisition, test_user):
    assert test_user.role == "buyer"
    rid = _requirement_id(db_session, test_requisition)

    resp = client.post(f"/api/requirements/{rid}/tasks", json={"title": "legit task"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "legit task"


def test_create_requirement_task_allows_owning_sales(client, db_session, test_requisition, test_user):
    test_user.role = UserRole.SALES
    test_requisition.created_by = test_user.id
    db_session.commit()
    rid = _requirement_id(db_session, test_requisition)

    resp = client.post(f"/api/requirements/{rid}/tasks", json={"title": "my own task"})
    assert resp.status_code == 200
