"""tests/test_po_confirm_link.py — the buyer relay (Deal Sheet T4).

Covers:
  1. The tokenized no-login confirm page (GET/POST /po/confirm/{token}): form for an
     open line, records via the SAME confirm_po service (state moves, buyer attributed,
     payment method required), read-only after (single-use by STATE), bad token → 404,
     wrong-state → inactive panel.
  2. notify_approved's buyer email carries the workspace deep link, a per-line
     tokenized Confirm-PO link, need-by context, and the Copy-for-ERP block; the buyer
     DM carries the deep link too.
  3. The Cut-PO task card deep-links its line.

Called by: pytest
Depends on: app.routers.po_confirm, app.services.po_confirm_tokens,
    app.services.buyplan_notifications, conftest (client, db_session, test_user,
    sales_user), tests.test_approvals_hub_tabs seed helpers.
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.constants import BuyPlanLineStatus, BuyPlanStatus
from app.models import User
from app.models.buy_plan import BuyPlanLine
from app.services.po_confirm_tokens import mint_po_confirm_token, resolve_po_confirm_token
from tests.test_approvals_hub_tabs import _line, _plan, _req_quote

# ── Token service ─────────────────────────────────────────────────────────


def test_token_round_trips_and_rejects_tampering():
    token = mint_po_confirm_token(41, 7)
    assert resolve_po_confirm_token(token) == (41, 7)
    assert resolve_po_confirm_token(token + "x") is None
    assert resolve_po_confirm_token("garbage") is None


# ── The public page ───────────────────────────────────────────────────────


def _active_line(db, user, status=BuyPlanLineStatus.AWAITING_PO.value):
    req, q, rq = _req_quote(db, user)
    bp = _plan(db, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _line(db, bp, rq, user, status=status)
    db.commit()
    return bp, line


def test_bad_token_renders_expired_404(client: TestClient):
    r = client.get("/po/confirm/not-a-token")
    assert r.status_code == 404
    assert "Link expired" in r.text


def test_open_line_renders_confirm_form(client: TestClient, db_session, test_user: User):
    _bp, line = _active_line(db_session, test_user)
    token = mint_po_confirm_token(line.id, test_user.id)

    r = client.get(f"/po/confirm/{token}")
    assert r.status_code == 200
    assert "Confirm the PO you cut" in r.text
    assert 'name="po_number"' in r.text
    assert 'name="payment_method"' in r.text


def test_post_confirms_line_as_the_token_buyer(client: TestClient, db_session, test_user: User):
    bp, line = _active_line(db_session, test_user)
    token = mint_po_confirm_token(line.id, test_user.id)

    with patch("app.services.buyplan_notifications.notify_po_confirmed", new_callable=AsyncMock):
        r = client.post(
            f"/po/confirm/{token}",
            data={"po_number": "PO-4411", "estimated_ship_date": "2026-09-01", "payment_method": "wire"},
        )
    assert r.status_code == 200
    assert "PO recorded" in r.text

    db_session.expire_all()
    fresh = db_session.get(BuyPlanLine, line.id)
    assert fresh.status == BuyPlanLineStatus.PENDING_VERIFY.value
    assert fresh.po_number == "PO-4411"
    assert fresh.payment_method == "wire"

    # Single-use by STATE: the same link now renders read-only, never re-confirms.
    again = client.get(f"/po/confirm/{token}")
    assert "Already confirmed" in again.text

    # The public path leaves the SAME audit trail the in-app confirm leaves —
    # one field-edit row attributed to the token's buyer.
    from app.constants import ActivityType
    from app.models.intelligence import ActivityLog

    rows = (
        db_session.query(ActivityLog)
        .filter_by(activity_type=ActivityType.FIELD_EDIT.value, buy_plan_line_id=line.id)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].user_id == test_user.id
    edited = {e["field"] for e in rows[0].details["edits"]}
    assert "po_number" in edited


def test_post_without_payment_method_rerenders_form_with_error(client: TestClient, db_session, test_user: User):
    _bp, line = _active_line(db_session, test_user)
    token = mint_po_confirm_token(line.id, test_user.id)

    r = client.post(f"/po/confirm/{token}", data={"po_number": "PO-1", "payment_method": ""})
    assert r.status_code == 400
    assert "payment method" in r.text
    db_session.expire_all()
    assert db_session.get(BuyPlanLine, line.id).status == BuyPlanLineStatus.AWAITING_PO.value


def test_resourcing_line_renders_inactive_panel(client: TestClient, db_session, test_user: User):
    _bp, line = _active_line(db_session, test_user, status=BuyPlanLineStatus.RESOURCING.value)
    token = mint_po_confirm_token(line.id, test_user.id)

    r = client.get(f"/po/confirm/{token}")
    assert r.status_code == 200
    assert "isn't awaiting a PO" in r.text


# ── The approval notification carries the relay links ─────────────────────


@pytest.mark.asyncio
async def test_approved_email_carries_deep_link_token_link_and_erp_block(db_session, test_user, sales_user):
    from app.services.buyplan_notifications import notify_approved

    req, q, rq = _req_quote(db_session, sales_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    bp.customer_po_number = "CUST-PO-9"
    line = _line(db_session, bp, rq, test_user, status=BuyPlanLineStatus.AWAITING_PO.value)
    line.buyer_id = test_user.id
    db_session.commit()

    with patch("app.services.buyplan_notifications._send_email", new_callable=AsyncMock) as mock_email:
        with patch("app.services.buyplan_notifications._teams_channel", new_callable=AsyncMock):
            with patch("app.services.buyplan_notifications._teams_dm", new_callable=AsyncMock) as mock_dm:
                await notify_approved(bp, db_session)

    assert mock_email.await_count == 1
    html = mock_email.call_args[0][2]
    assert f"/v2/approvals?tab=purchase-orders&select=line-{line.id}" in html  # deep link
    assert "/po/confirm/" in html  # tokenized no-login confirm link
    assert "Copy for the ERP" in html  # the paste block
    assert "CUST-PO-9" in html  # customer PO context

    # The emailed token actually resolves to this line + buyer.
    token = html.split("/po/confirm/")[1].split('"')[0]
    assert resolve_po_confirm_token(token) == (line.id, test_user.id)

    # The buyer DM carries the same deep link.
    dm_text = mock_dm.call_args[0][1]
    assert f"select=line-{line.id}" in dm_text


# ── Cut-PO task card deep link ────────────────────────────────────────────


def test_cut_po_task_row_links_the_line(client: TestClient, db_session, test_user: User):
    from app.services.task_service import on_buy_plan_assigned

    _bp, line = _active_line(db_session, test_user)
    on_buy_plan_assigned(
        db_session,
        requisition_id=line.buy_plan.requisition_id,
        buyer_id=test_user.id,
        vendor_name="Acme",
        mpn="LM317T",
        line_id=line.id,
    )
    db_session.commit()

    from app.models.task import RequisitionTask

    task = db_session.query(RequisitionTask).filter_by(source_ref=f"buyline:{line.id}").one()
    r = client.get(f"/api/requisitions/{line.buy_plan.requisition_id}/tasks/{task.id}/row")
    assert r.status_code == 200
    assert f"/v2/approvals?tab=purchase-orders&select=line-{line.id}" in r.text
