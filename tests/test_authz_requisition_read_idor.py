"""Read-IDOR regression for requisition-scoped GET partials/APIs.

The canonical v2 path scopes requisition reads by ownership, but legacy GET
partials/APIs loaded the record by id and only 404'd on missing — never
calling require_requisition_access — so a restricted (SALES/TRADER) non-owner
could read another rep's requisition detail, tabs, PDF, and task rows by id.
A restricted non-owner must get 404 (existence not leaked); owners and
unrestricted buyers must still get 200.

The /api/requirements/{id}/offers|notes|history probes died with their orphan
routes (W2 B11); the guarantee now rides the surviving requisition-scoped
reads: GET /api/requisitions/{req_id}/pdf and
GET /api/requisitions/{req_id}/tasks/{task_id}/row.

Called by: pytest
Depends on: app.routers.htmx.requisitions, app.routers.documents,
            conftest fixtures (client, db_session, test_requisition, test_user, admin_user)
"""

from datetime import UTC, datetime
from unittest.mock import patch

from app.constants import TaskStatus, UserRole
from app.models import RequisitionTask


def _make_foreign(db_session, test_requisition, test_user, admin_user, role=UserRole.SALES):
    """Restrict test_user and hand requisition ownership to someone else."""
    test_user.role = role
    test_requisition.created_by = admin_user.id
    db_session.commit()


def _make_task(db_session, test_requisition, created_by: int) -> RequisitionTask:
    t = RequisitionTask(
        requisition_id=test_requisition.id,
        title="IDOR probe task",
        task_type="general",
        status=TaskStatus.OPEN,
        priority=2,
        source="manual",
        created_by=created_by,
        created_at=datetime.now(UTC),
    )
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    return t


# ── GET /v2/partials/requisitions/{req_id} (detail) ──────────────────────────


def test_requisition_detail_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    _make_foreign(db_session, test_requisition, test_user, admin_user)
    assert client.get(f"/v2/partials/requisitions/{test_requisition.id}").status_code == 404


def test_requisition_detail_blocks_non_owner_trader(client, db_session, test_requisition, test_user, admin_user):
    _make_foreign(db_session, test_requisition, test_user, admin_user, role=UserRole.TRADER)
    assert client.get(f"/v2/partials/requisitions/{test_requisition.id}").status_code == 404


def test_requisition_detail_allows_owning_sales(client, db_session, test_requisition, test_user):
    test_user.role = UserRole.SALES
    test_requisition.created_by = test_user.id
    db_session.commit()
    assert client.get(f"/v2/partials/requisitions/{test_requisition.id}").status_code == 200


# ── GET /v2/partials/requisitions/{req_id}/tab/{tab} ─────────────────────────


def test_requisition_tab_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    _make_foreign(db_session, test_requisition, test_user, admin_user)
    assert client.get(f"/v2/partials/requisitions/{test_requisition.id}/tab/parts").status_code == 404


def test_requisition_tab_allows_buyer(client, db_session, test_requisition, test_user):
    assert test_user.role == "buyer"
    assert client.get(f"/v2/partials/requisitions/{test_requisition.id}/tab/parts").status_code == 200


# ── GET /api/requisitions/{req_id}/pdf ───────────────────────────────────────


def test_requisition_pdf_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    _make_foreign(db_session, test_requisition, test_user, admin_user)
    assert client.get(f"/api/requisitions/{test_requisition.id}/pdf").status_code == 404


@patch("app.services.document_service.generate_rfq_summary_pdf", return_value=b"%PDF-fake-content")
def test_requisition_pdf_allows_buyer(mock_gen, client, db_session, test_requisition, test_user):
    assert test_user.role == "buyer"
    assert client.get(f"/api/requisitions/{test_requisition.id}/pdf").status_code == 200


# ── GET /api/requisitions/{req_id}/tasks/{task_id}/row ───────────────────────


def test_requisition_task_row_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    task = _make_task(db_session, test_requisition, created_by=admin_user.id)
    _make_foreign(db_session, test_requisition, test_user, admin_user)
    assert client.get(f"/api/requisitions/{test_requisition.id}/tasks/{task.id}/row").status_code == 404


def test_requisition_task_row_allows_buyer(client, db_session, test_requisition, test_user):
    assert test_user.role == "buyer"
    task = _make_task(db_session, test_requisition, created_by=test_user.id)
    assert client.get(f"/api/requisitions/{test_requisition.id}/tasks/{task.id}/row").status_code == 200
