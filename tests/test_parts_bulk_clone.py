"""test_parts_bulk_clone.py — Clone-to-Active bulk action on the parts workspace.

Exercises POST /v2/partials/parts/bulk-clone: happy path with single- and
multi-requisition toast wording, empty-ids 400, restricted-role ownership 404,
background sightings search scheduling, clone-from-any-status (won/lost/
hotlist/open), and the archive-view status passthrough on the re-render.

Depends on: conftest fixtures (client, db_session, test_user, test_requisition,
admin_user).
"""

import pytest

from app.constants import SourcingStatus, UserRole
from app.models import Requirement, Requisition

BULK_CLONE_URL = "/v2/partials/parts/bulk-clone"


def _make_requisition(db, user, name="REQ-CLONE-SRC"):
    req = Requisition(name=name, customer_name="Acme", status="open", created_by=user.id)
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


def _make_requirement(db, req_id, mpn, status=SourcingStatus.OPEN):
    r = Requirement(requisition_id=req_id, primary_mpn=mpn, sourcing_status=status)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


@pytest.fixture(autouse=True)
def search_spy(monkeypatch):
    """Replace the background sightings search with a recorder.

    Autouse so no test ever fires the real multi-supplier fan-out; tests that assert
    scheduling request it by name and inspect the recorded calls.
    """
    calls: list[tuple[list[int], int]] = []

    async def _fake(requirement_ids, user_id, source="user"):
        calls.append((list(requirement_ids), user_id))

    monkeypatch.setattr("app.routers.htmx.parts.run_search_and_publish", _fake)
    return calls


def test_happy_path_single_requisition_toast(client, db_session, test_user):
    src = _make_requisition(db_session, test_user)
    p1 = _make_requirement(db_session, src.id, "PART-C1", status=SourcingStatus.WON)
    p2 = _make_requirement(db_session, src.id, "PART-C2", status=SourcingStatus.HOTLIST)

    resp = client.post(BULK_CLONE_URL, json={"requirement_ids": [p1.id, p2.id], "status": ""})

    assert resp.status_code == 200
    new_req = db_session.query(Requisition).filter(Requisition.cloned_from_id == src.id).one()
    trigger = resp.headers.get("HX-Trigger", "")
    assert "showToast" in trigger
    assert "2 part(s) cloned" in trigger
    assert f"REQ-{new_req.id:03d}" in trigger
    assert new_req.status == "open"
    clones = db_session.query(Requirement).filter(Requirement.requisition_id == new_req.id).all()
    assert {c.primary_mpn for c in clones} == {"PART-C1", "PART-C2"}
    assert all(c.sourcing_status == SourcingStatus.OPEN for c in clones)
    assert {c.cloned_from_id for c in clones} == {p1.id, p2.id}
    # Source parts untouched.
    assert db_session.get(Requirement, p1.id).sourcing_status == SourcingStatus.WON
    assert db_session.get(Requirement, p2.id).sourcing_status == SourcingStatus.HOTLIST


def test_multi_requisition_toast_wording(client, db_session, test_user):
    req_a = _make_requisition(db_session, test_user, name="SRC-A")
    req_b = _make_requisition(db_session, test_user, name="SRC-B")
    p1 = _make_requirement(db_session, req_a.id, "PART-MA")
    p2 = _make_requirement(db_session, req_b.id, "PART-MB")
    p3 = _make_requirement(db_session, req_a.id, "PART-MC")

    resp = client.post(BULK_CLONE_URL, json={"requirement_ids": [p1.id, p2.id, p3.id], "status": ""})

    assert resp.status_code == 200
    trigger = resp.headers.get("HX-Trigger", "")
    assert "3 part(s) cloned" in trigger
    assert "2 requisitions" in trigger
    assert db_session.query(Requisition).filter(Requisition.cloned_from_id.in_([req_a.id, req_b.id])).count() == 2


def test_empty_ids_rejected_400(client):
    resp = client.post(BULK_CLONE_URL, json={"requirement_ids": [], "status": ""})
    assert resp.status_code == 400


def test_restricted_sales_non_owner_404(client, db_session, test_requisition, test_user, admin_user):
    """A SALES user cannot clone parts off a requisition they do not own."""
    test_user.role = UserRole.SALES
    test_requisition.created_by = admin_user.id
    db_session.commit()
    part = test_requisition.requirements[0]

    resp = client.post(BULK_CLONE_URL, json={"requirement_ids": [part.id], "status": ""})

    assert resp.status_code == 404
    # Nothing was created.
    assert db_session.query(Requisition).filter(Requisition.cloned_from_id == test_requisition.id).count() == 0


def test_background_search_scheduled_with_new_requirement_ids(client, db_session, test_user, search_spy):
    src = _make_requisition(db_session, test_user)
    p1 = _make_requirement(db_session, src.id, "PART-BG1")
    p2 = _make_requirement(db_session, src.id, "PART-BG2")

    resp = client.post(BULK_CLONE_URL, json={"requirement_ids": [p1.id, p2.id], "status": ""})

    assert resp.status_code == 200
    new_req = db_session.query(Requisition).filter(Requisition.cloned_from_id == src.id).one()
    new_ids = [r.id for r in db_session.query(Requirement).filter(Requirement.requisition_id == new_req.id)]
    assert len(search_spy) == 1
    scheduled_ids, scheduled_user = search_spy[0]
    assert sorted(scheduled_ids) == sorted(new_ids)
    assert scheduled_user == test_user.id


def test_clone_allowed_from_any_status(client, db_session, test_user):
    src = _make_requisition(db_session, test_user)
    parts = [
        _make_requirement(db_session, src.id, "PART-WONX", status=SourcingStatus.WON),
        _make_requirement(db_session, src.id, "PART-LOSTX", status=SourcingStatus.LOST),
        _make_requirement(db_session, src.id, "PART-HOTX", status=SourcingStatus.HOTLIST),
        _make_requirement(db_session, src.id, "PART-OPENX", status=SourcingStatus.OPEN),
    ]

    resp = client.post(BULK_CLONE_URL, json={"requirement_ids": [p.id for p in parts], "status": ""})

    assert resp.status_code == 200
    assert "4 part(s) cloned" in resp.headers.get("HX-Trigger", "")
    new_req = db_session.query(Requisition).filter(Requisition.cloned_from_id == src.id).one()
    clones = db_session.query(Requirement).filter(Requirement.requisition_id == new_req.id).all()
    assert len(clones) == 4
    assert all(c.sourcing_status == SourcingStatus.OPEN for c in clones)


def test_archive_view_status_passthrough(client, db_session, test_user):
    """Cloning from the Archive view re-renders the archive view (status echo)."""
    src = _make_requisition(db_session, test_user)
    part = _make_requirement(db_session, src.id, "PART-ARC", status=SourcingStatus.WON)

    resp = client.post(BULK_CLONE_URL, json={"requirement_ids": [part.id], "status": "archive"})

    assert resp.status_code == 200
    # The archive branch of the bulk bar (Reopen + Clone to Active) rendered.
    assert "bulkReopen" in resp.text
    assert "Clone to Active" in resp.text
