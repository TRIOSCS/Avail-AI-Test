"""One mutation gate on every task surface (Task 3 of the 2026-08-03 plan).

task_service.is_task_mutation_authorized (creator | assignee | admin |
account-owner-for-customer-tasks) now also gates the requisition-board
complete/delete endpoints (previously any user with requisition access
could mutate any board task) and vendor-task delete (previously
admin-only — the creator can now delete their own vendor task).

Called by: pytest
Depends on: conftest.py (db_session, client, test_user, admin_user,
    test_requisition, test_vendor_card), app.services.task_service
"""

from __future__ import annotations

import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import TaskStatus
from app.models import Requisition, User
from app.models.task import RequisitionTask
from app.models.vendors import VendorCard

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def stranger(db_session: Session) -> User:
    """A second unrestricted (buyer) user: has requisition access, owns nothing."""
    u = User(
        email="stranger@trioscs.com",
        name="Stranger Buyer",
        role="buyer",
        azure_id="test-azure-id-stranger",
        created_at=datetime.now(UTC),
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


def _client_for(db_session: Session, who: User) -> TestClient:
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        return who

    async def _override_fresh():
        return "mock-token"

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_admin] = _override_user
    app.dependency_overrides[require_buyer] = _override_user
    app.dependency_overrides[require_fresh_token] = _override_fresh
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def stranger_client(db_session: Session, stranger: User):
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    overridden = [get_db, require_user, require_admin, require_buyer, require_fresh_token]
    try:
        with _client_for(db_session, stranger) as c:
            yield c
    finally:
        for dep in overridden:
            app.dependency_overrides.pop(dep, None)


@pytest.fixture()
def admin_client(db_session: Session, admin_user: User):
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    overridden = [get_db, require_user, require_admin, require_buyer, require_fresh_token]
    try:
        with _client_for(db_session, admin_user) as c:
            yield c
    finally:
        for dep in overridden:
            app.dependency_overrides.pop(dep, None)


def _board_task(
    db: Session,
    req: Requisition,
    *,
    created_by: int,
    assigned_to_id: int,
    title: str = "Board task",
) -> RequisitionTask:
    t = RequisitionTask(
        requisition_id=req.id,
        title=title,
        task_type="general",
        status=TaskStatus.OPEN,
        priority=2,
        source="manual",
        created_by=created_by,
        assigned_to_id=assigned_to_id,
        created_at=datetime.now(UTC),
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


# ---------------------------------------------------------------------------
# (a)(b) Uninvolved user WITH requisition access: board complete/delete → 403
# ---------------------------------------------------------------------------


class TestBoardMutationGate:
    def test_uninvolved_user_cannot_complete_board_task(
        self, stranger_client, db_session: Session, test_requisition: Requisition, test_user: User
    ):
        task = _board_task(db_session, test_requisition, created_by=test_user.id, assigned_to_id=test_user.id)
        resp = stranger_client.post(f"/api/requisitions/{test_requisition.id}/tasks/{task.id}/complete")
        assert resp.status_code == 403
        db_session.expire_all()
        assert db_session.get(RequisitionTask, task.id).status == TaskStatus.OPEN

    def test_uninvolved_user_cannot_delete_board_task(
        self, stranger_client, db_session: Session, test_requisition: Requisition, test_user: User
    ):
        task = _board_task(db_session, test_requisition, created_by=test_user.id, assigned_to_id=test_user.id)
        resp = stranger_client.delete(f"/api/requisitions/{test_requisition.id}/tasks/{task.id}")
        assert resp.status_code == 403
        assert db_session.get(RequisitionTask, task.id) is not None

    # ── (d) creator / assignee / admin still succeed ─────────────────────

    def test_creator_can_complete_and_delete(
        self, client, db_session: Session, test_requisition: Requisition, test_user: User, admin_user: User
    ):
        task = _board_task(db_session, test_requisition, created_by=test_user.id, assigned_to_id=admin_user.id)
        resp = client.post(f"/api/requisitions/{test_requisition.id}/tasks/{task.id}/complete")
        assert resp.status_code == 200
        resp = client.delete(f"/api/requisitions/{test_requisition.id}/tasks/{task.id}")
        assert resp.status_code == 200
        db_session.expire_all()
        assert db_session.get(RequisitionTask, task.id) is None

    def test_assignee_can_complete_and_delete(
        self, client, db_session: Session, test_requisition: Requisition, test_user: User, admin_user: User
    ):
        task = _board_task(db_session, test_requisition, created_by=admin_user.id, assigned_to_id=test_user.id)
        resp = client.post(f"/api/requisitions/{test_requisition.id}/tasks/{task.id}/complete")
        assert resp.status_code == 200
        resp = client.delete(f"/api/requisitions/{test_requisition.id}/tasks/{task.id}")
        assert resp.status_code == 200

    def test_admin_can_complete_and_delete(
        self, admin_client, db_session: Session, test_requisition: Requisition, test_user: User
    ):
        task = _board_task(db_session, test_requisition, created_by=test_user.id, assigned_to_id=test_user.id)
        resp = admin_client.post(f"/api/requisitions/{test_requisition.id}/tasks/{task.id}/complete")
        assert resp.status_code == 200
        resp = admin_client.delete(f"/api/requisitions/{test_requisition.id}/tasks/{task.id}")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# (c) Vendor-task delete opens to the creator (was admin-only)
# ---------------------------------------------------------------------------


class TestVendorDeleteOpensToCreator:
    def test_creator_nonadmin_can_delete_own_vendor_task(
        self, client, db_session: Session, test_vendor_card: VendorCard, test_user: User
    ):
        task = RequisitionTask(
            vendor_card_id=test_vendor_card.id,
            title="My vendor task",
            task_type="general",
            status=TaskStatus.OPEN,
            source="manual",
            created_by=test_user.id,
            assigned_to_id=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)
        resp = client.delete(f"/v2/partials/tasks/{task.id}")
        assert resp.status_code == 200
        db_session.expire_all()
        assert db_session.get(RequisitionTask, task.id) is None
