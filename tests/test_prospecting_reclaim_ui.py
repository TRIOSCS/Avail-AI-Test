"""tests/test_prospecting_reclaim_ui.py — Reclaim/reassign UI wiring for swept
prospects.

Covers finding DC-02: the account-sweep email tells users to "reclaim from the
Prospecting tab", but the tab had no reclaim/reassign controls. These tests pin the
buttons that now reach the EXISTING /reclaim and /reassign endpoints:

  - A swept card/detail shows Reclaim (not the generic Claim) to the former owner.
  - An unrelated rep still sees the plain Claim button (no reclaim/reassign).
  - A former owner inside the 30-day cooldown sees a disabled Reclaim with the date.
  - Managers/admins get a Reassign button + a working user-picker modal.
  - The reassign modal is manager-gated and the action reassigns + dismisses.

Called by: pytest autodiscovery
Depends on: conftest.py fixtures (client, db_session, test_user, manager_user, admin_user)
"""

import os

os.environ["TESTING"] = "1"

import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.dependencies import require_user
from app.main import app
from app.models import Company
from app.models.prospect_account import ProspectAccount


def _act_as(user):
    """Point require_user (and everything that Depends on it) at *user* for this test.

    The client fixture pops require_user in teardown, so no manual restore is needed.
    """
    app.dependency_overrides[require_user] = lambda: user


def make_company(db: Session, owner_id: int) -> Company:
    c = Company(
        name=f"Swept Co {uuid.uuid4().hex[:6]}",
        domain=f"co-{uuid.uuid4().hex[:6]}.com",
        is_active=True,
        account_owner_id=owner_id,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def make_swept(
    db: Session, *, swept_from_owner_id: int, blocked_until=None, company: Company | None = None
) -> ProspectAccount:
    """A suggested prospect parked back in the pool by the auto-sweep."""
    p = ProspectAccount(
        name=f"Swept {uuid.uuid4().hex[:6]}",
        domain=f"p-{uuid.uuid4().hex[:6]}.com",
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


# ── Card / detail render ──────────────────────────────────────────────────


class TestReclaimRender:
    def test_swept_card_shows_reclaim_for_former_owner(self, client, db_session, test_user):
        co = make_company(db_session, owner_id=test_user.id)
        p = make_swept(db_session, swept_from_owner_id=test_user.id, company=co)
        resp = client.get("/v2/partials/prospecting")
        assert resp.status_code == 200
        assert f"/v2/partials/prospects/{p.id}/reclaim" in resp.text
        # The generic claim path is replaced by reclaim for the former owner.
        assert f"/v2/partials/prospecting/{p.id}/claim" not in resp.text

    def test_swept_detail_shows_reclaim_for_former_owner(self, client, db_session, test_user):
        co = make_company(db_session, owner_id=test_user.id)
        p = make_swept(db_session, swept_from_owner_id=test_user.id, company=co)
        resp = client.get(f"/v2/partials/prospecting/{p.id}")
        assert resp.status_code == 200
        assert f"/v2/partials/prospects/{p.id}/reclaim" in resp.text
        assert "Reclaim" in resp.text

    def test_unrelated_rep_sees_plain_claim_not_reclaim(self, client, db_session, test_user, manager_user):
        # Swept from a DIFFERENT owner; acting user (buyer) is neither former owner nor manager.
        co = make_company(db_session, owner_id=manager_user.id)
        p = make_swept(db_session, swept_from_owner_id=manager_user.id, company=co)
        resp = client.get("/v2/partials/prospecting")
        assert resp.status_code == 200
        assert f"/v2/partials/prospects/{p.id}/reclaim" not in resp.text
        assert f"/v2/partials/prospects/{p.id}/reassign-form" not in resp.text
        assert f"/v2/partials/prospecting/{p.id}/claim" in resp.text

    def test_former_owner_in_cooldown_sees_disabled_reclaim(self, client, db_session, test_user):
        co = make_company(db_session, owner_id=test_user.id)
        blocked = datetime.now(timezone.utc) + timedelta(days=30)
        p = make_swept(db_session, swept_from_owner_id=test_user.id, blocked_until=blocked, company=co)
        resp = client.get("/v2/partials/prospecting")
        assert resp.status_code == 200
        assert "In cooldown until" in resp.text
        assert "Reclaim" in resp.text
        # Disabled button carries no POST wiring — no dead click, honest state instead.
        assert f'hx-post="/v2/partials/prospects/{p.id}/reclaim"' not in resp.text

    def test_manager_sees_reassign_button(self, client, db_session, test_user, manager_user):
        _act_as(manager_user)
        co = make_company(db_session, owner_id=test_user.id)
        p = make_swept(db_session, swept_from_owner_id=test_user.id, company=co)
        resp = client.get(f"/v2/partials/prospecting/{p.id}")
        assert resp.status_code == 200
        assert f"/v2/partials/prospects/{p.id}/reassign-form" in resp.text
        assert "Reassign" in resp.text


# ── Reassign modal (user picker) ──────────────────────────────────────────


class TestReassignForm:
    def test_reassign_form_forbidden_for_non_manager(self, client, db_session, test_user):
        co = make_company(db_session, owner_id=test_user.id)
        p = make_swept(db_session, swept_from_owner_id=test_user.id, company=co)
        resp = client.get(f"/v2/partials/prospects/{p.id}/reassign-form")
        assert resp.status_code == 403

    def test_reassign_form_renders_user_options_for_manager(
        self, client, db_session, test_user, manager_user, admin_user
    ):
        _act_as(manager_user)
        co = make_company(db_session, owner_id=test_user.id)
        p = make_swept(db_session, swept_from_owner_id=test_user.id, company=co)
        resp = client.get(f"/v2/partials/prospects/{p.id}/reassign-form?ctx=detail")
        assert resp.status_code == 200
        assert 'name="to_user_id"' in resp.text
        assert f"/v2/partials/prospects/{p.id}/reassign" in resp.text
        # Active users are offered as reassignment targets.
        assert admin_user.name in resp.text


# ── Actions (behavioral) ──────────────────────────────────────────────────


class TestReclaimAction:
    def test_reclaim_route_assigns_owner_and_dismisses(self, client, db_session, test_user):
        co = make_company(db_session, owner_id=test_user.id)
        p = make_swept(db_session, swept_from_owner_id=test_user.id, company=co)
        resp = client.post(f"/v2/partials/prospects/{p.id}/reclaim", data={"flt_status": ""})
        assert resp.status_code == 200
        db_session.refresh(co)
        db_session.refresh(p)
        assert co.account_owner_id == test_user.id
        assert p.status == "dismissed"


class TestReassignAction:
    def test_reassign_route_reassigns_and_clears_cooldown(
        self, client, db_session, test_user, manager_user, admin_user
    ):
        _act_as(manager_user)
        co = make_company(db_session, owner_id=test_user.id)
        blocked = datetime.now(timezone.utc) + timedelta(days=30)
        p = make_swept(db_session, swept_from_owner_id=test_user.id, blocked_until=blocked, company=co)
        resp = client.post(
            f"/v2/partials/prospects/{p.id}/reassign",
            data={"to_user_id": admin_user.id, "flt_status": ""},
        )
        assert resp.status_code == 200
        db_session.refresh(co)
        db_session.refresh(p)
        assert co.account_owner_id == admin_user.id
        assert p.status == "dismissed"
        assert p.reclaim_blocked_until is None

    def test_reassign_route_forbidden_for_non_manager_shows_error_toast(self, client, db_session, test_user):
        """A denied reassign must surface an error toast, not a silent suppressed 4xx.

        HTMX drops non-2xx swaps and the JSON error handler carries no showToast, so the
        old bare ``HTTPException(403)`` left the reassign modal open with ZERO feedback.
        The honest response is a 200 + error showToast (HX-Reswap:none so nothing swaps),
        and the reassign must not have happened.
        """
        co = make_company(db_session, owner_id=test_user.id)
        p = make_swept(db_session, swept_from_owner_id=test_user.id, company=co)
        resp = client.post(
            f"/v2/partials/prospects/{p.id}/reassign",
            data={"to_user_id": test_user.id, "flt_status": ""},
        )
        assert resp.status_code == 200
        trigger = json.loads(resp.headers["HX-Trigger"])
        assert trigger["showToast"]["type"] == "error"
        assert "manager or admin" in trigger["showToast"]["message"]
        # Card is left in place (no silent removal): the swap is suppressed.
        assert resp.headers.get("HX-Reswap") == "none"
        db_session.refresh(co)
        assert co.account_owner_id == test_user.id  # ownership unchanged

    def test_reassign_route_no_company_shows_error_toast(self, client, db_session, test_user, manager_user):
        """A swept prospect with no linked company can't be reassigned; a manager must
        get an honest error toast rather than a silently-suppressed 400 that no-ops the
        modal."""
        _act_as(manager_user)
        p = make_swept(db_session, swept_from_owner_id=test_user.id, company=None)
        resp = client.post(
            f"/v2/partials/prospects/{p.id}/reassign",
            data={"to_user_id": test_user.id, "flt_status": ""},
        )
        assert resp.status_code == 200
        trigger = json.loads(resp.headers["HX-Trigger"])
        assert trigger["showToast"]["type"] == "error"
        assert "not linked to a company" in trigger["showToast"]["message"]
