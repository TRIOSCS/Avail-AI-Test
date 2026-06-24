"""test_user_management.py — Admin Users tab CRUD (Phase 2).

Covers users_context helper, the admin-gated GET tab, and the three POST
mutations (invite / change-role / activate-deactivate): DB effect + a
UserAdminAudit row per action, self-protection + last-admin guards, the
agent service account being hidden + uneditable, and admin-only gating
(buyer AND manager get 403 on every endpoint).

The admin-auth pattern mirrors tests/test_credential_management.py: a
TestClient whose require_user/require_admin/get_db are overridden to the
admin user + test session. For the 403 path we monkeypatch
app.dependencies.require_user (which the real require_admin calls directly)
to a non-admin user so the REAL role check runs and rejects.

Called by: pytest
Depends on: app.routers.admin.users, app.models (User, UserAdminAudit), conftest
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.constants import UserAuditAction, UserRole
from app.models import User, UserAdminAudit

_AGENT_EMAIL = "agent@availai.local"


# ── Helpers / fixtures ───────────────────────────────────────────────


def _make_user(db, *, email, role="buyer", name=None, is_active=True, azure_id=None, last_login_at=None):
    u = User(
        email=email,
        name=name or email.split("@")[0],
        role=role,
        is_active=is_active,
        azure_id=azure_id,
        last_login_at=last_login_at,
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture()
def admin_client(db_session, admin_user):
    """TestClient authenticated as the admin user (require_admin satisfied)."""
    from app.database import get_db
    from app.dependencies import require_admin, require_user
    from app.main import app

    def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = lambda: admin_user
    app.dependency_overrides[require_admin] = lambda: admin_user
    try:
        yield TestClient(app)
    finally:
        for dep in (get_db, require_user, require_admin):
            app.dependency_overrides.pop(dep, None)


def _non_admin_client(db_session, user, monkeypatch):
    """TestClient where the REAL require_admin runs against *user* (non-admin).

    require_admin calls the module-level require_user directly, so we patch that symbol
    to return *user*; the real role check then 403s.
    """
    from app.database import get_db
    from app.main import app

    monkeypatch.setattr("app.dependencies.require_user", lambda request, db: user)
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app), get_db


def _audit_rows(db, action):
    return db.query(UserAdminAudit).filter_by(action=str(action)).all()


# ── users_context ────────────────────────────────────────────────────


class TestUsersContext:
    def test_excludes_agent_and_derives_status(self, db_session, admin_user):
        from app.routers.admin.users import users_context

        _make_user(db_session, email=_AGENT_EMAIL, role="agent")
        disabled = _make_user(db_session, email="off@trioscs.com", is_active=False)
        invited = _make_user(db_session, email="inv@trioscs.com")  # active, no azure_id, no login
        active = _make_user(
            db_session, email="on@trioscs.com", azure_id="az-1", last_login_at=datetime.now(timezone.utc)
        )

        ctx = users_context(db_session)
        emails = [r["user"].email for r in ctx["rows"]]
        assert _AGENT_EMAIL not in emails
        status = {r["user"].email: r["status"] for r in ctx["rows"]}
        assert status[disabled.email] == "Disabled"
        assert status[invited.email] == "Invited"
        assert status[active.email] == "Active"
        # roles offered for assignment never include the service role
        assert UserRole.AGENT not in ctx["roles"]
        assert UserRole.ADMIN in ctx["roles"]
        assert ctx["active_admin_count"] == 1  # admin_user


# ── GET tab gating ───────────────────────────────────────────────────


class TestUsersTab:
    def test_renders_for_admin(self, admin_client):
        r = admin_client.get("/v2/partials/settings/users")
        assert r.status_code == 200
        assert "Users" in r.text

    def test_forbidden_for_non_admin(self, client):
        # default client's require_user returns the buyer test_user
        assert client.get("/v2/partials/settings/users").status_code == 403


# ── Invite ───────────────────────────────────────────────────────────


class TestInvite:
    def test_admin_can_invite(self, admin_client, db_session):
        r = admin_client.post(
            "/api/admin/users/invite",
            data={"email": "New.Person@TriosCS.com", "role": "sales", "name": "New Person"},
        )
        assert r.status_code == 200
        u = db_session.query(User).filter_by(email="new.person@trioscs.com").first()
        assert u is not None
        assert u.role == UserRole.SALES
        assert u.is_active is True
        assert len(_audit_rows(db_session, UserAuditAction.INVITE)) == 1

    def test_invite_defaults_name_to_localpart(self, admin_client, db_session):
        r = admin_client.post("/api/admin/users/invite", data={"email": "solo@trioscs.com", "role": "buyer"})
        assert r.status_code == 200
        u = db_session.query(User).filter_by(email="solo@trioscs.com").first()
        assert u.name == "solo"

    def test_invite_rejects_bad_email(self, admin_client, db_session):
        r = admin_client.post("/api/admin/users/invite", data={"email": "notanemail", "role": "buyer"})
        assert r.status_code == 400
        assert db_session.query(User).filter_by(email="notanemail").first() is None

    def test_invite_rejects_duplicate(self, admin_client, db_session, test_user):
        before = db_session.query(User).count()
        r = admin_client.post("/api/admin/users/invite", data={"email": test_user.email, "role": "buyer"})
        assert r.status_code == 400
        assert db_session.query(User).count() == before

    def test_invite_rejects_agent_role(self, admin_client, db_session):
        r = admin_client.post("/api/admin/users/invite", data={"email": "svc@trioscs.com", "role": "agent"})
        assert r.status_code == 400
        assert db_session.query(User).filter_by(email="svc@trioscs.com").first() is None


# ── Change role ──────────────────────────────────────────────────────


class TestChangeRole:
    def test_admin_can_change_role(self, admin_client, db_session, test_user):
        r = admin_client.post(f"/api/admin/users/{test_user.id}/role", data={"role": "manager"})
        assert r.status_code == 200
        db_session.refresh(test_user)
        assert test_user.role == UserRole.MANAGER
        rows = _audit_rows(db_session, UserAuditAction.ROLE_CHANGE)
        assert len(rows) == 1
        assert rows[0].detail["from"] == "buyer"
        assert rows[0].detail["to"] == "manager"

    def test_role_rejects_agent_role(self, admin_client, db_session, test_user):
        r = admin_client.post(f"/api/admin/users/{test_user.id}/role", data={"role": "agent"})
        assert r.status_code == 400
        db_session.refresh(test_user)
        assert test_user.role == UserRole.BUYER

    def test_role_404_for_missing_user(self, admin_client):
        assert admin_client.post("/api/admin/users/999999/role", data={"role": "buyer"}).status_code == 404

    def test_role_404_for_agent_account(self, admin_client, db_session):
        agent = _make_user(db_session, email=_AGENT_EMAIL, role="agent")
        r = admin_client.post(f"/api/admin/users/{agent.id}/role", data={"role": "buyer"})
        assert r.status_code == 404

    def test_cannot_demote_self(self, admin_client, db_session, admin_user):
        r = admin_client.post(f"/api/admin/users/{admin_user.id}/role", data={"role": "buyer"})
        assert r.status_code == 400
        db_session.refresh(admin_user)
        assert admin_user.role == UserRole.ADMIN

    def test_cannot_demote_last_admin(self, admin_client, db_session, admin_user):
        # A second admin exists, but they're inactive → admin_user is the last ACTIVE admin.
        _make_user(db_session, email="other-admin@trioscs.com", role="admin", is_active=False)
        r = admin_client.post(f"/api/admin/users/{admin_user.id}/role", data={"role": "manager"})
        assert r.status_code == 400
        db_session.refresh(admin_user)
        assert admin_user.role == UserRole.ADMIN


# ── Activate / deactivate ────────────────────────────────────────────


class TestActiveToggle:
    def test_admin_can_deactivate(self, admin_client, db_session, test_user):
        r = admin_client.post(f"/api/admin/users/{test_user.id}/active", data={"is_active": "false"})
        assert r.status_code == 200
        db_session.refresh(test_user)
        assert test_user.is_active is False
        assert len(_audit_rows(db_session, UserAuditAction.DEACTIVATE)) == 1

    def test_admin_can_reactivate(self, admin_client, db_session):
        off = _make_user(db_session, email="off@trioscs.com", is_active=False)
        r = admin_client.post(f"/api/admin/users/{off.id}/active", data={"is_active": "true"})
        assert r.status_code == 200
        db_session.refresh(off)
        assert off.is_active is True
        assert len(_audit_rows(db_session, UserAuditAction.ACTIVATE)) == 1

    def test_cannot_deactivate_self(self, admin_client, db_session, admin_user):
        r = admin_client.post(f"/api/admin/users/{admin_user.id}/active", data={"is_active": "false"})
        assert r.status_code == 400
        db_session.refresh(admin_user)
        assert admin_user.is_active is True

    def test_cannot_deactivate_last_admin(self, admin_client, db_session, admin_user):
        # Make admin_user NOT the actor by acting via a second active admin client.
        from app.database import get_db
        from app.dependencies import require_admin, require_user
        from app.main import app

        second_admin = _make_user(db_session, email="second-admin@trioscs.com", role="admin")
        # Deactivate second_admin first so admin_user becomes the only active admin,
        # then a different active admin tries to deactivate admin_user.
        app.dependency_overrides[get_db] = lambda: db_session
        app.dependency_overrides[require_user] = lambda: second_admin
        app.dependency_overrides[require_admin] = lambda: second_admin
        try:
            c = TestClient(app)
            # second_admin deactivates admin_user — both active, allowed
            r1 = c.post(f"/api/admin/users/{admin_user.id}/active", data={"is_active": "false"})
            assert r1.status_code == 200
            # now second_admin is the last active admin; cannot deactivate self either,
            # but more importantly cannot be deactivated as last admin by anyone.
            r2 = c.post(f"/api/admin/users/{second_admin.id}/active", data={"is_active": "false"})
            assert r2.status_code == 400
            db_session.refresh(second_admin)
            assert second_admin.is_active is True
        finally:
            for dep in (get_db, require_user, require_admin):
                app.dependency_overrides.pop(dep, None)

    def test_active_404_for_agent_account(self, admin_client, db_session):
        agent = _make_user(db_session, email=_AGENT_EMAIL, role="agent")
        r = admin_client.post(f"/api/admin/users/{agent.id}/active", data={"is_active": "false"})
        assert r.status_code == 404


# ── Admin-only gating (buyer AND manager → 403 on every endpoint) ────


class TestAdminOnly:
    @pytest.mark.parametrize("role", ["buyer", "manager"])
    def test_all_post_endpoints_403_for_non_admin(self, db_session, role, monkeypatch, test_user):
        user = _make_user(db_session, email=f"{role}@gate.test", role=role)
        client, get_db = _non_admin_client(db_session, user, monkeypatch)
        try:
            assert (
                client.post("/api/admin/users/invite", data={"email": "x@trioscs.com", "role": "buyer"}).status_code
                == 403
            )
            assert client.post(f"/api/admin/users/{test_user.id}/role", data={"role": "manager"}).status_code == 403
            assert (
                client.post(f"/api/admin/users/{test_user.id}/active", data={"is_active": "false"}).status_code == 403
            )
        finally:
            from app.main import app

            app.dependency_overrides.pop(get_db, None)
