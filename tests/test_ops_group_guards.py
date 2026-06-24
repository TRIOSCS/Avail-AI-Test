"""test_ops_group_guards.py — Safety guards on the ops verification-group toggle.

The ops verification group gates SO/PO verification and buy-plan completion. The
toggle handler must refuse to (a) deactivate the LAST active member (which would make
buy plans uncompletable app-wide) or (b) let an admin remove themselves. The success
path fires a showToast HX-Trigger so the admin gets feedback.

Covered handler: app/routers/admin/buy_plan_ops.py::toggle_ops_member.
The `client` fixture authenticates as `test_user` AND overrides require_admin to that
same user, so `test_user` is the "current admin" for self-removal checks.
"""

from app.models.buy_plan import VerificationGroupMember


def test_cannot_remove_last_active_member(client, db_session, admin_user):
    """Deactivating the only active member is refused (400 + error), no mutation."""
    # admin_user is the ONLY active member (current user test_user is NOT a member),
    # so removing admin_user would drop active_count to 0.
    db_session.add(VerificationGroupMember(user_id=admin_user.id, is_active=True))
    db_session.commit()

    resp = client.post("/api/admin/ops-group/toggle", data={"user_id": admin_user.id})
    assert resp.status_code == 400
    assert "error" in resp.json()

    # The member must NOT have been deactivated.
    m = db_session.query(VerificationGroupMember).filter_by(user_id=admin_user.id).first()
    db_session.refresh(m)
    assert m.is_active is True


def test_cannot_remove_self(client, db_session, admin_user):
    """The current admin cannot remove themselves, even when others remain active.

    `test_user` is the authed admin (via the client fixture's require_admin override).
    Seed test_user + admin_user both active so removing self is blocked by the
    self-removal guard, not the last-member guard.
    """
    from app.dependencies import require_user
    from app.main import app

    test_user = app.dependency_overrides[require_user]()
    db_session.add(VerificationGroupMember(user_id=test_user.id, is_active=True))
    db_session.add(VerificationGroupMember(user_id=admin_user.id, is_active=True))
    db_session.commit()

    resp = client.post("/api/admin/ops-group/toggle", data={"user_id": test_user.id})
    assert resp.status_code == 400
    assert "yourself" in resp.json()["error"].lower()

    m = db_session.query(VerificationGroupMember).filter_by(user_id=test_user.id).first()
    db_session.refresh(m)
    assert m.is_active is True


def test_add_member_succeeds_with_toast(client, db_session, sales_user):
    """Adding a member returns 200 and fires a showToast HX-Trigger."""
    resp = client.post("/api/admin/ops-group/toggle", data={"user_id": sales_user.id})
    assert resp.status_code == 200
    assert "showToast" in resp.headers.get("HX-Trigger", "")

    m = db_session.query(VerificationGroupMember).filter_by(user_id=sales_user.id).first()
    assert m is not None and m.is_active is True


def test_reactivate_inactive_member_not_blocked(client, db_session, sales_user):
    """Re-activating an inactive member is an add, not a removal — never blocked."""
    db_session.add(VerificationGroupMember(user_id=sales_user.id, is_active=False))
    db_session.commit()

    resp = client.post("/api/admin/ops-group/toggle", data={"user_id": sales_user.id})
    assert resp.status_code == 200
    assert "showToast" in resp.headers.get("HX-Trigger", "")

    m = db_session.query(VerificationGroupMember).filter_by(user_id=sales_user.id).first()
    db_session.refresh(m)
    assert m.is_active is True


def test_deactivate_non_last_member_succeeds(client, db_session, admin_user, sales_user):
    """Deactivating one of several active members succeeds with a toast."""
    db_session.add(VerificationGroupMember(user_id=admin_user.id, is_active=True))
    db_session.add(VerificationGroupMember(user_id=sales_user.id, is_active=True))
    db_session.commit()

    resp = client.post("/api/admin/ops-group/toggle", data={"user_id": sales_user.id})
    assert resp.status_code == 200
    assert "showToast" in resp.headers.get("HX-Trigger", "")

    m = db_session.query(VerificationGroupMember).filter_by(user_id=sales_user.id).first()
    db_session.refresh(m)
    assert m.is_active is False
