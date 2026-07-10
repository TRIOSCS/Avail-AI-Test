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

from datetime import UTC, datetime

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
        created_at=datetime.now(UTC),
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
        active = _make_user(db_session, email="on@trioscs.com", azure_id="az-1", last_login_at=datetime.now(UTC))

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


# ── Set manager (reports_to) ─────────────────────────────────────────


class TestSetManager:
    def test_admin_can_set_manager(self, admin_client, db_session, test_user):
        mgr = _make_user(db_session, email="boss@trioscs.com", role="manager")
        r = admin_client.post(f"/api/admin/users/{test_user.id}/manager", data={"reports_to_id": str(mgr.id)})
        assert r.status_code == 200
        db_session.refresh(test_user)
        assert test_user.reports_to_id == mgr.id
        rows = _audit_rows(db_session, UserAuditAction.MANAGER_CHANGE)
        assert len(rows) == 1
        assert rows[0].detail["from"] is None
        assert rows[0].detail["to"] == mgr.id

    def test_admin_can_set_admin_as_manager(self, admin_client, db_session, test_user, admin_user):
        r = admin_client.post(f"/api/admin/users/{test_user.id}/manager", data={"reports_to_id": str(admin_user.id)})
        assert r.status_code == 200
        db_session.refresh(test_user)
        assert test_user.reports_to_id == admin_user.id

    def test_admin_can_clear_manager(self, admin_client, db_session, test_user):
        mgr = _make_user(db_session, email="boss2@trioscs.com", role="manager")
        test_user.reports_to_id = mgr.id
        db_session.commit()
        r = admin_client.post(f"/api/admin/users/{test_user.id}/manager", data={"reports_to_id": ""})
        assert r.status_code == 200
        db_session.refresh(test_user)
        assert test_user.reports_to_id is None
        rows = _audit_rows(db_session, UserAuditAction.MANAGER_CHANGE)
        assert rows[-1].detail["from"] == mgr.id
        assert rows[-1].detail["to"] is None

    def test_rejects_self_as_manager(self, admin_client, db_session, test_user):
        r = admin_client.post(f"/api/admin/users/{test_user.id}/manager", data={"reports_to_id": str(test_user.id)})
        assert r.status_code == 400
        db_session.refresh(test_user)
        assert test_user.reports_to_id is None

    def test_rejects_non_supervisor_manager(self, admin_client, db_session, test_user):
        buyer = _make_user(db_session, email="plain-buyer@trioscs.com", role="buyer")
        r = admin_client.post(f"/api/admin/users/{test_user.id}/manager", data={"reports_to_id": str(buyer.id)})
        assert r.status_code == 400
        db_session.refresh(test_user)
        assert test_user.reports_to_id is None

    def test_rejects_inactive_manager(self, admin_client, db_session, test_user):
        dead = _make_user(db_session, email="dead-boss@trioscs.com", role="manager", is_active=False)
        r = admin_client.post(f"/api/admin/users/{test_user.id}/manager", data={"reports_to_id": str(dead.id)})
        assert r.status_code == 400
        db_session.refresh(test_user)
        assert test_user.reports_to_id is None

    def test_no_op_when_unchanged_does_not_audit(self, admin_client, db_session, test_user):
        mgr = _make_user(db_session, email="boss3@trioscs.com", role="manager")
        test_user.reports_to_id = mgr.id
        db_session.commit()
        r = admin_client.post(f"/api/admin/users/{test_user.id}/manager", data={"reports_to_id": str(mgr.id)})
        assert r.status_code == 200
        assert _audit_rows(db_session, UserAuditAction.MANAGER_CHANGE) == []

    def test_manager_404_for_missing_user(self, admin_client):
        r = admin_client.post("/api/admin/users/999999/manager", data={"reports_to_id": ""})
        assert r.status_code == 404

    def test_manager_options_and_row_reports_to_in_context(self, db_session, admin_user, test_user):
        from app.routers.admin.users import users_context

        mgr = _make_user(db_session, email="boss4@trioscs.com", role="manager")
        _make_user(db_session, email="inactive-boss@trioscs.com", role="manager", is_active=False)
        test_user.reports_to_id = mgr.id
        db_session.commit()

        ctx = users_context(db_session)
        opt_emails = {u.email for u in ctx["manager_options"]}
        assert mgr.email in opt_emails  # active manager offered
        assert admin_user.email in opt_emails  # active admin offered
        assert "inactive-boss@trioscs.com" not in opt_emails  # inactive excluded
        assert test_user.email not in opt_emails  # a plain buyer is not a manager option
        row = next(r for r in ctx["rows"] if r["user"].id == test_user.id)
        assert row["reports_to_id"] == mgr.id


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
                client.post(f"/api/admin/users/{test_user.id}/manager", data={"reports_to_id": ""}).status_code == 403
            )
            assert (
                client.post(f"/api/admin/users/{test_user.id}/active", data={"is_active": "false"}).status_code == 403
            )
        finally:
            from app.main import app

            app.dependency_overrides.pop(get_db, None)


# ── Audit log viewer (Phase 5) ───────────────────────────────────────


def _seed_audit(db, *, actor_id, target_user_id, action, detail=None):
    """Insert a UserAdminAudit row directly (bypasses the route)."""
    from app.services.user_admin import record_user_audit

    record_user_audit(db, actor_id=actor_id, target_user_id=target_user_id, action=action, detail=detail)
    db.commit()


class TestUsersAuditContext:
    def test_resolves_actor_and_target_newest_first(self, db_session, admin_user):
        from app.routers.admin.users import users_audit_context

        target = _make_user(db_session, email="t@trioscs.com", name="Target")
        _seed_audit(
            db_session,
            actor_id=admin_user.id,
            target_user_id=target.id,
            action=UserAuditAction.INVITE,
            detail={"email": target.email, "role": "buyer"},
        )
        _seed_audit(
            db_session,
            actor_id=admin_user.id,
            target_user_id=target.id,
            action=UserAuditAction.ROLE_CHANGE,
            detail={"from": "buyer", "to": "trader"},
        )

        ctx = users_audit_context(db_session)
        assert ctx["total"] == 2
        assert ctx["truncated"] is False
        actions = [r["action"] for r in ctx["audit_rows"]]
        # newest first → role_change (inserted last) before invite
        assert actions == [UserAuditAction.ROLE_CHANGE, UserAuditAction.INVITE]
        first = ctx["audit_rows"][0]
        assert first["actor"].id == admin_user.id
        assert first["target"].id == target.id

    def test_missing_actor_and_target_tolerated(self, db_session, admin_user):
        from app.routers.admin.users import users_audit_context

        # actor_id None (system) and target pointing at a now-deleted id are tolerated.
        target = _make_user(db_session, email="gone@trioscs.com")
        _seed_audit(
            db_session,
            actor_id=None,
            target_user_id=target.id,
            action=UserAuditAction.DEACTIVATE,
        )
        ctx = users_audit_context(db_session)
        row = ctx["audit_rows"][0]
        assert row["actor"] is None
        assert row["target"].id == target.id

    def test_limit_truncates_and_orders(self, db_session, admin_user):
        from app.routers.admin.users import users_audit_context

        target = _make_user(db_session, email="many@trioscs.com")
        _seed_audit(db_session, actor_id=admin_user.id, target_user_id=target.id, action=UserAuditAction.ACTIVATE)
        _seed_audit(db_session, actor_id=admin_user.id, target_user_id=target.id, action=UserAuditAction.DEACTIVATE)

        ctx = users_audit_context(db_session, limit=1)
        assert ctx["total"] == 2
        assert ctx["limit"] == 1
        assert ctx["truncated"] is True
        assert len(ctx["audit_rows"]) == 1
        # the single returned row is the newest (deactivate, inserted last)
        assert ctx["audit_rows"][0]["action"] == UserAuditAction.DEACTIVATE


class TestUsersAuditEndpoint:
    def test_renders_for_admin_with_both_actions(self, admin_client, db_session, test_user):
        # Perform a real invite + role change so the route writes the audit rows.
        r1 = admin_client.post("/api/admin/users/invite", data={"email": "audited@trioscs.com", "role": "buyer"})
        assert r1.status_code == 200
        r2 = admin_client.post(f"/api/admin/users/{test_user.id}/role", data={"role": "manager"})
        assert r2.status_code == 200

        r = admin_client.get("/api/admin/users/audit")
        assert r.status_code == 200
        # both humanized action labels appear, role-change (newest) before invite
        assert "Role changed" in r.text
        assert "Invited" in r.text
        assert r.text.index("Role changed") < r.text.index("Invited")

    def test_empty_state(self, admin_client):
        r = admin_client.get("/api/admin/users/audit")
        assert r.status_code == 200
        assert "No actions recorded yet" in r.text

    @pytest.mark.parametrize("role", ["buyer", "manager"])
    def test_forbidden_for_non_admin(self, db_session, role, monkeypatch):
        user = _make_user(db_session, email=f"{role}@auditgate.test", role=role)
        client, get_db = _non_admin_client(db_session, user, monkeypatch)
        try:
            assert client.get("/api/admin/users/audit").status_code == 403
        finally:
            from app.main import app

            app.dependency_overrides.pop(get_db, None)


# ── Prepayment approver toggle + limit ───────────────────────────────


class TestPrepaymentApprover:
    def test_grant_prepayment_right(self, admin_client, db_session, test_user):
        """Admin can grant prepayment approval right; audit row written."""
        r = admin_client.post(
            f"/api/admin/users/{test_user.id}/prepayment-approver",
            data={"can_approve": "true", "limit": ""},
        )
        assert r.status_code == 200
        db_session.refresh(test_user)
        assert test_user.can_approve_prepayments is True
        assert test_user.prepayment_approval_limit is None
        assert len(_audit_rows(db_session, UserAuditAction.APPROVAL_GRANT)) == 1

    def test_grant_with_dollar_limit(self, admin_client, db_session, test_user):
        """Admin can grant prepayment approval with a capped limit."""
        r = admin_client.post(
            f"/api/admin/users/{test_user.id}/prepayment-approver",
            data={"can_approve": "true", "limit": "1000.00"},
        )
        assert r.status_code == 200
        db_session.refresh(test_user)
        assert test_user.can_approve_prepayments is True
        from decimal import Decimal

        assert test_user.prepayment_approval_limit == Decimal("1000.00")

    def test_revoke_prepayment_right(self, admin_client, db_session, test_user):
        """Admin can revoke prepayment approval right; audit row written."""
        test_user.can_approve_prepayments = True
        db_session.commit()

        r = admin_client.post(
            f"/api/admin/users/{test_user.id}/prepayment-approver",
            data={"can_approve": "false", "limit": ""},
        )
        assert r.status_code == 200
        db_session.refresh(test_user)
        assert test_user.can_approve_prepayments is False
        assert len(_audit_rows(db_session, UserAuditAction.APPROVAL_REVOKE)) == 1

    def test_invalid_limit_returns_400(self, admin_client, db_session, test_user):
        """Non-numeric limit returns 400."""
        r = admin_client.post(
            f"/api/admin/users/{test_user.id}/prepayment-approver",
            data={"can_approve": "true", "limit": "abc"},
        )
        assert r.status_code == 400

    def test_negative_limit_returns_400(self, admin_client, db_session, test_user):
        """Zero or negative limit returns 400."""
        r = admin_client.post(
            f"/api/admin/users/{test_user.id}/prepayment-approver",
            data={"can_approve": "true", "limit": "-50"},
        )
        assert r.status_code == 400

    def test_noop_no_audit(self, admin_client, db_session, test_user):
        """No-op (state unchanged) does not write an audit row."""
        # default: can_approve_prepayments=False, limit=None
        r = admin_client.post(
            f"/api/admin/users/{test_user.id}/prepayment-approver",
            data={"can_approve": "false", "limit": ""},
        )
        assert r.status_code == 200
        assert len(_audit_rows(db_session, UserAuditAction.APPROVAL_GRANT)) == 0
        assert len(_audit_rows(db_session, UserAuditAction.APPROVAL_REVOKE)) == 0

    def test_admin_gated(self, db_session, monkeypatch, test_user):
        """Non-admin gets 403."""
        user = _make_user(db_session, email="buyer2@gate.test", role="buyer")
        client, get_db = _non_admin_client(db_session, user, monkeypatch)
        try:
            r = client.post(
                f"/api/admin/users/{test_user.id}/prepayment-approver",
                data={"can_approve": "true", "limit": ""},
            )
            assert r.status_code == 403
        finally:
            from app.main import app

            app.dependency_overrides.pop(get_db, None)

    def test_404_for_agent_account(self, admin_client, db_session):
        """Agent service account returns 404."""
        agent = _make_user(db_session, email=_AGENT_EMAIL, role="agent")
        r = admin_client.post(
            f"/api/admin/users/{agent.id}/prepayment-approver",
            data={"can_approve": "true", "limit": ""},
        )
        assert r.status_code == 404


def test_users_audit_template_renders():
    """Smoke-render users_audit.html with a hand-built context (no DB)."""
    from types import SimpleNamespace

    from app.template_env import templates

    actor = SimpleNamespace(name="Admin Person", email="admin@trioscs.com")
    target = SimpleNamespace(name=None, email="target@trioscs.com")
    rows = [
        {
            "when": datetime.now(UTC),
            "actor": actor,
            "target": target,
            "action": UserAuditAction.ROLE_CHANGE,
            "detail": {"from": "buyer", "to": "trader"},
        },
        {
            "when": datetime.now(UTC),
            "actor": None,
            "target": None,
            "action": UserAuditAction.INVITE,
            "detail": {"email": "x@trioscs.com", "role": "buyer"},
        },
    ]
    html = templates.get_template("htmx/partials/settings/users_audit.html").render(
        audit_rows=rows, limit=200, truncated=True, total=999
    )
    assert "User management audit log" in html
    assert "Role changed" in html
    assert "Invited" in html
    assert "target@trioscs.com" in html
    assert "system" in html  # actor None
    assert "Showing latest 200 of 999" in html


# ── SET-03: validation errors (400) are swappable + rendered ──────────────────


class TestUsersTabErrorRender:
    """SET-03 — a 400 validation re-render carries the inline error banner AND the forms
    carry hx-target-4xx so htmx (which won't swap a 4xx by default) actually shows it
    instead of falling back to a generic toast."""

    def test_users_tab_forms_carry_4xx_error_target(self, admin_client):
        """Every user-mgmt form routes 4xx responses back into #users-content so the re-
        rendered error banner is swapped in rather than dropped."""
        html = admin_client.get("/v2/partials/settings/users").text
        assert 'hx-target-4xx="#users-content"' in html
        # The invite form (always present) must carry it, not just per-row forms.
        invite_idx = html.index("/api/admin/users/invite")
        # Look at the invite form's attribute block around its hx-post.
        assert 'hx-target-4xx="#users-content"' in html[invite_idx - 200 : invite_idx + 200]

    def test_invite_400_body_contains_error_banner(self, admin_client, db_session):
        """The 400 re-render body carries the inline banner text (the swappable content
        hx-target-4xx delivers) — not an empty/JSON body."""
        r = admin_client.post("/api/admin/users/invite", data={"email": "notanemail", "role": "buyer"})
        assert r.status_code == 400
        assert "Enter a valid email address." in r.text
        assert 'id="users-content"' in r.text  # full partial re-rendered, swap-ready
