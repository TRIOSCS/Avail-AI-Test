"""test_prepayment_notifications.py — accounting/AP prepayment notifications (Task 5).

Covers app.services.prepayment_notifications:
  - notify_prepayment_requested / _approved email BOTH configured group DLs (accounting +
    AP) and post a Teams Adaptive Card to the prepayment webhook;
  - an unset config key skips that channel with no raise;
  - a per-channel send failure is isolated (the other channel still runs);
  - when BOTH channels fail/skip while a group address WAS configured, a durable in-app
    ActivityLog alert is written to the requester (notification-honesty, finding #8);
  - the card renders the amount to 2 decimals honoring currency + the beneficiary legal
    name (findings #9/#14);
  - distinct DO-NOT-PAY (requested) vs OK-TO-WIRE (approved) headings (finding #13).

Called by: pytest
Depends on: app.services.prepayment_notifications, app.services.prepayment_service,
            app.services.approvals.service, conftest (db_session), unittest.mock.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.constants import PaymentMethod
from app.models import ActivityLog, Offer, Requirement, SystemConfig, User
from app.models.approvals import ApprovalRequest
from app.models.buy_plan import BuyPlan, BuyPlanLine
from app.models.quotes import Quote
from app.models.sourcing import Requisition
from app.models.vendors import VendorCard
from app.services import prepayment_notifications as pn
from app.services.prepayment_service import create_prepayment

ACC = "accounting@trio.test"
AP = "ap@trio.test"
HOOK = "https://outlook.office.com/webhook/prepay"


# ── Builders ─────────────────────────────────────────────────────────────


def _seed_approver(db: Session) -> User:
    """A manager who owns their requisitions AND can approve prepayments (routing +
    ownership both satisfied by the same user in these unit tests)."""
    u = User(
        email=f"appr-{uuid.uuid4().hex[:6]}@trioscs.com",
        name="PP Approver",
        role="manager",
        azure_id=f"az-{uuid.uuid4().hex[:8]}",
        is_active=True,
        can_approve_prepayments=True,
        prepayment_approval_limit=None,
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def _make_prepayment(
    db: Session,
    *,
    requester: User,
    vendor_legal: str | None = "Acme Components LLC",
    offer_vendor: str = "AcmeVendor",
    currency: str = "USD",
    amount: Decimal = Decimal("20002.38"),
    test_report_sent: bool = False,
    remarks: str | None = "rush",
) -> tuple[object, ApprovalRequest]:
    """Build a full plan→line→offer→vendor-card graph and a routed Prepayment."""
    req = Requisition(
        name=f"REQ-{uuid.uuid4().hex[:6]}",
        customer_name="AcmeCo",
        status="active",
        created_by=requester.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()
    rq = Requirement(requisition_id=req.id, primary_mpn="LM317", created_at=datetime.now(timezone.utc))
    db.add(rq)
    db.flush()
    q = Quote(
        requisition_id=req.id,
        quote_number=f"Q-{uuid.uuid4().hex[:8]}",
        line_items=[],
        status="sent",
        created_by_id=requester.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(q)
    db.flush()
    bp = BuyPlan(
        requisition_id=req.id,
        quote_id=q.id,
        status="active",
        so_status="approved",
        sales_order_number="SO-999",
        submitted_by_id=requester.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(bp)
    db.flush()
    vc = VendorCard(
        normalized_name=f"vc-{uuid.uuid4().hex[:8]}",
        display_name="AcmeVendor Display",
        legal_name=vendor_legal,
    )
    db.add(vc)
    db.flush()
    off = Offer(
        requirement_id=rq.id,
        vendor_card_id=vc.id,
        vendor_name=offer_vendor,
        vendor_name_normalized=offer_vendor.lower(),
        mpn="LM317",
        normalized_mpn="LM317",
        unit_price=10.0,
    )
    db.add(off)
    db.flush()
    line = BuyPlanLine(
        buy_plan_id=bp.id,
        requirement_id=rq.id,
        offer_id=off.id,
        quantity=2,
        unit_cost=10.0,
        buyer_id=requester.id,
        status="pending_verify",
        po_number="PO-2024",
        po_confirmed_at=datetime.now(timezone.utc),
    )
    db.add(line)
    db.flush()
    pp, ar = create_prepayment(
        db,
        buy_plan_id=bp.id,
        buy_plan_line_id=line.id,
        vendor_card_id=vc.id,
        payment_method=PaymentMethod.WIRE,
        total_incl_fees=amount,
        test_report_sent=test_report_sent,
        buyer_remarks=remarks,
        created_by=requester,
        currency=currency,
    )
    db.commit()
    return pp, ar


def _set_config(db: Session, **kv: str) -> None:
    """Seed system_config rows directly (set_config_value 404s on unknown keys)."""
    for k, v in kv.items():
        db.add(SystemConfig(key=k, value=v, updated_at=datetime.now(timezone.utc)))
    db.commit()


# ── Email + Teams: requested ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_requested_emails_groups_and_posts_card(db_session: Session):
    appr = _seed_approver(db_session)
    pp, _ar = _make_prepayment(db_session, requester=appr)
    _set_config(
        db_session,
        accounting_group_email=ACC,
        ap_group_email=AP,
        prepayment_teams_webhook=HOOK,
    )
    with (
        patch.object(pn, "_send_group_email", new=AsyncMock(return_value=True)) as email,
        patch.object(pn, "post_teams_channel_card", new=AsyncMock()) as card,
    ):
        result = await pn.notify_prepayment_requested(pp.id, db=db_session)

    to_addrs = email.call_args.kwargs.get("to") or email.call_args.args[1]
    assert ACC in to_addrs and AP in to_addrs
    assert card.called
    assert (card.call_args.args[1:] or (None,))[0] == HOOK or card.call_args.kwargs.get("webhook_url") == HOOK
    assert result["email_sent"] is True and result["teams_sent"] is True
    assert ACC in result["recipients"] and AP in result["recipients"]


# ── Email + Teams: approved (distinct OK-TO-WIRE subject) ─────────────────


@pytest.mark.asyncio
async def test_approved_emails_groups_with_ok_to_wire_subject(db_session: Session):
    from app.services.approvals.service import decide

    appr = _seed_approver(db_session)
    pp, ar = _make_prepayment(db_session, requester=appr)
    decide(db_session, ar.id, appr, "approve", comment="approved")
    db_session.commit()

    _set_config(db_session, accounting_group_email=ACC, ap_group_email=AP, prepayment_teams_webhook=HOOK)
    with (
        patch.object(pn, "_send_group_email", new=AsyncMock(return_value=True)) as email,
        patch.object(pn, "post_teams_channel_card", new=AsyncMock()) as card,
    ):
        await pn.notify_prepayment_approved(pp.id, db=db_session)

    assert email.called and card.called
    subject = email.call_args.kwargs.get("subject") or email.call_args.args[2]
    assert "OK TO WIRE" in subject


# ── Unset config skips both channels, no raise ────────────────────────────


@pytest.mark.asyncio
async def test_unset_config_skips_channel_no_raise(db_session: Session):
    appr = _seed_approver(db_session)
    pp, _ar = _make_prepayment(db_session, requester=appr)
    with (
        patch.object(pn, "_send_group_email", new=AsyncMock(return_value=True)) as email,
        patch.object(pn, "post_teams_channel_card", new=AsyncMock()) as card,
    ):
        result = await pn.notify_prepayment_requested(pp.id, db=db_session)

    assert not card.called
    assert not email.called
    assert result["email_sent"] is False and result["teams_sent"] is False
    # Nothing was expected to go out (no address configured) → no failure alert.
    assert db_session.query(ActivityLog).filter(ActivityLog.subject.like("Prepayment%FAILED%")).count() == 0


# ── A per-channel failure is isolated ─────────────────────────────────────


@pytest.mark.asyncio
async def test_send_failure_is_isolated(db_session: Session):
    appr = _seed_approver(db_session)
    pp, _ar = _make_prepayment(db_session, requester=appr)
    _set_config(db_session, accounting_group_email=ACC, prepayment_teams_webhook=HOOK)
    with (
        patch.object(pn, "_send_group_email", new=AsyncMock(side_effect=RuntimeError("graph down"))) as email,
        patch.object(pn, "post_teams_channel_card", new=AsyncMock()) as card,
    ):
        result = await pn.notify_prepayment_requested(pp.id, db=db_session)  # must NOT raise

    assert email.called and card.called  # Teams still posted despite the email exception
    assert result["email_sent"] is False and result["teams_sent"] is True


# ── All channels failed but a group address was set → in-app alert ────────


@pytest.mark.asyncio
async def test_all_channels_failed_writes_inapp_alert(db_session: Session):
    appr = _seed_approver(db_session)
    pp, _ar = _make_prepayment(db_session, requester=appr)
    _set_config(db_session, accounting_group_email=ACC)  # no webhook → Teams skipped
    with (
        patch.object(pn, "_send_group_email", new=AsyncMock(return_value=False)) as email,
        patch.object(pn, "post_teams_channel_card", new=AsyncMock()) as card,
    ):
        result = await pn.notify_prepayment_requested(pp.id, db=db_session)

    assert email.called and not card.called
    assert result["email_sent"] is False and result["teams_sent"] is False
    alert = (
        db_session.query(ActivityLog)
        .filter(
            ActivityLog.user_id == appr.id,
            ActivityLog.subject.like(f"Prepayment #{pp.id} notification FAILED%"),
        )
        .one()
    )
    assert "accounting/AP" in alert.subject
    assert alert.channel == "system"


# ── Teams-webhook-only configured + post fails → in-app alert ─────────────


@pytest.mark.asyncio
async def test_teams_only_failure_writes_inapp_alert(db_session: Session):
    """Only the Teams webhook is configured (no group DLs).

    If the post fails, nobody was told — the honesty alert must still fire (keys on the
    webhook being set, not just the group recipients). Blind spot: an ops team on Teams-
    only would otherwise get silence.
    """
    appr = _seed_approver(db_session)
    pp, _ar = _make_prepayment(db_session, requester=appr)
    _set_config(db_session, prepayment_teams_webhook=HOOK)  # webhook only, no group emails
    with (
        patch.object(pn, "_send_group_email", new=AsyncMock(return_value=True)) as email,
        patch.object(pn, "post_teams_channel_card", new=AsyncMock(side_effect=RuntimeError("teams down"))) as card,
    ):
        result = await pn.notify_prepayment_requested(pp.id, db=db_session)

    assert not email.called  # no group DLs → email channel skipped entirely
    assert card.called  # Teams was attempted
    assert result["email_sent"] is False and result["teams_sent"] is False
    assert result["recipients"] == []  # no group emails configured
    alert = (
        db_session.query(ActivityLog)
        .filter(
            ActivityLog.user_id == appr.id,
            ActivityLog.subject.like(f"Prepayment #{pp.id} notification FAILED%"),
        )
        .one()
    )
    assert "accounting/AP" in alert.subject
    assert alert.channel == "system"


# ── Card content: currency to 2 decimals + beneficiary legal name ─────────


def test_card_shows_currency_2dp_and_beneficiary(db_session: Session):
    appr = _seed_approver(db_session)
    pp, _ar = _make_prepayment(
        db_session, requester=appr, vendor_legal="Acme Components LLC", amount=Decimal("20002.38")
    )
    text = json.dumps(pn._card(pp, "requested"))
    assert "USD 20,002.38" in text  # 2 decimals + currency (finding #9)
    assert "Acme Components LLC" in text  # beneficiary legal name (finding #14)


def test_beneficiary_falls_back_to_vendor_name_without_legal(db_session: Session):
    appr = _seed_approver(db_session)
    pp, _ar = _make_prepayment(db_session, requester=appr, vendor_legal=None, offer_vendor="AcmeVendor")
    text = json.dumps(pn._card(pp, "requested"))
    assert "AcmeVendor" in text  # snapshot vendor_name when no legal_name


# ── Distinct headings: DO-NOT-PAY vs OK-TO-WIRE (finding #13) ─────────────


def test_distinct_headings(db_session: Session):
    appr = _seed_approver(db_session)
    pp, _ar = _make_prepayment(db_session, requester=appr)
    req_card = json.dumps(pn._card(pp, "requested"))
    app_card = json.dumps(pn._card(pp, "approved"))
    assert "PENDING APPROVAL" in req_card and "DO NOT PAY YET" in req_card
    assert "OK TO WIRE" in app_card and "APPROVED" in app_card
    assert "DO NOT PAY" not in app_card
