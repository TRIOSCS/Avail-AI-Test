"""tests/test_htmx_parts_workspace.py — Tests for the split-panel parts workspace.

Covers the parts list endpoint, detail tab endpoints, task CRUD,
column preferences, and workspace routing.

Called by: pytest
Depends on: conftest.py fixtures (client, db_session, test_user)
"""

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Offer, Requirement, Requisition, Sighting, User
from app.models.task import RequisitionTask


def _make_requisition(db: Session, user: User, name: str = "Test Req", status: str = "active") -> Requisition:
    req = Requisition(name=name, customer_name="Acme Corp", status=status, created_by=user.id)
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


def _make_requirement(db: Session, req: Requisition, mpn: str = "LM358N", brand: str = "TI") -> Requirement:
    r = Requirement(requisition_id=req.id, primary_mpn=mpn, brand=brand, target_qty=100)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _make_offer(db: Session, req: Requisition, requirement: Requirement) -> Offer:
    o = Offer(
        requisition_id=req.id, requirement_id=requirement.id,
        vendor_name="SupplierX", mpn=requirement.primary_mpn,
        unit_price=1.25, qty_available=500, status="active",
    )
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def _make_sighting(db: Session, requirement: Requirement) -> Sighting:
    s = Sighting(
        requirement_id=requirement.id, vendor_name="VendorY",
        mpn_matched=requirement.primary_mpn, qty_available=1000,
        unit_price=0.95, source_type="brokerbin", score=0.75,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


# ── Workspace shell ──────────────────────────────────────────────────────


class TestWorkspaceShell:
    def test_workspace_partial_returns_html(self, client: TestClient):
        resp = client.get("/v2/partials/parts/workspace")
        assert resp.status_code == 200
        assert "select a part" in resp.text.lower() or "Select a part" in resp.text

    def test_v2_requisitions_loads_workspace(self, client: TestClient):
        resp = client.get("/v2/requisitions")
        assert resp.status_code == 200
        assert "parts/workspace" in resp.text or "AvailAI" in resp.text


# ── Parts list ───────────────────────────────────────────────────────────


class TestPartsList:
    def test_parts_list_empty(self, client: TestClient):
        resp = client.get("/v2/partials/parts")
        assert resp.status_code == 200
        assert "No parts found" in resp.text

    def test_parts_list_shows_requirement(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        r = _make_requirement(db_session, req)
        resp = client.get("/v2/partials/parts")
        assert resp.status_code == 200
        assert "LM358N" in resp.text

    def test_parts_list_filters_by_brand(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        _make_requirement(db_session, req, mpn="LM358N", brand="TI")
        _make_requirement(db_session, req, mpn="NE555P", brand="Fairchild")
        resp = client.get("/v2/partials/parts?brand=TI")
        assert resp.status_code == 200
        assert "LM358N" in resp.text
        assert "NE555P" not in resp.text

    def test_parts_list_filters_by_search(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        _make_requirement(db_session, req, mpn="LM358N", brand="TI")
        resp = client.get("/v2/partials/parts?q=LM358")
        assert resp.status_code == 200
        assert "LM358N" in resp.text

    def test_parts_list_excludes_archived_by_default(self, client: TestClient, db_session: Session, test_user: User):
        active_req = _make_requisition(db_session, test_user, name="Active Req", status="active")
        archived_req = _make_requisition(db_session, test_user, name="Archived Req", status="archived")
        _make_requirement(db_session, active_req, mpn="ACTIVE-001")
        _make_requirement(db_session, archived_req, mpn="ARCHIVED-001")
        resp = client.get("/v2/partials/parts")
        assert resp.status_code == 200
        assert "ACTIVE-001" in resp.text
        assert "ARCHIVED-001" not in resp.text

    def test_parts_list_includes_archived_when_toggled(self, client: TestClient, db_session: Session, test_user: User):
        archived_req = _make_requisition(db_session, test_user, name="Archived Req", status="archived")
        _make_requirement(db_session, archived_req, mpn="ARCHIVED-001")
        resp = client.get("/v2/partials/parts?include_archived=true")
        assert resp.status_code == 200
        assert "ARCHIVED-001" in resp.text

    def test_parts_list_shows_offer_count(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        r = _make_requirement(db_session, req)
        _make_offer(db_session, req, r)
        resp = client.get("/v2/partials/parts")
        assert resp.status_code == 200
        # The offer count should appear somewhere in the row
        assert "$1.25" in resp.text or "1.2500" in resp.text

    def test_parts_list_sorting(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        _make_requirement(db_session, req, mpn="AAA-001")
        _make_requirement(db_session, req, mpn="ZZZ-999")
        resp = client.get("/v2/partials/parts?sort=mpn&dir=asc")
        assert resp.status_code == 200
        text = resp.text
        assert text.index("AAA-001") < text.index("ZZZ-999")

    def test_parts_list_pagination(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        for i in range(5):
            _make_requirement(db_session, req, mpn=f"PART-{i:03d}")
        resp = client.get("/v2/partials/parts?limit=2&offset=0")
        assert resp.status_code == 200
        assert "1\u20132 of 5" in resp.text or "1–2 of 5" in resp.text


# ── Detail tabs ──────────────────────────────────────────────────────────


class TestDetailTabs:
    def test_offers_tab(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        r = _make_requirement(db_session, req)
        _make_offer(db_session, req, r)
        resp = client.get(f"/v2/partials/parts/{r.id}/tab/offers")
        assert resp.status_code == 200
        assert "SupplierX" in resp.text

    def test_offers_tab_empty(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        r = _make_requirement(db_session, req)
        resp = client.get(f"/v2/partials/parts/{r.id}/tab/offers")
        assert resp.status_code == 200
        assert "No offers" in resp.text

    def test_sourcing_tab(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        r = _make_requirement(db_session, req)
        _make_sighting(db_session, r)
        resp = client.get(f"/v2/partials/parts/{r.id}/tab/sourcing")
        assert resp.status_code == 200
        assert "VendorY" in resp.text

    def test_activity_tab(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        r = _make_requirement(db_session, req)
        resp = client.get(f"/v2/partials/parts/{r.id}/tab/activity")
        assert resp.status_code == 200
        assert "No activity" in resp.text or "Activity" in resp.text

    def test_comms_tab(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        r = _make_requirement(db_session, req)
        resp = client.get(f"/v2/partials/parts/{r.id}/tab/comms")
        assert resp.status_code == 200
        assert "No tasks" in resp.text or "Communications" in resp.text

    def test_tab_404_for_missing_part(self, client: TestClient):
        resp = client.get("/v2/partials/parts/999999/tab/offers")
        assert resp.status_code == 404


# ── Task CRUD ────────────────────────────────────────────────────────────


class TestTaskCrud:
    def test_create_task(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        r = _make_requirement(db_session, req)
        resp = client.post(
            f"/v2/partials/parts/{r.id}/tasks",
            data={"title": "Follow up with vendor", "assigned_to": str(test_user.id)},
        )
        assert resp.status_code == 200
        assert "Follow up with vendor" in resp.text

        task = db_session.query(RequisitionTask).filter_by(requirement_id=r.id).first()
        assert task is not None
        assert task.title == "Follow up with vendor"
        assert task.status == "todo"

    def test_create_task_requires_title(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        r = _make_requirement(db_session, req)
        resp = client.post(f"/v2/partials/parts/{r.id}/tasks", data={"title": ""})
        assert resp.status_code == 422

    def test_mark_task_done(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        r = _make_requirement(db_session, req)
        task = RequisitionTask(
            requisition_id=req.id, requirement_id=r.id,
            title="Test task", status="todo", created_by=test_user.id,
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)

        resp = client.post(f"/v2/partials/parts/tasks/{task.id}/done")
        assert resp.status_code == 200

        db_session.refresh(task)
        assert task.status == "done"
        assert task.completed_at is not None

    def test_reopen_task(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        r = _make_requirement(db_session, req)
        task = RequisitionTask(
            requisition_id=req.id, requirement_id=r.id,
            title="Done task", status="done", created_by=test_user.id,
            completed_at=datetime.now(timezone.utc),
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)

        resp = client.post(f"/v2/partials/parts/tasks/{task.id}/reopen")
        assert resp.status_code == 200

        db_session.refresh(task)
        assert task.status == "todo"
        assert task.completed_at is None


# ── Column preferences ──────────────────────────────────────────────────


class TestColumnPrefs:
    def test_save_column_prefs(self, client: TestClient, db_session: Session, test_user: User):
        resp = client.post(
            "/v2/partials/parts/column-prefs",
            data={"columns": ["mpn", "brand", "offers"]},
        )
        assert resp.status_code == 200

        db_session.refresh(test_user)
        assert test_user.parts_column_prefs == ["mpn", "brand", "offers"]

    def test_save_empty_prefs_uses_defaults(self, client: TestClient, db_session: Session, test_user: User):
        resp = client.post("/v2/partials/parts/column-prefs", data={})
        assert resp.status_code == 200

        db_session.refresh(test_user)
        assert len(test_user.parts_column_prefs) > 0
