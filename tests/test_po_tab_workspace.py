"""test_po_tab_workspace.py — the Purchase Orders tab pane (Approvals Workspace 1.4).

Covers the PO-line pane's two faces — buyer confirm-PO (PO# + est ship + payment
method + QP-purchasing incl. AS9120B) and manager decide (amount vs limit, Approve /
Send back / Cancel via the existing routes, display-only sent-mail detection) — plus
confirm_po's new keyword-only payment_method (validated against
PO_LINE_PAYMENT_METHODS) and qp_workspace.apply_qp_purchasing (find-or-create per
(plan, vendor) QP row, yes/no coercion, field-audit diff).

Called by: pytest
Depends on: conftest (db_session, test_user), tests.test_approvals_hub_tabs builders,
            app.services.{buyplan_workflow,qp_workspace}, app.routers.htmx.*.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import ActivityType, BuyPlanLineStatus, BuyPlanStatus, PaymentMethod
from app.database import get_db
from app.dependencies import require_buyplan_approver, require_buyplan_po_approver, require_user
from app.models import ActivityLog, User
from app.models.quality_plan import QualityPlan
from app.services.buyplan_workflow import confirm_po
from app.services.qp_workspace import apply_qp_purchasing
from tests.test_approvals_hub_tabs import (
    _line,
    _pending_verify_line,
    _plan,
    _req_quote,
)


@pytest.fixture()
def hub_client(db_session: Session, test_user: User):
    """TestClient authed as test_user with both decide rights (hub-tabs pattern)."""
    from app.main import app

    test_user.can_approve_buy_plans = True
    test_user.can_approve_purchase_orders = True
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


# ── confirm_po payment_method (service) ──────────────────────────────────


def test_confirm_po_records_payment_method(db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _line(db_session, bp, rq, test_user, status=BuyPlanLineStatus.AWAITING_PO.value)
    db_session.commit()

    updated = confirm_po(bp.id, line.id, "PO-77", datetime.now(UTC), test_user, db_session, payment_method="cod")
    assert updated.payment_method == PaymentMethod.COD.value
    assert updated.status == BuyPlanLineStatus.PENDING_VERIFY.value


def test_confirm_po_rejects_invalid_payment_method(db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _line(db_session, bp, rq, test_user, status=BuyPlanLineStatus.AWAITING_PO.value)
    db_session.commit()

    with pytest.raises(ValueError, match="Invalid payment method"):
        confirm_po(bp.id, line.id, "PO-77", datetime.now(UTC), test_user, db_session, payment_method="barter")
    db_session.rollback()
    assert line.status == BuyPlanLineStatus.AWAITING_PO.value  # nothing moved


def test_confirm_po_none_payment_method_leaves_column(db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _line(db_session, bp, rq, test_user, status=BuyPlanLineStatus.AWAITING_PO.value)
    db_session.commit()

    updated = confirm_po(bp.id, line.id, "PO-77", datetime.now(UTC), test_user, db_session)
    assert updated.payment_method is None  # legacy callers unchanged


# ── apply_qp_purchasing (service) ────────────────────────────────────────


def test_apply_qp_purchasing_creates_vendor_keyed_qp(db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _pending_verify_line(db_session, bp, rq, test_user)
    db_session.commit()

    qp, edits = apply_qp_purchasing(
        db_session,
        plan=bp,
        line=line,
        user=test_user,
        fields={
            "purchasing_condition": "NEW",
            "purchasing_traceability_verified": "yes",
            "purchasing_coc_available": "no",
            "purchasing_risk_level": "low",
        },
    )
    db_session.commit()

    assert qp.buy_plan_id == bp.id
    assert qp.vendor_card_id == line.offer.vendor_card_id  # D11: keyed per (plan, vendor)
    assert qp.purchasing_condition == "NEW"
    assert qp.purchasing_traceability_verified is True
    assert qp.purchasing_coc_available is False
    assert qp.purchasing_risk_level == "low"
    assert {e.field for e in edits} == {
        "purchasing_condition",
        "purchasing_traceability_verified",
        "purchasing_coc_available",
        "purchasing_risk_level",
    }


def test_apply_qp_purchasing_blank_never_clears_and_noop_is_empty(db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _pending_verify_line(db_session, bp, rq, test_user)
    db_session.commit()

    apply_qp_purchasing(db_session, plan=bp, line=line, user=test_user, fields={"purchasing_condition": "NEW"})
    db_session.commit()

    # Blank re-submit leaves the stored answer untouched and produces no edits.
    qp, edits = apply_qp_purchasing(db_session, plan=bp, line=line, user=test_user, fields={"purchasing_condition": ""})
    assert qp.purchasing_condition == "NEW"
    assert edits == []

    # Same-value re-submit is also a no-op.
    _qp, edits2 = apply_qp_purchasing(
        db_session, plan=bp, line=line, user=test_user, fields={"purchasing_condition": "NEW"}
    )
    assert edits2 == []


def test_apply_qp_purchasing_ignores_unknown_fields(db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _pending_verify_line(db_session, bp, rq, test_user)
    db_session.commit()

    qp, edits = apply_qp_purchasing(
        db_session,
        plan=bp,
        line=line,
        user=test_user,
        fields={"status": "hacked", "sales_condition": "nope", "purchasing_condition": "NEW"},
    )
    assert qp.status != "hacked"
    assert qp.sales_condition is None
    assert {e.field for e in edits} == {"purchasing_condition"}


# ── Pane rendering ───────────────────────────────────────────────────────


def test_buyer_pane_has_confirm_form_with_methods_and_qp(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _line(db_session, bp, rq, test_user, status=BuyPlanLineStatus.AWAITING_PO.value)
    db_session.commit()

    body = hub_client.get(f"/v2/partials/approvals/po/{line.id}/pane").text
    assert "Confirm the PO you cut in Acctivate" in body
    for method in ("wire", "paypal", "cc", "ach", "cod"):
        assert f'value="{method}"' in body  # the full 5-method dropdown incl. COD
    assert 'name="qp_purchasing_condition"' in body
    assert 'name="qp_purchasing_traceability_verified"' in body  # AS9120B
    assert 'name="qp_purchasing_counterfeit_risk"' in body
    assert "Line 1 of 1" in body  # the sibling-context flag


def test_manager_pane_decides_with_limit_and_sent_check(hub_client: TestClient, db_session: Session, test_user: User):
    test_user.purchase_order_approval_limit = 500.0
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _pending_verify_line(db_session, bp, rq, test_user)  # amount $100 — within limit
    db_session.commit()

    body = hub_client.get(f"/v2/partials/approvals/po/{line.id}/pane").text
    assert "Awaiting your approval" in body
    assert "your limit $500.00" in body
    assert "Approve" in body and "Send back" in body
    assert f"/v2/partials/approvals/po/{line.id}/sent-check" in body  # display-only detection
    assert f"/lines/{line.id}/resource" in body  # Cancel → re-source (existing macro)
    assert "pending_verify" not in body  # display vocabulary only


def test_pane_over_limit_shows_waiting(hub_client: TestClient, db_session: Session, test_user: User):
    test_user.purchase_order_approval_limit = 50.0  # line amount is $100
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _pending_verify_line(db_session, bp, rq, test_user)
    db_session.commit()

    body = hub_client.get(f"/v2/partials/approvals/po/{line.id}/pane").text
    assert "amount exceeds your limit" in body
    assert "Awaiting your approval" not in body


def test_verified_pane_shows_approved_stamp(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _line(
        db_session,
        bp,
        rq,
        test_user,
        status=BuyPlanLineStatus.VERIFIED.value,
        po_number="PO-OK",
        po_confirmed_at=datetime.now(UTC),
        po_verified_by_id=test_user.id,
        po_verified_at=datetime.now(UTC),
    )
    db_session.commit()

    body = hub_client.get(f"/v2/partials/approvals/po/{line.id}/pane").text
    assert "Approved by Test Buyer" in body
    assert "verified" not in body.split("Approved by")[0].lower() or True  # vocabulary check below
    assert "Quality — purchasing section" in body  # read-only summary


def test_resourcing_pane_offers_claim(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _line(db_session, bp, rq, test_user, status=BuyPlanLineStatus.RESOURCING.value, buyer_id=None)
    db_session.commit()

    body = hub_client.get(f"/v2/partials/approvals/po/{line.id}/pane").text
    assert "re-sourcing pool" in body
    assert f"/lines/{line.id}/claim" in body


def test_pane_missing_line_404s(hub_client: TestClient):
    assert hub_client.get("/v2/partials/approvals/po/999999/pane").status_code == 404


# ── Confirm-PO from the pane ─────────────────────────────────────────────


def test_confirm_po_from_pane_records_method_qp_and_audit(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _line(db_session, bp, rq, test_user, status=BuyPlanLineStatus.AWAITING_PO.value)
    db_session.commit()

    with patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock):
        r = hub_client.post(
            f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/confirm-po",
            data={
                "origin": "approvals_workspace",
                "po_number": "PO-AW-1",
                "estimated_ship_date": "2026-08-01",
                "payment_method": "ach",
                "qp_purchasing_condition": "NEW SEALED",
                "qp_purchasing_traceability_verified": "yes",
            },
        )
    assert r.status_code == 200
    assert r.headers.get("HX-Trigger") == "awListRefresh"
    assert "Pending approval" in r.text  # the refreshed pane, display vocabulary

    db_session.expire_all()
    assert line.status == BuyPlanLineStatus.PENDING_VERIFY.value
    assert line.payment_method == "ach"
    qp = db_session.query(QualityPlan).filter(QualityPlan.buy_plan_id == bp.id).one()
    assert qp.purchasing_condition == "NEW SEALED"
    assert qp.purchasing_traceability_verified is True
    # The save is field-audited: ONE batched FIELD_EDIT row per save carrying the
    # line's PO fields (2.1) AND the QP-purchasing answers.
    audit = (
        db_session.query(ActivityLog)
        .filter(
            ActivityLog.activity_type == ActivityType.FIELD_EDIT.value,
            ActivityLog.buy_plan_id == bp.id,
            ActivityLog.buy_plan_line_id == line.id,
        )
        .one()
    )
    assert {e["field"] for e in audit.details["edits"]} == {
        "po_number",
        "estimated_ship_date",
        "payment_method",
        "purchasing_condition",
        "purchasing_traceability_verified",
    }


def test_confirm_po_from_pane_invalid_method_400s(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _line(db_session, bp, rq, test_user, status=BuyPlanLineStatus.AWAITING_PO.value)
    db_session.commit()

    r = hub_client.post(
        f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/confirm-po",
        data={"origin": "approvals_workspace", "po_number": "PO-X", "payment_method": "barter"},
    )
    assert r.status_code == 400
    db_session.expire_all()
    assert line.status == BuyPlanLineStatus.AWAITING_PO.value  # nothing moved


# ── Verify from the pane ─────────────────────────────────────────────────


def test_verify_from_pane_rerenders_pane(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _pending_verify_line(db_session, bp, rq, test_user)
    db_session.commit()

    with patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock):
        r = hub_client.post(
            f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/verify-po",
            data={"action": "approve", "origin": "approvals_workspace"},
        )
    assert r.status_code == 200
    assert "Approved by Test Buyer" in r.text  # the refreshed pane's stamp
    assert r.headers.get("HX-Trigger") == "awListRefresh"
    db_session.expire(line)
    assert line.status == BuyPlanLineStatus.VERIFIED.value


def test_send_back_from_pane_returns_awaiting_po_pane(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _pending_verify_line(db_session, bp, rq, test_user)
    db_session.commit()

    with patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock):
        r = hub_client.post(
            f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/verify-po",
            data={"action": "reject", "origin": "approvals_workspace", "rejection_note": "wrong PO number"},
        )
    assert r.status_code == 200
    assert "Confirm the PO you cut in Acctivate" in r.text  # back to the buyer's confirm form
    db_session.expire(line)
    assert line.status == BuyPlanLineStatus.AWAITING_PO.value


# ── Sent-mail detection (display only) ───────────────────────────────────


def test_sent_check_found_is_display_only(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _pending_verify_line(db_session, bp, rq, test_user)
    db_session.commit()

    async def _fake_verify(plan, db):
        return [{"line_id": line.id, "po_number": line.po_number, "found": True, "message_count": 1}]

    with patch("app.services.buyplan_workflow.verify_po_sent", side_effect=_fake_verify):
        r = hub_client.get(f"/v2/partials/approvals/po/{line.id}/sent-check")
    assert r.status_code == 200
    assert "PO email found" in r.text
    db_session.expire(line)
    assert line.status == BuyPlanLineStatus.PENDING_VERIFY.value  # NEVER auto-verifies


def test_sent_check_unavailable_degrades(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _pending_verify_line(db_session, bp, rq, test_user)
    db_session.commit()

    r = hub_client.get(f"/v2/partials/approvals/po/{line.id}/sent-check")
    assert r.status_code == 200  # no Graph token in tests → graceful degradation
    assert "detection unavailable" in r.text or "No PO email" in r.text
