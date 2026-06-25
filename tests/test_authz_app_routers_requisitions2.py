"""Regression tests: owner-reassignment authz on app/routers/requisitions2.py.

Reassigning a requisition's owner (created_by) is a supervisor action — only
manager/admin may do it. Three reassignment paths are gated with is_manager_or_admin:
  PATCH /requisitions2/{id}/inline           field=owner   (inline_save)
  POST  /requisitions2/{id}/action/assign    owner_id=...  (row_action)
  POST  /requisitions2/bulk/assign           owner_id=...  (bulk_action)

A non-manager (BUYER — even the requisition's own owner) gets 403 and the owner is
unchanged; a manager succeeds. Other inline fields (e.g. name) stay editable by the owner.

The `client` fixture auth-overrides to test_user; we flip test_user.role to exercise
non-manager vs manager. test_requisition.created_by == test_user.id by fixture default.
"""

from app.constants import UserRole
from app.models import Requisition


def _other_owner(db_session, admin_user):
    return admin_user.id


# ── inline_save field=owner ─────────────────────────────────────────────


def test_inline_owner_reassign_blocks_non_manager_buyer(client, db_session, test_user, admin_user, test_requisition):
    test_user.role = UserRole.BUYER  # non-manager, and the requisition's own owner
    db_session.commit()
    original = test_requisition.created_by
    resp = client.patch(
        f"/requisitions2/{test_requisition.id}/inline",
        data={"field": "owner", "value": str(admin_user.id)},
    )
    assert resp.status_code == 403
    db_session.refresh(test_requisition)
    assert test_requisition.created_by == original


def test_inline_owner_reassign_allows_manager(client, db_session, test_user, admin_user, test_requisition):
    test_user.role = UserRole.MANAGER
    db_session.commit()
    resp = client.patch(
        f"/requisitions2/{test_requisition.id}/inline",
        data={"field": "owner", "value": str(admin_user.id)},
    )
    assert resp.status_code == 200
    db_session.refresh(test_requisition)
    assert test_requisition.created_by == admin_user.id


def test_inline_name_edit_still_allowed_for_owner_buyer(client, db_session, test_user, test_requisition):
    """The manager gate is owner-specific: a BUYER owner can still edit other fields."""
    test_user.role = UserRole.BUYER
    db_session.commit()
    resp = client.patch(
        f"/requisitions2/{test_requisition.id}/inline",
        data={"field": "name", "value": "Renamed By Owner"},
    )
    assert resp.status_code == 200
    db_session.refresh(test_requisition)
    assert test_requisition.name == "Renamed By Owner"


# ── row_action assign ───────────────────────────────────────────────────


def test_row_action_assign_blocks_non_manager_buyer(client, db_session, test_user, admin_user, test_requisition):
    test_user.role = UserRole.BUYER
    db_session.commit()
    original = test_requisition.created_by
    resp = client.post(
        f"/requisitions2/{test_requisition.id}/action/assign",
        data={"owner_id": str(admin_user.id)},
    )
    assert resp.status_code == 403
    db_session.refresh(test_requisition)
    assert test_requisition.created_by == original


def test_row_action_assign_allows_manager(client, db_session, test_user, admin_user, test_requisition):
    test_user.role = UserRole.MANAGER
    db_session.commit()
    resp = client.post(
        f"/requisitions2/{test_requisition.id}/action/assign",
        data={"owner_id": str(admin_user.id)},
    )
    assert resp.status_code == 200
    db_session.refresh(test_requisition)
    assert test_requisition.created_by == admin_user.id


# ── bulk_action assign ──────────────────────────────────────────────────


def test_bulk_assign_blocks_non_manager_buyer(client, db_session, test_user, admin_user, test_requisition):
    test_user.role = UserRole.BUYER
    db_session.commit()
    original = test_requisition.created_by
    resp = client.post(
        "/requisitions2/bulk/assign",
        data={"ids": str(test_requisition.id), "owner_id": str(admin_user.id)},
    )
    assert resp.status_code == 403
    db_session.refresh(test_requisition)
    assert test_requisition.created_by == original


def test_bulk_assign_allows_manager(client, db_session, test_user, admin_user, test_requisition):
    test_user.role = UserRole.MANAGER
    db_session.commit()
    resp = client.post(
        "/requisitions2/bulk/assign",
        data={"ids": str(test_requisition.id), "owner_id": str(admin_user.id)},
    )
    assert resp.status_code == 200
    reassigned = db_session.get(Requisition, test_requisition.id)
    assert reassigned.created_by == admin_user.id
