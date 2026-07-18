"""test_change_summary.py — two-part approve + change summary + notes-to-fixer
(Approvals Workspace 2.2).

Covers:
  - _change_summary.html: the audit-log-since-submission rendered "was X → now Y"
    in the approval block, with the empty state;
  - handoff=proceed → the existing approve path + a change-summary in-app
    notification to the submitter (skipped when nothing changed);
  - handoff=send_back → the existing reject→draft transition; a blank manager note
    auto-fills the default (the engine requires a non-blank comment); the manager's
    edits persist; the note lands as a decision-tagged NOTE row + fixer notification;
  - PO send-back (verify-po reject) and prepayment reject notes-to-fixer: decision-
    tagged NOTE rows on the line / prepayment + in-app notification to the fixer.

Called by: pytest
Depends on: conftest (db_session, test_user), tests.test_approvals_hub_tabs builders,
            app.routers.htmx.{buy_plans,approvals_hub}, app.services.workspace_notes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import ActivityType, BuyPlanLineStatus, BuyPlanStatus, PrepaymentStatus
from app.database import get_db
from app.dependencies import require_buyplan_approver, require_buyplan_po_approver, require_user
from app.models import ActivityLog, User
from app.models.notification import Notification
from app.services.field_audit import FieldEdit, log_field_edits
from tests.test_approvals_hub_tabs import (
    _line,
    _pending_buy_plan_request,
    _plan,
    _req_quote,
)
from tests.test_prepayment_workspace import _prepay_on_line  # noqa: F401 (fixture-style builder)


@pytest.fixture()
def hub_client(db_session: Session, test_user: User):
    """TestClient authed as test_user with every decide right."""
    from app.main import app

    test_user.can_approve_buy_plans = True
    test_user.can_approve_purchase_orders = True
    test_user.can_approve_prepayments = True
    db_session.commit()

    app.dependency_overrides[get_db] = lambda: (yield db_session)  # type: ignore[misc]
    app.dependency_overrides[require_user] = lambda: test_user
    app.dependency_overrides[require_buyplan_approver] = lambda: test_user
    app.dependency_overrides[require_buyplan_po_approver] = lambda: test_user
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in (get_db, require_user, require_buyplan_approver, require_buyplan_po_approver):
            app.dependency_overrides.pop(dep, None)


def _pending_plan(db: Session, user: User, *, with_edit: bool = False):
    """A PENDING plan with an open engine request; optionally one post-submission field
    edit so the change summary has content."""
    from datetime import UTC, datetime

    req, q, rq = _req_quote(db, user)
    bp = _plan(db, req, q, status=BuyPlanStatus.PENDING.value)
    bp.submitted_at = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    _pending_buy_plan_request(db, bp, user)
    if with_edit:
        log_field_edits(
            db,
            user=user,
            buy_plan_id=bp.id,
            edits=[FieldEdit(field="unit_sell", old="2.0", new="3.5")],
        )
    db.commit()
    return req, rq, bp


def _notes(db: Session) -> list[ActivityLog]:
    return (
        db.query(ActivityLog)
        .filter(ActivityLog.activity_type == ActivityType.NOTE.value)
        .order_by(ActivityLog.id)
        .all()
    )


# ── The change summary on the pane ───────────────────────────────────────


def test_summary_renders_was_now_rows(hub_client, db_session, test_user):
    _req, _rq, bp = _pending_plan(db_session, test_user, with_edit=True)

    body = hub_client.get(f"/v2/partials/approvals/plan/{bp.id}/pane").text
    assert "Changes since submission" in body
    assert "unit_sell" in body
    assert "was" in body and "now" in body
    assert "2.0" in body and "3.5" in body


def test_summary_empty_state(hub_client, db_session, test_user):
    _req, _rq, bp = _pending_plan(db_session, test_user, with_edit=False)

    body = hub_client.get(f"/v2/partials/approvals/plan/{bp.id}/pane").text
    assert "No changes since submission." in body


def test_summary_excludes_pre_submission_edits(hub_client, db_session, test_user):
    """edits_since windows on submitted_at — an edit from before submission is not part
    of this approval's summary."""
    from datetime import UTC, datetime

    _req, _rq, bp = _pending_plan(db_session, test_user, with_edit=True)
    old_row = db_session.query(ActivityLog).filter(ActivityLog.activity_type == ActivityType.FIELD_EDIT.value).one()
    old_row.created_at = datetime(2026, 6, 1, tzinfo=UTC)  # before submitted_at
    db_session.commit()

    body = hub_client.get(f"/v2/partials/approvals/plan/{bp.id}/pane").text
    assert "No changes since submission." in body


def test_workspace_block_offers_both_handoffs(hub_client, db_session, test_user):
    _req, _rq, bp = _pending_plan(db_session, test_user)

    body = hub_client.get(f"/v2/partials/approvals/plan/{bp.id}/pane").text
    assert 'name="handoff" value="proceed"' in body
    assert 'name="handoff" value="send_back"' in body
    assert "Approve &amp; notify" in body
    assert "Send back for sign-off" in body
    assert "Confirm reject" in body  # the hard-no path keeps its required note


# ── handoff=proceed ──────────────────────────────────────────────────────


def test_proceed_approves_and_notifies_submitter_with_summary(hub_client, db_session, test_user):
    _req, _rq, bp = _pending_plan(db_session, test_user, with_edit=True)

    with patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock):
        r = hub_client.post(
            f"/v2/partials/buy-plans/{bp.id}/approve",
            data={"handoff": "proceed", "origin": "approvals_workspace", "lens": "sales-orders"},
        )
    assert r.status_code == 200
    db_session.expire_all()
    assert bp.status == BuyPlanStatus.ACTIVE.value
    notif = db_session.query(Notification).filter(Notification.event_type == "buy_plan_changes").one()
    assert notif.user_id == test_user.id  # the submitter (fixer) gets the summary
    assert "unit_sell: was 2.0 → now 3.5" in notif.body


def test_proceed_with_no_changes_skips_summary_notification(hub_client, db_session, test_user):
    _req, _rq, bp = _pending_plan(db_session, test_user, with_edit=False)

    with patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock):
        r = hub_client.post(
            f"/v2/partials/buy-plans/{bp.id}/approve",
            data={"handoff": "proceed", "origin": "approvals_workspace"},
        )
    assert r.status_code == 200
    db_session.expire_all()
    assert bp.status == BuyPlanStatus.ACTIVE.value
    assert db_session.query(Notification).filter(Notification.event_type == "buy_plan_changes").count() == 0


# ── handoff=send_back ────────────────────────────────────────────────────


def test_send_back_blank_note_autofills_and_returns_draft(hub_client, db_session, test_user):
    _req, _rq, bp = _pending_plan(db_session, test_user, with_edit=True)

    with patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock):
        r = hub_client.post(
            f"/v2/partials/buy-plans/{bp.id}/approve",
            data={"handoff": "send_back", "origin": "approvals_workspace", "notes": ""},
        )
    assert r.status_code == 200
    db_session.expire_all()
    assert bp.status == BuyPlanStatus.DRAFT.value  # the existing reject→draft transition
    assert bp.approval_notes == "Sent back for sign-off — see change summary"
    (note,) = _notes(db_session)
    assert note.details == {"decision": "sent_back"}
    assert note.buy_plan_id == bp.id
    notif = db_session.query(Notification).filter(Notification.event_type == "buy_plan_sent_back").one()
    assert notif.user_id == test_user.id


def test_send_back_manager_note_lands_on_thread(hub_client, db_session, test_user):
    _req, _rq, bp = _pending_plan(db_session, test_user)

    with patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock):
        r = hub_client.post(
            f"/v2/partials/buy-plans/{bp.id}/approve",
            data={"handoff": "send_back", "origin": "approvals_workspace", "notes": "double-check line 2 sell"},
        )
    assert r.status_code == 200
    (note,) = _notes(db_session)
    assert note.notes == "double-check line 2 sell"
    assert note.details == {"decision": "sent_back"}


def test_send_back_preserves_manager_edits(hub_client, db_session, test_user):
    """Spec §7: the manager's edits persist through a send-back."""
    from app.models.buy_plan import BuyPlanLine

    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.PENDING.value)
    line = _line(db_session, bp, rq, test_user, status=BuyPlanLineStatus.AWAITING_PO.value)
    _pending_buy_plan_request(db_session, bp, test_user)
    db_session.commit()

    r = hub_client.post(f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/edit", data={"quantity": "175"})
    assert r.status_code == 200

    with patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock):
        r = hub_client.post(
            f"/v2/partials/buy-plans/{bp.id}/approve",
            data={"handoff": "send_back", "origin": "approvals_workspace"},
        )
    assert r.status_code == 200
    db_session.expire_all()
    assert bp.status == BuyPlanStatus.DRAFT.value
    assert db_session.get(BuyPlanLine, line.id).quantity == 175  # the edit survived


def test_plain_reject_note_tagged_rejected(hub_client, db_session, test_user):
    _req, _rq, bp = _pending_plan(db_session, test_user)

    with patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock):
        r = hub_client.post(
            f"/v2/partials/buy-plans/{bp.id}/approve",
            data={"action": "reject", "origin": "approvals_workspace", "notes": "margin too thin"},
        )
    assert r.status_code == 200
    (note,) = _notes(db_session)
    assert note.details == {"decision": "rejected"}
    assert note.notes == "margin too thin"
    notif = db_session.query(Notification).filter(Notification.event_type == "buy_plan_rejected").one()
    assert "margin too thin" in notif.body


# ── PO send-back note-to-fixer ───────────────────────────────────────────


def test_po_send_back_note_and_buyer_notification(hub_client, db_session, test_user):
    from datetime import UTC, datetime

    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _line(
        db_session,
        bp,
        rq,
        test_user,
        status=BuyPlanLineStatus.PENDING_VERIFY.value,
        po_number="PO-22",
        po_confirmed_at=datetime.now(UTC),
    )
    db_session.commit()

    with patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock):
        r = hub_client.post(
            f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/verify-po",
            data={"action": "reject", "rejection_note": "wrong ship date", "origin": "approvals_workspace"},
        )
    assert r.status_code == 200
    db_session.expire_all()
    assert line.status == BuyPlanLineStatus.AWAITING_PO.value
    (note,) = _notes(db_session)
    assert note.buy_plan_line_id == line.id
    assert note.details == {"decision": "sent_back"}
    assert note.notes == "wrong ship date"
    notif = db_session.query(Notification).filter(Notification.event_type == "po_sent_back").one()
    assert notif.user_id == line.buyer_id


def test_po_send_back_without_note_still_notifies_buyer(hub_client, db_session, test_user):
    from datetime import UTC, datetime

    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _line(
        db_session,
        bp,
        rq,
        test_user,
        status=BuyPlanLineStatus.PENDING_VERIFY.value,
        po_number="PO-23",
        po_confirmed_at=datetime.now(UTC),
    )
    db_session.commit()

    with patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock):
        r = hub_client.post(
            f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/verify-po",
            data={"action": "reject", "origin": "approvals_workspace"},
        )
    assert r.status_code == 200
    assert _notes(db_session) == []  # no note text → no thread row (note optional on send-back)
    assert db_session.query(Notification).filter(Notification.event_type == "po_sent_back").count() == 1


# ── Prepayment reject note-to-fixer ──────────────────────────────────────


def test_prepay_reject_note_and_requester_notification(hub_client, db_session, test_user):
    from tests.test_prepayment_workspace import _prepay_on_line as build

    _bp, _line_, pp, ar = build(db_session, test_user)

    with patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock):
        r = hub_client.post(
            f"/v2/partials/approvals/prepay-requests/{ar.id}/decide",
            data={"action": "reject", "comment": "terms changed", "origin": "approvals_workspace"},
        )
    assert r.status_code == 200
    db_session.expire_all()
    assert pp.status == PrepaymentStatus.VOID.value
    (note,) = _notes(db_session)
    assert note.prepayment_id == pp.id
    assert note.details == {"decision": "rejected"}
    assert note.notes == "terms changed"
    notif = db_session.query(Notification).filter(Notification.event_type == "prepay_rejected").one()
    assert notif.user_id == pp.created_by_id
    assert "terms changed" in notif.body
