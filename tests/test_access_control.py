"""test_access_control.py — Unit tests for the access-control foundation.

Covers the Phase 1 (Foundation) primitives of the user-management feature:
- user_has_access(): admin-all, role defaults, explicit per-user overrides,
  and the ops_verification delegation to VerificationGroupMember.
- record_user_audit(): appends a UserAdminAudit row the caller commits.

Plus Phase 4a (per-user access panel + nav gating):
- module_access_map(): {nav_id: bool} powering the bottom-nav gate.
- The admin Access editor POST /api/admin/users/{id}/access round-trips
  access_overrides (proving the JSON column reassignment flushes) and drives
  ops_verification through VerificationGroupMember, writing an audit row.
- GET /access-panel + POST /access admin-only gating, 404 for the agent
  target, and 400 for an invalid key.

These are pure-unit tests against the shared in-memory SQLite session
(conftest db_session + role-user fixtures). The HTTP-level tests reuse the
admin_client / non-admin monkeypatch technique from tests/test_user_management.py.

Called by: pytest autodiscovery
Depends on: app.dependencies, app.constants, app.services.user_admin, app.models,
            app.routers.admin.users
"""

from datetime import UTC

import pytest
from fastapi.testclient import TestClient

from app.constants import (
    CAPABILITY_ACCESS_KEYS,
    MODULE_ACCESS_KEYS,
    AccessKey,
    UserAuditAction,
    UserRole,
)
from app.dependencies import user_has_access
from app.models import UserAdminAudit
from app.models.buy_plan import VerificationGroupMember
from app.services.user_admin import record_user_audit

_AGENT_EMAIL = "agent@availai.local"

# The four interactive non-admin roles that preserve today's permissive behavior.
_INTERACTIVE_ROLE_FIXTURES = ("test_user", "sales_user", "trader_user", "manager_user")


# ── admin sees everything ────────────────────────────────────────────


def test_admin_has_every_access_key(admin_user):
    """Admin → True for every AccessKey (module + capability + ops_verification)."""
    for key in AccessKey:
        assert user_has_access(admin_user, key) is True, f"admin denied {key}"


# ── interactive role defaults preserve current behavior ──────────────


@pytest.mark.parametrize("role_fixture", _INTERACTIVE_ROLE_FIXTURES)
def test_interactive_roles_have_all_modules_by_default(role_fixture, request):
    """Every interactive role sees every nav module by default (nav stays fully
    visible)."""
    user = request.getfixturevalue(role_fixture)
    for key in MODULE_ACCESS_KEYS:
        assert user_has_access(user, key) is True, f"{user.role} denied module {key}"


@pytest.mark.parametrize("role_fixture", _INTERACTIVE_ROLE_FIXTURES)
def test_interactive_roles_have_buyer_capabilities_by_default(role_fixture, request):
    """send_rfq / approve_offers / export_data True by default (buyer-tier baseline)."""
    user = request.getfixturevalue(role_fixture)
    for key in (
        AccessKey.SEND_RFQ,
        AccessKey.APPROVE_OFFERS,
        AccessKey.EXPORT_DATA,
    ):
        assert user_has_access(user, key) is True, f"{user.role} denied capability {key}"


@pytest.mark.parametrize("role_fixture", _INTERACTIVE_ROLE_FIXTURES)
def test_manage_connectors_not_default_for_interactive_roles(role_fixture, request):
    """manage_connectors is NOT a blanket interactive default (connector credentials +
    is_active are workspace-global shared state — a default would let any buyer
    overwrite shared API keys / disable data sources).

    It must be granted deliberately per user.
    """
    user = request.getfixturevalue(role_fixture)
    assert user_has_access(user, AccessKey.MANAGE_CONNECTORS) is False, (
        f"{user.role} must NOT hold manage_connectors by default"
    )


def test_manage_connectors_granted_via_explicit_override(test_user, db_session):
    """An admin can deliberately grant manage_connectors to a trusted non-admin via an
    explicit access override, and only then does the capability resolve True."""
    assert user_has_access(test_user, AccessKey.MANAGE_CONNECTORS) is False
    test_user.access_overrides = {AccessKey.MANAGE_CONNECTORS.value: True}
    db_session.commit()
    assert user_has_access(test_user, AccessKey.MANAGE_CONNECTORS) is True


def test_manage_connectors_always_true_for_admin(admin_user):
    """Admin always qualifies for manage_connectors (short-circuit), independent of any
    role default — removing it from the interactive defaults never locks admins out."""
    assert user_has_access(admin_user, AccessKey.MANAGE_CONNECTORS) is True


def test_capability_keys_constant_matches_expected_set():
    """CAPABILITY_ACCESS_KEYS is the six capability keys (registry shape guard)."""
    assert set(CAPABILITY_ACCESS_KEYS) == {
        AccessKey.SEND_RFQ,
        AccessKey.APPROVE_OFFERS,
        AccessKey.EXPORT_DATA,
        AccessKey.MANAGE_CONNECTORS,
        AccessKey.OPS_VERIFICATION,
        AccessKey.EXPORT_BULK_DATA,
    }


# ── ops_verification delegates to VerificationGroupMember ─────────────


def test_ops_verification_false_without_membership(test_user, db_session):
    """Non-admin with no VerificationGroupMember → ops_verification is False."""
    assert user_has_access(test_user, AccessKey.OPS_VERIFICATION, db_session) is False


def test_ops_verification_false_when_db_missing(test_user):
    """ops_verification requires db to resolve the group — None db → False (no
    crash)."""
    assert user_has_access(test_user, AccessKey.OPS_VERIFICATION) is False


def test_ops_verification_true_for_active_member(test_user, db_session):
    """Active VerificationGroupMember → ops_verification becomes True."""
    db_session.add(VerificationGroupMember(user_id=test_user.id, is_active=True))
    db_session.commit()
    assert user_has_access(test_user, AccessKey.OPS_VERIFICATION, db_session) is True


def test_ops_verification_false_for_inactive_member(test_user, db_session):
    """Inactive VerificationGroupMember → ops_verification stays False."""
    db_session.add(VerificationGroupMember(user_id=test_user.id, is_active=False))
    db_session.commit()
    assert user_has_access(test_user, AccessKey.OPS_VERIFICATION, db_session) is False


# ── explicit per-user overrides win over the role default ────────────


def test_explicit_revoke_beats_permissive_default(test_user, db_session):
    """A buyer with access_overrides={'crm': False} is denied crm (override wins)."""
    test_user.access_overrides = {AccessKey.CRM.value: False}
    db_session.commit()
    assert user_has_access(test_user, AccessKey.CRM) is False
    # A non-overridden module is still allowed by the role default.
    assert user_has_access(test_user, AccessKey.REQUISITIONS) is True


def test_explicit_grant_above_empty_default(db_session):
    """An agent (empty defaults) gains crm only via an explicit override grant."""
    from datetime import datetime

    from app.models import User

    agent = User(
        email="svc-agent@trioscs.com",
        name="Service Agent",
        role=UserRole.AGENT,
        created_at=datetime.now(UTC),
    )
    db_session.add(agent)
    db_session.commit()
    db_session.refresh(agent)

    # Without the override: agent role has empty defaults → denied.
    assert user_has_access(agent, AccessKey.CRM) is False

    # With the override grant: explicitly True → allowed.
    agent.access_overrides = {AccessKey.CRM.value: True}
    db_session.commit()
    assert user_has_access(agent, AccessKey.CRM) is True


def test_access_accepts_str_key(test_user):
    """A raw string access key (the .value) resolves identically to the enum member."""
    assert user_has_access(test_user, "requisitions") is True


# ── audit helper ─────────────────────────────────────────────────────


def test_record_user_audit_writes_row(admin_user, test_user, db_session):
    """record_user_audit appends a UserAdminAudit row with the right fields."""
    record_user_audit(
        db_session,
        actor_id=admin_user.id,
        target_user_id=test_user.id,
        action=UserAuditAction.ROLE_CHANGE,
        detail={"from": "buyer", "to": "manager"},
    )
    db_session.commit()

    row = db_session.query(UserAdminAudit).filter_by(target_user_id=test_user.id).one()
    assert row.actor_id == admin_user.id
    assert row.target_user_id == test_user.id
    assert row.action == UserAuditAction.ROLE_CHANGE.value
    assert row.detail == {"from": "buyer", "to": "manager"}
    assert row.created_at is not None


def test_record_user_audit_defaults_detail_to_empty(admin_user, test_user, db_session):
    """Omitting detail stores an empty dict, never NULL."""
    record_user_audit(
        db_session,
        actor_id=admin_user.id,
        target_user_id=test_user.id,
        action=UserAuditAction.DEACTIVATE,
    )
    db_session.commit()
    row = db_session.query(UserAdminAudit).filter_by(target_user_id=test_user.id).one()
    assert row.detail == {}


# ══════════════════════════════════════════════════════════════════════
# Phase 4a — per-user access panel + nav gating
# ══════════════════════════════════════════════════════════════════════


# ── module_access_map (powers the bottom-nav gate) ───────────────────


class TestModuleAccessMap:
    def test_admin_all_true(self, admin_user):
        from app.routers.admin.users import NAV_ID_TO_ACCESS, module_access_map

        m = module_access_map(admin_user)
        assert set(m) == set(NAV_ID_TO_ACCESS)
        assert all(m.values()), m

    def test_buyer_all_true_by_default(self, test_user):
        from app.routers.admin.users import NAV_ID_TO_ACCESS, module_access_map

        m = module_access_map(test_user)
        assert set(m) == set(NAV_ID_TO_ACCESS)
        assert all(m.values()), m

    def test_override_revokes_single_module(self, test_user, db_session):
        from app.routers.admin.users import module_access_map

        # The map is keyed by hyphenated nav-id; the override is keyed by the
        # AccessKey value ("crm"). crm's nav-id IS "crm" so they coincide here.
        test_user.access_overrides = {AccessKey.CRM.value: False}
        db_session.commit()
        m = module_access_map(test_user)
        assert m["crm"] is False
        # Every other nav module stays visible.
        for nav_id, allowed in m.items():
            if nav_id != "crm":
                assert allowed is True, nav_id

    def test_nav_id_map_covers_every_module_key(self):
        from app.routers.admin.users import NAV_ID_TO_ACCESS

        # Every hyphenated nav-id maps to a module AccessKey, and the set of mapped
        # keys is exactly MODULE_ACCESS_KEYS (no module ungated, none invented).
        assert set(NAV_ID_TO_ACCESS.values()) == set(MODULE_ACCESS_KEYS)
        assert all(isinstance(k, str) and isinstance(v, AccessKey) for k, v in NAV_ID_TO_ACCESS.items())


# ── HTTP fixtures (mirror tests/test_user_management.py) ─────────────


@pytest.fixture()
def admin_client(db_session, admin_user):
    """TestClient authenticated as the admin user (require_admin satisfied)."""
    from app.database import get_db
    from app.dependencies import require_admin, require_user
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: admin_user
    app.dependency_overrides[require_admin] = lambda: admin_user
    try:
        yield TestClient(app)
    finally:
        for dep in (get_db, require_user, require_admin):
            app.dependency_overrides.pop(dep, None)


def _make_user(db, *, email, role="buyer", name=None, is_active=True):
    from datetime import datetime

    from app.models import User

    u = User(
        email=email,
        name=name or email.split("@")[0],
        role=role,
        is_active=is_active,
        created_at=datetime.now(UTC),
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


# ── POST /access — access_overrides round-trip (proves JSON reassignment) ─


class TestAccessOverridePost:
    def test_off_then_default_round_trips_and_flushes(self, admin_client, db_session, test_user):
        # OFF: explicit revoke is persisted AND user_has_access reflects it.
        r = admin_client.post(
            f"/api/admin/users/{test_user.id}/access",
            data={"key": AccessKey.CRM.value, "value": "off"},
        )
        assert r.status_code == 200
        db_session.refresh(test_user)
        assert test_user.access_overrides.get(AccessKey.CRM.value) is False
        assert user_has_access(test_user, AccessKey.CRM) is False
        rows = (
            db_session.query(UserAdminAudit)
            .filter_by(target_user_id=test_user.id, action=UserAuditAction.ACCESS_REVOKE.value)
            .all()
        )
        assert len(rows) == 1
        assert rows[0].detail == {"key": AccessKey.CRM.value, "value": "off"}

        # DEFAULT: the key is popped from the dict → role default applies again.
        r = admin_client.post(
            f"/api/admin/users/{test_user.id}/access",
            data={"key": AccessKey.CRM.value, "value": "default"},
        )
        assert r.status_code == 200
        db_session.refresh(test_user)
        assert AccessKey.CRM.value not in (test_user.access_overrides or {})
        assert user_has_access(test_user, AccessKey.CRM) is True

    def test_on_grants_for_empty_default_role(self, admin_client, db_session):
        # An agent's role default is empty; ON must grant via a flushed override.
        agent = _make_user(db_session, email="svc@trioscs.com", role="agent")
        r = admin_client.post(
            f"/api/admin/users/{agent.id}/access",
            data={"key": AccessKey.EXPORT_DATA.value, "value": "on"},
        )
        assert r.status_code == 200
        db_session.refresh(agent)
        assert agent.access_overrides.get(AccessKey.EXPORT_DATA.value) is True
        assert user_has_access(agent, AccessKey.EXPORT_DATA) is True
        rows = (
            db_session.query(UserAdminAudit)
            .filter_by(target_user_id=agent.id, action=UserAuditAction.ACCESS_GRANT.value)
            .all()
        )
        assert len(rows) == 1

    def test_invalid_key_400(self, admin_client, db_session, test_user):
        r = admin_client.post(
            f"/api/admin/users/{test_user.id}/access",
            data={"key": "not_a_real_key", "value": "on"},
        )
        assert r.status_code == 400


# ── POST /access — ops_verification drives VerificationGroupMember ────


class TestAccessOpsVerification:
    def test_on_then_off_via_membership_not_overrides(self, admin_client, db_session, test_user):
        # ON → creates an active VerificationGroupMember; overrides untouched.
        r = admin_client.post(
            f"/api/admin/users/{test_user.id}/access",
            data={"key": AccessKey.OPS_VERIFICATION.value, "value": "on"},
        )
        assert r.status_code == 200
        db_session.refresh(test_user)
        assert AccessKey.OPS_VERIFICATION.value not in (test_user.access_overrides or {})
        member = db_session.query(VerificationGroupMember).filter_by(user_id=test_user.id).one()
        assert member.is_active is True
        assert user_has_access(test_user, AccessKey.OPS_VERIFICATION, db_session) is True

        # OFF → flips the existing membership inactive (no second row).
        r = admin_client.post(
            f"/api/admin/users/{test_user.id}/access",
            data={"key": AccessKey.OPS_VERIFICATION.value, "value": "off"},
        )
        assert r.status_code == 200
        members = db_session.query(VerificationGroupMember).filter_by(user_id=test_user.id).all()
        assert len(members) == 1
        assert members[0].is_active is False
        db_session.refresh(test_user)
        assert user_has_access(test_user, AccessKey.OPS_VERIFICATION, db_session) is False

    def test_default_treated_as_off(self, admin_client, db_session, test_user):
        db_session.add(VerificationGroupMember(user_id=test_user.id, is_active=True))
        db_session.commit()
        r = admin_client.post(
            f"/api/admin/users/{test_user.id}/access",
            data={"key": AccessKey.OPS_VERIFICATION.value, "value": "default"},
        )
        assert r.status_code == 200
        member = db_session.query(VerificationGroupMember).filter_by(user_id=test_user.id).one()
        assert member.is_active is False


# ── access-panel render + admin-only gating + 404 for agent target ───


class TestAccessPanelRender:
    def test_panel_renders_for_admin(self, admin_client, test_user):
        r = admin_client.get(f"/api/admin/users/{test_user.id}/access-panel")
        assert r.status_code == 200
        assert "Access" in r.text
        # Capability + module labels both present.
        assert "Buy Plans" in r.text
        assert "Send RFQs" in r.text

    def test_panel_404_for_agent_target(self, admin_client, db_session):
        agent = _make_user(db_session, email=_AGENT_EMAIL, role="agent")
        assert admin_client.get(f"/api/admin/users/{agent.id}/access-panel").status_code == 404

    def test_access_404_for_agent_target(self, admin_client, db_session):
        agent = _make_user(db_session, email=_AGENT_EMAIL, role="agent")
        r = admin_client.post(
            f"/api/admin/users/{agent.id}/access",
            data={"key": AccessKey.CRM.value, "value": "off"},
        )
        assert r.status_code == 404


class TestAccessAdminOnly:
    @pytest.mark.parametrize("role", ["buyer", "manager"])
    def test_endpoints_403_for_non_admin(self, db_session, role, monkeypatch, test_user):
        from app.database import get_db
        from app.main import app

        actor = _make_user(db_session, email=f"{role}@gate.test", role=role)
        monkeypatch.setattr("app.dependencies.require_user", lambda request, db: actor)
        app.dependency_overrides[get_db] = lambda: db_session
        try:
            c = TestClient(app)
            assert c.get(f"/api/admin/users/{test_user.id}/access-panel").status_code == 403
            assert (
                c.post(
                    f"/api/admin/users/{test_user.id}/access",
                    data={"key": AccessKey.CRM.value, "value": "off"},
                ).status_code
                == 403
            )
        finally:
            app.dependency_overrides.pop(get_db, None)


# ══════════════════════════════════════════════════════════════════════
# Phase 4b — route access enforcement (require_access wired onto real routes)
# ══════════════════════════════════════════════════════════════════════


def _client_as(db_session, user):
    """A TestClient whose require_user / require_buyer / require_fresh_token all resolve
    to *user*, and whose get_db uses the test session.

    require_access depends on require_user via Depends, so overriding require_user makes
    the per-user access_overrides on *user* flow straight into the gate.
    """
    from app.database import get_db
    from app.dependencies import require_buyer, require_fresh_token, require_user
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: user
    app.dependency_overrides[require_buyer] = lambda: user

    async def _fresh():
        return "mock-token"

    app.dependency_overrides[require_fresh_token] = _fresh
    client = TestClient(app)
    client._overridden = (get_db, require_user, require_buyer, require_fresh_token)
    return client


def _drop_overrides(client):
    from app.main import app

    for dep in getattr(client, "_overridden", ()):  # pragma: no branch
        app.dependency_overrides.pop(dep, None)


# ── module PARTIAL entry routes are gated ────────────────────────────


class TestModulePartialGating:
    def test_crm_shell_403_when_crm_revoked(self, db_session, test_user):
        test_user.access_overrides = {AccessKey.CRM.value: False}
        db_session.commit()
        c = _client_as(db_session, test_user)
        try:
            assert c.get("/v2/partials/crm/shell").status_code == 403
        finally:
            _drop_overrides(c)

    def test_crm_shell_200_for_default_buyer(self, db_session, test_user):
        c = _client_as(db_session, test_user)
        try:
            assert c.get("/v2/partials/crm/shell").status_code == 200
        finally:
            _drop_overrides(c)

    def test_sightings_workspace_403_when_revoked(self, db_session, test_user):
        test_user.access_overrides = {AccessKey.SIGHTINGS.value: False}
        db_session.commit()
        c = _client_as(db_session, test_user)
        try:
            assert c.get("/v2/partials/sightings/workspace").status_code == 403
        finally:
            _drop_overrides(c)

    def test_resell_workspace_403_when_revoked(self, db_session, test_user):
        test_user.access_overrides = {AccessKey.RESELL.value: False}
        db_session.commit()
        c = _client_as(db_session, test_user)
        try:
            assert c.get("/v2/partials/resell/workspace").status_code == 403
        finally:
            _drop_overrides(c)

    def test_materials_workspace_403_when_revoked(self, db_session, test_user):
        test_user.access_overrides = {AccessKey.MATERIALS.value: False}
        db_session.commit()
        c = _client_as(db_session, test_user)
        try:
            assert c.get("/v2/partials/materials/workspace").status_code == 403
        finally:
            _drop_overrides(c)

    def test_admin_never_blocked_on_partial(self, db_session, admin_user):
        # An admin with a stray override is still admin → user_has_access short-circuits True.
        admin_user.access_overrides = {AccessKey.CRM.value: False}
        db_session.commit()
        c = _client_as(db_session, admin_user)
        try:
            assert c.get("/v2/partials/crm/shell").status_code == 200
        finally:
            _drop_overrides(c)


# ── v2_page full-page module gate (redirect to first allowed) ─────────


class TestV2PageGating:
    """v2_page reads get_user(request, db) directly (NOT the require_user override), so
    we monkeypatch the bound name on the router module to authenticate."""

    def _patch_user(self, monkeypatch, db_session, user):
        from app.database import get_db
        from app.main import app

        monkeypatch.setattr("app.routers.htmx_views.get_user", lambda request, db: user)
        app.dependency_overrides[get_db] = lambda: db_session
        return TestClient(app)

    def _unpatch(self):
        from app.database import get_db
        from app.main import app

        app.dependency_overrides.pop(get_db, None)

    def test_crm_page_redirects_when_crm_revoked(self, db_session, test_user, monkeypatch):
        test_user.access_overrides = {AccessKey.CRM.value: False}
        db_session.commit()
        c = self._patch_user(monkeypatch, db_session, test_user)
        try:
            r = c.get("/v2/crm", follow_redirects=False)
            assert r.status_code == 302
            # Redirect target is an allowed module — requisitions is first in MODULE order.
            assert r.headers["location"] == "/v2/requisitions"
        finally:
            self._unpatch()

    def test_crm_page_200_for_default_buyer(self, db_session, test_user, monkeypatch):
        c = self._patch_user(monkeypatch, db_session, test_user)
        try:
            assert c.get("/v2/crm", follow_redirects=False).status_code == 200
        finally:
            self._unpatch()

    def test_redirect_skips_revoked_first_module(self, db_session, test_user, monkeypatch):
        # Requisitions revoked too → redirect lands on the next allowed module (sightings).
        test_user.access_overrides = {
            AccessKey.CRM.value: False,
            AccessKey.REQUISITIONS.value: False,
        }
        db_session.commit()
        c = self._patch_user(monkeypatch, db_session, test_user)
        try:
            r = c.get("/v2/crm", follow_redirects=False)
            assert r.status_code == 302
            assert r.headers["location"] == "/v2/sightings"
        finally:
            self._unpatch()

    def test_no_modules_allowed_returns_403(self, db_session, monkeypatch):
        # A non-admin agent (empty role defaults, no grants) → no module allowed → 403.
        agent = _make_user(db_session, email="locked@trioscs.com", role="agent")
        c = self._patch_user(monkeypatch, db_session, agent)
        try:
            r = c.get("/v2/crm", follow_redirects=False)
            assert r.status_code == 403
            assert "don't have access" in r.text.lower() or "logout" in r.text.lower()
        finally:
            self._unpatch()

    def test_ungated_view_not_redirected(self, db_session, test_user, monkeypatch):
        # settings is not in _VIEW_ACCESS → never gated even with everything revoked.
        test_user.access_overrides = {k.value: False for k in MODULE_ACCESS_KEYS}
        db_session.commit()
        c = self._patch_user(monkeypatch, db_session, test_user)
        try:
            assert c.get("/v2/settings", follow_redirects=False).status_code == 200
        finally:
            self._unpatch()

    def test_admin_never_redirected(self, db_session, admin_user, monkeypatch):
        admin_user.access_overrides = {AccessKey.CRM.value: False}
        db_session.commit()
        c = self._patch_user(monkeypatch, db_session, admin_user)
        try:
            assert c.get("/v2/crm", follow_redirects=False).status_code == 200
        finally:
            self._unpatch()


# ── capability endpoints are gated ───────────────────────────────────


def _make_pending_offer(db, req):
    from datetime import datetime

    from app.models import Offer

    o = Offer(
        requisition_id=req.id,
        requirement_id=req.requirements[0].id,
        vendor_name="Arrow Electronics",
        mpn=req.requirements[0].primary_mpn,
        status="pending_review",
        entered_by_id=req.created_by,
        created_at=datetime.now(UTC),
    )
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


class TestCapabilityGating:
    def test_export_companies_403_for_default_buyer(self, db_session, test_user):
        # ISS-022: bulk dataset exports (companies/contacts/vendors/requisitions/
        # sightings) moved off EXPORT_DATA onto manager+admin-only EXPORT_BULK_DATA —
        # see tests/test_export_bulk_data_gate.py for the full role matrix.
        c = _client_as(db_session, test_user)
        try:
            assert c.get("/v2/customers/export.csv").status_code == 403
        finally:
            _drop_overrides(c)

    def test_export_companies_200_for_manager_default(self, db_session, manager_user):
        # Manager holds EXPORT_BULK_DATA by default (ISS-022).
        c = _client_as(db_session, manager_user)
        try:
            assert c.get("/v2/customers/export.csv").status_code == 200
        finally:
            _drop_overrides(c)

    def test_export_companies_403_when_export_bulk_data_revoked_for_manager(self, db_session, manager_user):
        manager_user.access_overrides = {AccessKey.EXPORT_BULK_DATA.value: False}
        db_session.commit()
        c = _client_as(db_session, manager_user)
        try:
            assert c.get("/v2/customers/export.csv").status_code == 403
        finally:
            _drop_overrides(c)

    def test_approve_offer_403_when_capability_revoked(self, db_session, test_user, test_requisition):
        test_user.access_overrides = {AccessKey.APPROVE_OFFERS.value: False}
        db_session.commit()
        offer = _make_pending_offer(db_session, test_requisition)
        c = _client_as(db_session, test_user)
        try:
            assert c.put(f"/api/offers/{offer.id}/approve").status_code == 403
        finally:
            _drop_overrides(c)

    def test_approve_offer_works_for_default_buyer(self, db_session, test_user, test_requisition):
        offer = _make_pending_offer(db_session, test_requisition)
        c = _client_as(db_session, test_user)
        try:
            r = c.put(f"/api/offers/{offer.id}/approve")
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "active"
        finally:
            _drop_overrides(c)

    def test_sightings_review_offer_403_when_approve_revoked(self, db_session, test_user):
        # The Sightings offers panel is an alternate entry point that approves/rejects
        # offers — it must honor the same approve_offers gate (no bypass).
        test_user.access_overrides = {AccessKey.APPROVE_OFFERS.value: False}
        db_session.commit()
        c = _client_as(db_session, test_user)
        try:
            # Gate fires before path/body resolution → 403 with dummy ids.
            assert c.post("/v2/partials/sightings/1/offers/1/review").status_code == 403
        finally:
            _drop_overrides(c)

    @pytest.mark.parametrize(
        "path",
        [
            "/v2/partials/offers/1/promote",  # review-queue HTMX promote
            "/v2/partials/offers/1/reject",  # review-queue HTMX reject
            "/api/offers/1/promote",  # review-queue JSON promote (T4→T5)
            "/api/offers/1/reject",  # review-queue JSON reject
        ],
    )
    def test_review_queue_promote_reject_403_when_approve_revoked(self, db_session, test_user, path):
        # The review-queue promote/reject endpoints perform the same pending_review →
        # active/rejected transition as approve_offer — they must honor approve_offers
        # (no bypass via the review queue). Gate fires before db.get → 403 with dummy id.
        test_user.access_overrides = {AccessKey.APPROVE_OFFERS.value: False}
        db_session.commit()
        c = _client_as(db_session, test_user)
        try:
            assert c.post(path).status_code == 403
        finally:
            _drop_overrides(c)

    def test_sightings_reconfirm_offer_403_when_approve_revoked(self, db_session, test_user):
        test_user.access_overrides = {AccessKey.APPROVE_OFFERS.value: False}
        db_session.commit()
        c = _client_as(db_session, test_user)
        try:
            assert c.post("/v2/partials/sightings/1/offers/1/reconfirm").status_code == 403
        finally:
            _drop_overrides(c)

    def test_send_inquiry_403_when_send_rfq_revoked(self, db_session, test_user):
        test_user.access_overrides = {AccessKey.SEND_RFQ.value: False}
        db_session.commit()
        c = _client_as(db_session, test_user)
        try:
            # Gate runs before the handler body / form parsing → 403, not 422.
            assert c.post("/v2/partials/sightings/send-inquiry").status_code == 403
        finally:
            _drop_overrides(c)

    def test_test_api_source_403_when_manage_connectors_revoked(self, db_session, test_user):
        test_user.access_overrides = {AccessKey.MANAGE_CONNECTORS.value: False}
        db_session.commit()
        c = _client_as(db_session, test_user)
        try:
            # Source id need not exist — the gate fires before db.get(ApiSource).
            assert c.post("/api/sources/999999/test").status_code == 403
        finally:
            _drop_overrides(c)

    def test_admin_never_blocked_on_capability(self, db_session, admin_user):
        admin_user.access_overrides = {AccessKey.EXPORT_DATA.value: False}
        db_session.commit()
        c = _client_as(db_session, admin_user)
        try:
            assert c.get("/v2/customers/export.csv").status_code == 200
        finally:
            _drop_overrides(c)
