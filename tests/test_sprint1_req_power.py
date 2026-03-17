"""test_sprint1_req_power.py — Tests for Sprint 1 requisition power features.

Verifies: Clone action, Won/Lost row actions via dropdown, row action dropdown template
rendering, and existing inline edit / bulk / sort features.

Called by: pytest
Depends on: conftest.py fixtures, app.routers.requisitions2
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Requirement, Requisition, User


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def active_req(db_session: Session, test_user: User):
    """An active requisition with one requirement."""
    req = Requisition(
        name="Sprint1 Test Req",
        status="active",
        customer_name="Acme Corp",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    r = Requirement(
        requisition_id=req.id, primary_mpn="LM317T", target_qty=500
    )
    db_session.add(r)
    db_session.commit()
    db_session.refresh(req)
    return req


# ── Clone Action ──────────────────────────────────────────────────────


class TestCloneAction:
    def test_clone_creates_new_requisition(self, client: TestClient, active_req: Requisition, db_session: Session):
        """POST /requisitions2/{id}/action/clone creates a cloned req."""
        resp = client.post(
            f"/requisitions2/{active_req.id}/action/clone",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

        # Verify a new requisition was created
        clones = (
            db_session.query(Requisition)
            .filter(Requisition.cloned_from_id == active_req.id)
            .all()
        )
        assert len(clones) == 1
        clone = clones[0]
        assert "clone" in clone.name.lower()
        assert clone.customer_name == active_req.customer_name
        assert clone.status == "active"

    def test_clone_copies_requirements(self, client: TestClient, active_req: Requisition, db_session: Session):
        """Clone should copy requirements to the new requisition."""
        client.post(
            f"/requisitions2/{active_req.id}/action/clone",
            headers={"HX-Request": "true"},
        )

        clone = (
            db_session.query(Requisition)
            .filter(Requisition.cloned_from_id == active_req.id)
            .first()
        )
        clone_reqs = (
            db_session.query(Requirement)
            .filter(Requirement.requisition_id == clone.id)
            .all()
        )
        assert len(clone_reqs) == 1
        assert clone_reqs[0].primary_mpn == "LM317T"

    def test_clone_toast_message(self, client: TestClient, active_req: Requisition):
        """Clone should return a toast message with the new req ID."""
        resp = client.post(
            f"/requisitions2/{active_req.id}/action/clone",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        trigger = resp.headers.get("HX-Trigger", "")
        assert "Cloned" in trigger


# ── Won/Lost Actions ─────────────────────────────────────────────────


class TestWonLostActions:
    def test_mark_won(self, client: TestClient, active_req: Requisition, db_session: Session):
        """POST /requisitions2/{id}/action/won marks req as won."""
        resp = client.post(
            f"/requisitions2/{active_req.id}/action/won",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(active_req)
        assert active_req.status == "won"

    def test_mark_lost(self, client: TestClient, active_req: Requisition, db_session: Session):
        """POST /requisitions2/{id}/action/lost marks quoted req as lost."""
        # Only quoted → lost is allowed by state machine
        active_req.status = "quoted"
        db_session.commit()

        resp = client.post(
            f"/requisitions2/{active_req.id}/action/lost",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(active_req)
        assert active_req.status == "lost"


# ── Dropdown Rendering ────────────────────────────────────────────────


class TestRowActionsDropdown:
    def test_active_req_shows_won_and_clone(self, client: TestClient, active_req: Requisition):
        """Table rows for active reqs should have won/clone actions (not lost)."""
        resp = client.get(
            "/requisitions2/table",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        html = resp.text
        assert f"/requisitions2/{active_req.id}/action/clone" in html
        assert f"/requisitions2/{active_req.id}/action/won" in html
        # Lost only available from quoted status
        assert f"/requisitions2/{active_req.id}/action/lost" not in html

    def test_archived_req_shows_activate(self, client: TestClient, active_req: Requisition, db_session: Session):
        """Archived reqs should show Activate (not Archive) in dropdown."""
        active_req.status = "archived"
        db_session.commit()

        resp = client.get(
            "/requisitions2/table?status=archived",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        html = resp.text
        assert f"/requisitions2/{active_req.id}/action/activate" in html
        # Won/Lost should not appear for archived reqs
        assert f"/requisitions2/{active_req.id}/action/won" not in html


# ── Existing Features Still Work ─────────────────────────────────────


class TestExistingFeatures:
    def test_sortable_columns(self, client: TestClient, active_req: Requisition):
        """Table should have sortable column links."""
        resp = client.get(
            "/requisitions2/table",
            headers={"HX-Request": "true"},
        )
        html = resp.text
        assert "sort=name" in html
        assert "sort=status" in html
        assert "sort=created_at" in html

    def test_inline_edit_cell(self, client: TestClient, active_req: Requisition):
        """GET /requisitions2/{id}/edit/name returns inline edit form."""
        resp = client.get(
            f"/requisitions2/{active_req.id}/edit/name",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert 'name="value"' in resp.text
        assert 'name="field"' in resp.text

    def test_inline_save(self, client: TestClient, active_req: Requisition, db_session: Session):
        """PATCH /requisitions2/{id}/inline saves the edit."""
        resp = client.patch(
            f"/requisitions2/{active_req.id}/inline",
            data={"field": "name", "value": "Renamed Req"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(active_req)
        assert active_req.name == "Renamed Req"

    def test_bulk_archive(self, client: TestClient, active_req: Requisition, db_session: Session):
        """POST /requisitions2/bulk/archive archives selected reqs."""
        resp = client.post(
            "/requisitions2/bulk/archive",
            data={"ids": str(active_req.id)},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(active_req)
        assert active_req.status == "archived"
