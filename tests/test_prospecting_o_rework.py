"""tests/test_prospecting_o_rework.py — Prospecting "O" rework.

Confirmed decisions (backlog item O + PLANNING DECISIONS 2026-07-05):
  1. Pool per-account actions = Claim + Dismiss ONLY — the Reclaim + Reassign controls are
     removed from the pool UI (card + detail).
  2. Claim -> account owned by the claimer, present on the CRM (Customers) tab under them,
     and gone from the suggested/claimable pool.
  3. A manager-only "Assign to rep" action -> rep picker -> assigns the account to the chosen
     rep (owned by them in CRM, gone from the pool). Non-managers get a 403. This subsumes the
     sweep "put-it-back" (a manager can assign a swept account to any rep incl. the original).

Called by: pytest autodiscovery
Depends on: conftest.py fixtures (client, db_session, test_user, sales_user, manager_user,
            admin_user), app.routers.htmx.prospecting, app.services.prospect_claim,
            app.services.crm_service.cdm_company_query
"""

import os

os.environ["TESTING"] = "1"

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import Session

from app.constants import ProspectAccountStatus
from app.dependencies import require_user
from app.main import app
from app.models import Company, User
from app.models.prospect_account import ProspectAccount
from app.services.crm_service import cdm_company_query


def _act_as(user):
    """Point require_user (and everything that Depends on it) at *user* for this test.

    The client fixture pops require_user in teardown, so no manual restore is needed.
    """
    app.dependency_overrides[require_user] = lambda: user


def make_prospect(db: Session, *, status: str = "suggested", **kw) -> ProspectAccount:
    defaults = dict(
        name=f"O-Prospect-{uuid.uuid4().hex[:6]}",
        domain=f"o-{uuid.uuid4().hex[:6]}.com",
        status=status,
        fit_score=70,
        readiness_score=55,
        discovery_source="manual",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    p = ProspectAccount(**defaults)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def make_company(db: Session, *, owner_id: int | None = None) -> Company:
    c = Company(
        name=f"O-Co-{uuid.uuid4().hex[:6]}",
        domain=f"co-{uuid.uuid4().hex[:6]}.com",
        is_active=True,
        account_owner_id=owner_id,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def make_swept(db: Session, *, swept_from_owner_id: int, company: Company | None = None, blocked_until=None):
    """A suggested prospect parked back in the pool by the auto-sweep (owner
    cleared)."""
    p = ProspectAccount(
        name=f"Swept-{uuid.uuid4().hex[:6]}",
        domain=f"sw-{uuid.uuid4().hex[:6]}.com",
        status="suggested",
        fit_score=70,
        readiness_score=55,
        discovery_source="auto_sweep",
        swept_from_owner_id=swept_from_owner_id,
        swept_at=datetime.now(timezone.utc),
        reclaim_blocked_until=blocked_until,
        company_id=company.id if company else None,
        created_at=datetime.now(timezone.utc),
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _crm_owned_names(db: Session, user: User) -> set[str]:
    """Names of CRM Companies *user* owns (the my_only scope of the Customers tab)."""
    rows = cdm_company_query(db, user, search="", staleness="", account_type="", my_only=True, sort="oldest").all()
    return {c.name for c in rows}


# ── Pool UI: Claim + Dismiss only; no Reclaim/Reassign; manager-only Assign ──


class TestPoolUiActions:
    def test_suggested_card_shows_claim_and_dismiss_only_for_rep(self, client, db_session, test_user):
        p = make_prospect(db_session)
        resp = client.get("/v2/partials/prospecting")
        assert resp.status_code == 200
        assert f"/v2/partials/prospecting/{p.id}/claim" in resp.text
        assert f"/v2/partials/prospecting/{p.id}/dismiss" in resp.text
        # The retired controls are gone from the pool UI.
        assert "/reclaim" not in resp.text
        assert "reassign" not in resp.text
        assert "Reclaim" not in resp.text
        assert "Reassign" not in resp.text
        # A rep (non-manager) sees no Assign control.
        assert "assign-form" not in resp.text

    def test_swept_card_no_reclaim_for_former_owner(self, client, db_session, test_user):
        co = make_company(db_session)  # owner cleared by sweep
        p = make_swept(db_session, swept_from_owner_id=test_user.id, company=co)
        resp = client.get("/v2/partials/prospecting")
        assert resp.status_code == 200
        assert "/reclaim" not in resp.text
        assert "Reclaim" not in resp.text
        # The former owner now just sees the plain Claim — the pool is unassigned accounts.
        assert f"/v2/partials/prospecting/{p.id}/claim" in resp.text

    def test_swept_detail_no_reclaim_or_reassign(self, client, db_session, test_user):
        co = make_company(db_session)
        p = make_swept(db_session, swept_from_owner_id=test_user.id, company=co)
        resp = client.get(f"/v2/partials/prospecting/{p.id}")
        assert resp.status_code == 200
        assert "Reclaim" not in resp.text
        assert "Reassign" not in resp.text
        assert "/reclaim" not in resp.text
        assert f"/v2/partials/prospecting/{p.id}/claim" in resp.text

    def test_manager_sees_assign_control_on_card(self, client, db_session, manager_user):
        _act_as(manager_user)
        p = make_prospect(db_session)
        resp = client.get("/v2/partials/prospecting")
        assert resp.status_code == 200
        assert f"/v2/partials/prospects/{p.id}/assign-form" in resp.text
        assert "Assign to rep" in resp.text

    def test_manager_sees_assign_control_on_detail(self, client, db_session, manager_user):
        _act_as(manager_user)
        p = make_prospect(db_session)
        resp = client.get(f"/v2/partials/prospecting/{p.id}")
        assert resp.status_code == 200
        assert f"/v2/partials/prospects/{p.id}/assign-form" in resp.text
        assert "Assign to rep" in resp.text


# ── Claim -> owner=claimer + in CRM + gone from pool ──────────────────────


class TestClaimToCrm:
    def test_claim_sets_owner_in_crm_and_leaves_pool(self, client, db_session, test_user):
        p = make_prospect(db_session)
        with patch("app.services.prospect_claim.trigger_deep_enrichment_bg", new_callable=AsyncMock):
            resp = client.post(f"/v2/partials/prospecting/{p.id}/claim")
        assert resp.status_code == 200

        db_session.refresh(p)
        assert p.status == ProspectAccountStatus.CLAIMED
        assert p.claimed_by == test_user.id
        assert p.company_id is not None

        co = db_session.get(Company, p.company_id)
        assert co.account_owner_id == test_user.id
        assert co.is_active is True
        # Appears on the CRM (Customers) tab, owned by the claimer.
        assert co.name in _crm_owned_names(db_session, test_user)

        # Gone from the suggested/claimable pool.
        assert p.status != ProspectAccountStatus.SUGGESTED
        sugg = client.get("/v2/partials/prospecting?status=suggested")
        assert f"/v2/partials/prospecting/{p.id}/claim" not in sugg.text


class TestDismiss:
    def test_dismiss_removes_from_pool(self, client, db_session, test_user):
        p = make_prospect(db_session)
        resp = client.post(f"/v2/partials/prospecting/{p.id}/dismiss", data={"reason": "other"})
        assert resp.status_code == 200
        db_session.refresh(p)
        assert p.status == ProspectAccountStatus.DISMISSED
        sugg = client.get("/v2/partials/prospecting?status=suggested")
        assert f"/v2/partials/prospecting/{p.id}/claim" not in sugg.text


# ── Manager Assign -> owner=chosen rep + in CRM + gone from pool ──────────


class TestManagerAssign:
    def test_assign_companyless_prospect_creates_rep_owned_crm_account(
        self, client, db_session, test_user, manager_user
    ):
        _act_as(manager_user)
        p = make_prospect(db_session)  # no company_id
        with patch("app.services.prospect_claim.trigger_deep_enrichment_bg", new_callable=AsyncMock):
            resp = client.post(
                f"/v2/partials/prospects/{p.id}/assign",
                data={"to_user_id": str(test_user.id), "flt_status": ""},
            )
        assert resp.status_code == 200

        db_session.refresh(p)
        assert p.status == ProspectAccountStatus.CLAIMED
        assert p.claimed_by == test_user.id  # owned by the CHOSEN rep, not the manager
        assert p.company_id is not None

        co = db_session.get(Company, p.company_id)
        assert co.account_owner_id == test_user.id
        assert co.name in _crm_owned_names(db_session, test_user)

        # Gone from the suggested pool.
        sugg = client.get("/v2/partials/prospecting?status=suggested")
        assert f"/v2/partials/prospecting/{p.id}/claim" not in sugg.text

    def test_assign_swept_prospect_transfers_owner_and_clears_cooldown(
        self, client, db_session, test_user, manager_user
    ):
        _act_as(manager_user)
        co = make_company(db_session)  # owner cleared by sweep
        blocked = datetime.now(timezone.utc) + timedelta(days=30)
        p = make_swept(db_session, swept_from_owner_id=test_user.id, company=co, blocked_until=blocked)
        with patch("app.services.prospect_claim.trigger_deep_enrichment_bg", new_callable=AsyncMock):
            resp = client.post(
                f"/v2/partials/prospects/{p.id}/assign",
                data={"to_user_id": str(test_user.id), "flt_status": ""},
            )
        assert resp.status_code == 200

        db_session.refresh(co)
        db_session.refresh(p)
        assert co.account_owner_id == test_user.id  # PATH A: existing company transferred
        assert p.status == ProspectAccountStatus.CLAIMED
        assert p.reclaim_blocked_until is None  # manager assignment ends the sweep cooldown
        assert co.name in _crm_owned_names(db_session, test_user)


# ── Assign gating: non-managers must NOT see/POST it (403) ────────────────


class TestAssignGating:
    def test_non_manager_assign_post_is_forbidden(self, client, db_session, sales_user):
        # The default `client` is authenticated as a buyer (test_user) — not a manager.
        p = make_prospect(db_session)
        resp = client.post(
            f"/v2/partials/prospects/{p.id}/assign",
            data={"to_user_id": str(sales_user.id), "flt_status": ""},
        )
        assert resp.status_code == 403
        db_session.refresh(p)
        assert p.status == ProspectAccountStatus.SUGGESTED  # nothing changed

    def test_non_manager_assign_form_is_forbidden(self, client, db_session):
        p = make_prospect(db_session)
        resp = client.get(f"/v2/partials/prospects/{p.id}/assign-form")
        assert resp.status_code == 403

    def test_manager_assign_form_renders_rep_picker(self, client, db_session, manager_user, admin_user):
        _act_as(manager_user)
        p = make_prospect(db_session)
        resp = client.get(f"/v2/partials/prospects/{p.id}/assign-form?ctx=detail")
        assert resp.status_code == 200
        assert 'name="to_user_id"' in resp.text
        assert f"/v2/partials/prospects/{p.id}/assign" in resp.text
        # Active users are offered as assignment targets.
        assert admin_user.name in resp.text


# ── Assign error paths: honest error toast, no mutation ───────────────────


class TestAssignErrors:
    def test_assign_inactive_target_shows_error_toast(self, client, db_session, manager_user):
        _act_as(manager_user)
        inactive = User(
            email=f"inactive-{uuid.uuid4().hex[:6]}@trioscs.com",
            name="Inactive Rep",
            role="sales",
            is_active=False,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(inactive)
        db_session.commit()
        db_session.refresh(inactive)

        p = make_prospect(db_session)
        resp = client.post(
            f"/v2/partials/prospects/{p.id}/assign",
            data={"to_user_id": str(inactive.id), "flt_status": ""},
        )
        assert resp.status_code == 200
        trigger = json.loads(resp.headers["HX-Trigger"])
        assert trigger["showToast"]["type"] == "error"
        assert "inactive" in trigger["showToast"]["message"]
        # Modal keeps its context (nothing swaps) and nothing was assigned.
        assert resp.headers.get("HX-Reswap") == "none"
        db_session.refresh(p)
        assert p.status == ProspectAccountStatus.SUGGESTED

    def test_assign_company_owned_by_another_shows_error_toast(
        self, client, db_session, manager_user, admin_user, test_user
    ):
        _act_as(manager_user)
        co = make_company(db_session, owner_id=admin_user.id)  # already owned
        p = make_prospect(db_session, company_id=co.id)
        resp = client.post(
            f"/v2/partials/prospects/{p.id}/assign",
            data={"to_user_id": str(test_user.id), "flt_status": ""},
        )
        assert resp.status_code == 200
        trigger = json.loads(resp.headers["HX-Trigger"])
        assert trigger["showToast"]["type"] == "error"
        assert "already owned" in trigger["showToast"]["message"]
        db_session.refresh(co)
        assert co.account_owner_id == admin_user.id  # unchanged


# ── Access-path gating: the assign endpoints require the PROSPECTING key ──


class TestAssignAccessPath:
    def test_assign_paths_require_prospecting_key(self):
        from app.access_paths import module_key_for_path
        from app.constants import AccessKey

        assert module_key_for_path("/v2/partials/prospects/9/assign") == AccessKey.PROSPECTING
        assert module_key_for_path("/v2/partials/prospects/9/assign-form") == AccessKey.PROSPECTING
