"""Privilege-escalation regression: only managers/admins may reassign a
requisition's owner via the legacy htmx_views inline-save + bulk-assign paths.

The canonical requisitions2 path already 403s on is_manager_or_admin, but these
legacy twins did not — letting any BUYER reassign ownership of any requisition
(single via inline-save, and up to 200 at once via bulk-assign). A non-manager
must get 403 and the owner must be unchanged; a manager/admin must succeed.

Called by: pytest
Depends on: app.routers.htmx_views, conftest (client, db_session, test_requisition,
            test_user, admin_user)
"""

from app.constants import UserRole


def test_inline_owner_reassign_blocked_for_buyer(client, db_session, test_requisition, test_user, admin_user):
    assert test_user.role == "buyer"
    original_owner = test_requisition.created_by
    resp = client.patch(
        f"/v2/partials/requisitions/{test_requisition.id}/inline",
        data={"field": "owner", "value": str(admin_user.id)},
    )
    assert resp.status_code == 403
    db_session.refresh(test_requisition)
    assert test_requisition.created_by == original_owner


def test_inline_owner_reassign_allowed_for_manager(client, db_session, test_requisition, test_user, admin_user):
    test_user.role = UserRole.MANAGER
    db_session.commit()
    resp = client.patch(
        f"/v2/partials/requisitions/{test_requisition.id}/inline",
        data={"field": "owner", "value": str(admin_user.id)},
    )
    assert resp.status_code == 200
    db_session.refresh(test_requisition)
    assert test_requisition.created_by == admin_user.id


def test_bulk_assign_blocked_for_buyer(client, db_session, test_requisition, test_user, admin_user):
    assert test_user.role == "buyer"
    original_owner = test_requisition.created_by
    resp = client.post(
        "/v2/partials/requisitions/bulk/assign",
        data={"ids": str(test_requisition.id), "owner_id": str(admin_user.id)},
    )
    assert resp.status_code == 403
    db_session.refresh(test_requisition)
    assert test_requisition.created_by == original_owner


def test_bulk_assign_allowed_for_manager(client, db_session, test_requisition, test_user, admin_user):
    test_user.role = UserRole.MANAGER
    db_session.commit()
    resp = client.post(
        "/v2/partials/requisitions/bulk/assign",
        data={"ids": str(test_requisition.id), "owner_id": str(admin_user.id)},
    )
    assert resp.status_code == 200
    db_session.refresh(test_requisition)
    assert test_requisition.created_by == admin_user.id
