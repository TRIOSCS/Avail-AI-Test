"""tests/test_po_confirm_gate.py — A9: confirm-PO gated to the line's assigned buyer or
a manager/admin (service + route + pane + AI paste-prefill).

Before this fix, ANY viewer with plan access (e.g. a co-owner sales/trader on the same
requisition) could submit the confirm-PO form for someone else's line — the service had
no actor check at all. This closes that gap by mirroring the exact buyer-or-manager
idiom ``mark_line_received`` already enforces (buyplan_po.py:126) inside ``confirm_po``,
and gives the AI paste-prefill route (which never calls confirm_po — it only pre-fills
the form, no DB write) its own copy of the same gate.

Covers:
  - SERVICE: confirm_po raises PermissionError for a non-assigned non-manager; an
    assigned buyer and a manager/admin both proceed.
  - ROUTE: POSTing the confirm form as a non-assigned, non-manager viewer → 403, line
    unchanged; a manager acting for the buyer → 200.
  - PANE: a non-assigned viewer's pane shows the read-only "Awaiting PO — assigned to
    {buyer}" card and NOT the confirm form; a manager's pane shows the same card plus
    an "Act for the buyer" expander that reveals the form; the assigned buyer still
    sees the form outright.
  - parse-confirmation: refuses the same non-assigned, non-manager user, with no AI
    call burned.

Called by: pytest
Depends on: conftest (db_session, test_user, manager_user, client, manager_client),
    tests.test_approvals_hub_tabs builders (_req_quote, _plan, _line),
    app.services.buyplan_workflow (confirm_po).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import BuyPlanLineStatus, BuyPlanStatus
from app.database import get_db
from app.dependencies import require_user
from app.models import User
from app.services.buyplan_workflow import confirm_po
from tests.test_approvals_hub_tabs import _line, _plan, _req_quote

# ── Fixtures / builders ──────────────────────────────────────────────────


@pytest.fixture()
def other_buyer(db_session: Session) -> User:
    """A second buyer-role user, distinct from test_user — the line's ACTUAL assigned
    buyer in the gap scenarios below."""
    user = User(
        email="otherbuyer@trioscs.com",
        name="Other Buyer",
        role="buyer",
        azure_id=f"test-azure-id-other-buyer-{uuid.uuid4().hex[:8]}",
        created_at=datetime.now(UTC),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _line_assigned_to_other(db: Session, owner: User, other: User, **overrides) -> tuple:
    """An ACTIVE plan OWNED by *owner* (so per-record ownership/get_buyplan_for_user
    passes) whose AWAITING_PO line is assigned to *other* — the exact non-assigned-
    non-manager gap A9 closes: the viewer has plan access but is not the line's buyer."""
    req, q, rq = _req_quote(db, owner)
    bp = _plan(db, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _line(db, bp, rq, owner, status=BuyPlanLineStatus.AWAITING_PO.value, buyer_id=other.id, **overrides)
    db.commit()
    return bp, line


def _client_as(db_session: Session, user: User) -> TestClient:
    """A raw TestClient authed as *user* — for actors with no dedicated conftest fixture
    (the line's own assigned buyer, who is a fresh per-test user here)."""
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: user
    return TestClient(app)


def _pop_overrides():
    from app.main import app

    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(require_user, None)


# ── (a) SERVICE gate ─────────────────────────────────────────────────────


class TestConfirmPoServiceGate:
    def test_non_assigned_non_manager_blocked(self, db_session: Session, test_user: User, other_buyer: User):
        bp, line = _line_assigned_to_other(db_session, test_user, other_buyer)

        with pytest.raises(PermissionError, match="buyer or a manager"):
            confirm_po(bp.id, line.id, "PO-HACK", datetime.now(UTC), test_user, db_session, payment_method="wire")
        db_session.rollback()
        db_session.refresh(line)
        assert line.status == BuyPlanLineStatus.AWAITING_PO.value
        assert line.po_number is None

    def test_assigned_buyer_proceeds(self, db_session: Session, test_user: User, other_buyer: User):
        bp, line = _line_assigned_to_other(db_session, test_user, other_buyer)

        updated = confirm_po(
            bp.id, line.id, "PO-BUYER", datetime.now(UTC), other_buyer, db_session, payment_method="wire"
        )
        assert updated.status == BuyPlanLineStatus.PENDING_VERIFY.value
        assert updated.po_number == "PO-BUYER"

    def test_manager_proceeds(self, db_session: Session, test_user: User, other_buyer: User, manager_user: User):
        bp, line = _line_assigned_to_other(db_session, test_user, other_buyer)

        updated = confirm_po(
            bp.id, line.id, "PO-MGR", datetime.now(UTC), manager_user, db_session, payment_method="wire"
        )
        assert updated.status == BuyPlanLineStatus.PENDING_VERIFY.value

    def test_admin_proceeds(self, db_session: Session, test_user: User, other_buyer: User, admin_user: User):
        bp, line = _line_assigned_to_other(db_session, test_user, other_buyer)

        updated = confirm_po(
            bp.id, line.id, "PO-ADMIN", datetime.now(UTC), admin_user, db_session, payment_method="wire"
        )
        assert updated.status == BuyPlanLineStatus.PENDING_VERIFY.value


# ── (b) ROUTE gate ───────────────────────────────────────────────────────


class TestConfirmPoRouteGate:
    def test_non_assigned_viewer_403s_line_unchanged(
        self, client: TestClient, db_session: Session, test_user: User, other_buyer: User
    ):
        """Client is authed as test_user, who owns the plan but is NOT the line's buyer
        — exactly the reported gap."""
        bp, line = _line_assigned_to_other(db_session, test_user, other_buyer)

        resp = client.post(
            f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/confirm-po",
            data={"po_number": "PO-HACK", "payment_method": "wire"},
        )
        assert resp.status_code == 403
        db_session.expire_all()
        assert line.status == BuyPlanLineStatus.AWAITING_PO.value
        assert line.po_number is None

    def test_manager_route_succeeds_acting_for_the_buyer(
        self, manager_client: TestClient, db_session: Session, test_user: User, other_buyer: User
    ):
        bp, line = _line_assigned_to_other(db_session, test_user, other_buyer)

        with patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock):
            resp = manager_client.post(
                f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/confirm-po",
                data={"po_number": "PO-MGR-ROUTE", "payment_method": "wire"},
            )
        assert resp.status_code == 200
        db_session.expire_all()
        assert line.status == BuyPlanLineStatus.PENDING_VERIFY.value
        assert line.po_number == "PO-MGR-ROUTE"

    def test_assigned_buyer_route_still_succeeds(self, db_session: Session, test_user: User, other_buyer: User):
        bp, line = _line_assigned_to_other(db_session, test_user, other_buyer)

        c = _client_as(db_session, other_buyer)
        try:
            with patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock):
                resp = c.post(
                    f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/confirm-po",
                    data={"po_number": "PO-SELF", "payment_method": "wire"},
                )
        finally:
            _pop_overrides()
        assert resp.status_code == 200
        db_session.expire_all()
        assert line.status == BuyPlanLineStatus.PENDING_VERIFY.value


# ── (c) PANE rendering ───────────────────────────────────────────────────


class TestPoPanePresentsByActor:
    def test_non_assigned_viewer_sees_readonly_card_not_form(
        self, client: TestClient, db_session: Session, test_user: User, other_buyer: User
    ):
        bp, line = _line_assigned_to_other(db_session, test_user, other_buyer)

        body = client.get(f"/v2/partials/approvals/po/{line.id}/pane").text
        assert "Awaiting PO — assigned to Other Buyer" in body
        assert "Confirm the PO you cut in Acctivate" not in body
        assert 'name="po_number"' not in body
        assert "Act for the buyer" not in body  # plain viewer, not a manager

    def test_manager_pane_shows_expander_revealing_the_same_form(
        self, manager_client: TestClient, db_session: Session, test_user: User, other_buyer: User
    ):
        bp, line = _line_assigned_to_other(db_session, test_user, other_buyer)

        body = manager_client.get(f"/v2/partials/approvals/po/{line.id}/pane").text
        assert "Awaiting PO — assigned to Other Buyer" in body
        assert "Act for the buyer" in body
        # The form is present in the DOM (behind x-show), so the manager can act.
        assert "Confirm the PO you cut in Acctivate" in body
        assert 'name="po_number"' in body

    def test_assigned_buyer_pane_shows_form_outright(self, db_session: Session, test_user: User, other_buyer: User):
        bp, line = _line_assigned_to_other(db_session, test_user, other_buyer)

        c = _client_as(db_session, other_buyer)
        try:
            body = c.get(f"/v2/partials/approvals/po/{line.id}/pane").text
        finally:
            _pop_overrides()
        assert "Confirm the PO you cut in Acctivate" in body
        assert "Awaiting PO — assigned to" not in body


# ── (d) parse-confirmation gate ──────────────────────────────────────────


class TestParseConfirmationGate:
    def test_non_assigned_non_manager_403s_without_ai_call(
        self, client: TestClient, db_session: Session, test_user: User, other_buyer: User
    ):
        bp, line = _line_assigned_to_other(db_session, test_user, other_buyer)

        with patch("app.routers.htmx.approvals_hub.parse_po_confirmation", new_callable=AsyncMock) as mock_parse:
            resp = client.post(
                f"/v2/partials/approvals/po/{line.id}/parse-confirmation",
                data={"pasted_text": "PO confirmation text"},
            )
        assert resp.status_code == 403
        mock_parse.assert_not_called()

    def test_manager_parse_confirmation_allowed(
        self, manager_client: TestClient, db_session: Session, test_user: User, other_buyer: User
    ):
        bp, line = _line_assigned_to_other(db_session, test_user, other_buyer)

        with patch(
            "app.routers.htmx.approvals_hub.parse_po_confirmation", new_callable=AsyncMock, return_value=None
        ) as mock_parse:
            resp = manager_client.post(
                f"/v2/partials/approvals/po/{line.id}/parse-confirmation",
                data={"pasted_text": "PO confirmation text"},
            )
        assert resp.status_code == 200
        mock_parse.assert_called_once()
