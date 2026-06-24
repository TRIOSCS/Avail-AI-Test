"""test_access_control.py — Unit tests for the access-control foundation.

Covers the Phase 1 (Foundation) primitives of the user-management feature:
- user_has_access(): admin-all, role defaults, explicit per-user overrides,
  and the ops_verification delegation to VerificationGroupMember.
- record_user_audit(): appends a UserAdminAudit row the caller commits.

These are pure-unit tests against the shared in-memory SQLite session
(conftest db_session + role-user fixtures). No HTTP / dependency overrides.

Called by: pytest autodiscovery
Depends on: app.dependencies, app.constants, app.services.user_admin, app.models
"""

import pytest

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
    """send_rfq / approve_offers / export_data / manage_connectors True by default."""
    user = request.getfixturevalue(role_fixture)
    for key in (
        AccessKey.SEND_RFQ,
        AccessKey.APPROVE_OFFERS,
        AccessKey.EXPORT_DATA,
        AccessKey.MANAGE_CONNECTORS,
    ):
        assert user_has_access(user, key) is True, f"{user.role} denied capability {key}"


def test_capability_keys_constant_matches_expected_set():
    """CAPABILITY_ACCESS_KEYS is the five capability keys (registry shape guard)."""
    assert set(CAPABILITY_ACCESS_KEYS) == {
        AccessKey.SEND_RFQ,
        AccessKey.APPROVE_OFFERS,
        AccessKey.EXPORT_DATA,
        AccessKey.MANAGE_CONNECTORS,
        AccessKey.OPS_VERIFICATION,
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
    from datetime import datetime, timezone

    from app.models import User

    agent = User(
        email="svc-agent@trioscs.com",
        name="Service Agent",
        role=UserRole.AGENT,
        created_at=datetime.now(timezone.utc),
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
