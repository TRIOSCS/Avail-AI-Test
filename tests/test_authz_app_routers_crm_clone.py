"""Regression tests for requisition-ownership IDOR guards in app/routers/crm/clone.py.

The clone endpoint must reject SALES/TRADER users attempting to clone a requisition they
do not own (created_by != user.id), while leaving the buyer/admin happy path intact.
"""

from app.constants import UserRole


def test_clone_requisition_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    """A SALES user cannot clone a requisition owned by someone else (404)."""
    test_user.role = UserRole.SALES
    test_requisition.created_by = admin_user.id  # owned by someone else
    db_session.commit()

    resp = client.post(f"/api/requisitions/{test_requisition.id}/clone")
    assert resp.status_code == 404


def test_clone_requisition_blocks_non_owner_trader(client, db_session, test_requisition, test_user, admin_user):
    """A TRADER user cannot clone a requisition owned by someone else (404)."""
    test_user.role = UserRole.TRADER
    test_requisition.created_by = admin_user.id
    db_session.commit()

    resp = client.post(f"/api/requisitions/{test_requisition.id}/clone")
    assert resp.status_code == 404


def test_clone_requisition_allows_owner_sales(client, db_session, test_requisition, test_user):
    """A SALES user CAN clone a requisition they own."""
    test_user.role = UserRole.SALES
    test_requisition.created_by = test_user.id
    db_session.commit()

    resp = client.post(f"/api/requisitions/{test_requisition.id}/clone")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_clone_requisition_allows_buyer(client, db_session, test_requisition, test_user, admin_user):
    """A BUYER (unrestricted) can clone any requisition, even one owned by someone
    else."""
    test_user.role = UserRole.BUYER
    test_requisition.created_by = admin_user.id
    db_session.commit()

    resp = client.post(f"/api/requisitions/{test_requisition.id}/clone")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
