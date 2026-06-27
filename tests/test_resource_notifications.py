"""test_resource_notifications.py — Re-source ("cut PO cancelled") urgent broadcast.

Covers the delivery-floor + honor-opt-out-only-on-personal-pushes policy of
buyplan_notifications.notify_resource_requested and the full-Adaptive-Card sibling
teams_notifications.post_teams_channel_card:

- Recipient set = all active buyers except the actor, plus the deal's salesperson
  (submitted_by, fallback requisition creator); inactive users and non-buyers excluded.
- In-app ActivityLog row written for EVERY recipient (including opted-out ones), with
  all five polymorphic FKs (user_id, requisition_id, requirement_id, vendor_card_id of
  the CANCELED vendor, buy_plan_id).
- Teams channel card ALWAYS posted (once); email + Teams DM only to opted-in recipients.
- post_teams_channel_card no-ops cleanly when the webhook credential is unset.

External I/O (email send, Teams channel card, Teams DM) is mocked at the SOURCE module.

Called by: pytest
Depends on: conftest.py, app.services.buyplan_notifications, app.services.teams_notifications
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from loguru import logger

from app.constants import ActivityType
from app.models import ActivityLog, Offer, Requirement, User
from app.models.buy_plan import BuyPlan, BuyPlanLine

# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════


@contextmanager
def _capture_logs(level):
    """Capture loguru messages at the given level into a list yielded to the caller."""
    captured = []
    sink_id = logger.add(lambda msg: captured.append(str(msg)), level=level)
    try:
        yield captured
    finally:
        logger.remove(sink_id)


def _make_user(db, email, name, role="buyer", is_active=True, alert=True):
    u = User(
        email=email,
        name=name,
        role=role,
        is_active=is_active,
        notify_resource_alert_enabled=alert,
        azure_id=f"az-{email}",
        m365_connected=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _make_plan_with_line(db, *, submitter_id, creator_id, vendor_card_id):
    """Build a minimal Requisition→Quote→BuyPlan→BuyPlanLine(+Offer) graph.

    Returns (plan, line). The line's offer carries vendor_card_id (the CANCELED vendor).
    """
    from app.models import Company, CustomerSite, Quote, Requisition

    req = Requisition(
        name="REQ-RSRC-1",
        customer_name="Acme Corp",
        status="open",
        created_by=creator_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()

    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn="LM317T",
        description="Adjustable voltage regulator",
        target_qty=1000,
        created_at=datetime.now(timezone.utc),
    )
    db.add(requirement)
    db.flush()

    co = Company(name="Acme Corp", is_active=True, created_at=datetime.now(timezone.utc))
    db.add(co)
    db.flush()
    site = CustomerSite(company_id=co.id, site_name="Acme HQ")
    db.add(site)
    db.flush()
    q = Quote(
        requisition_id=req.id,
        customer_site_id=site.id,
        quote_number="Q-RSRC-1",
        status="sent",
        line_items=[],
        subtotal=1000.0,
        total_cost=500.0,
        total_margin_pct=50.0,
        created_by_id=creator_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(q)
    db.flush()

    plan = BuyPlan(
        quote_id=q.id,
        requisition_id=req.id,
        submitted_by_id=submitter_id,
        status="active",
        so_status="verified",
        sales_order_number="SO-RSRC-1",
    )
    db.add(plan)
    db.flush()

    offer = Offer(
        requisition_id=req.id,
        requirement_id=requirement.id,
        vendor_card_id=vendor_card_id,
        vendor_name="Arrow Electronics",
        mpn="LM317T",
        qty_available=1000,
        unit_price=1.50,
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    db.add(offer)
    db.flush()

    line = BuyPlanLine(
        buy_plan_id=plan.id,
        requirement_id=requirement.id,
        offer_id=offer.id,
        quantity=750,
        unit_cost=1.50,
    )
    db.add(line)
    db.commit()
    db.refresh(plan)
    db.refresh(line)
    return plan, line


def _patch_channels():
    """Patch all three external-I/O sinks at their SOURCE modules.

    Returns a contextmanager-yielding tuple (mock_email, mock_card, mock_dm).
    """
    return (
        patch("app.services.buyplan_notifications._send_email", new_callable=AsyncMock),
        patch("app.services.teams_notifications.post_teams_channel_card", new_callable=AsyncMock),
        patch("app.services.buyplan_notifications._teams_dm", new_callable=AsyncMock),
    )


# ═══════════════════════════════════════════════════════════════════════
# post_teams_channel_card
# ═══════════════════════════════════════════════════════════════════════


class TestPostTeamsChannelCard:
    @pytest.mark.asyncio
    async def test_no_op_when_webhook_unset(self):
        """Silently returns (no http.post, no raise) when TEAMS_WEBHOOK_URL is unset."""
        mock_http = MagicMock()
        mock_http.post = AsyncMock()
        with _capture_logs("DEBUG") as captured:
            with (
                patch("app.services.teams_notifications.get_credential_cached", return_value=None),
                patch("app.services.teams_notifications.http", mock_http),
            ):
                from app.services.teams_notifications import post_teams_channel_card

                result = await post_teams_channel_card({"type": "AdaptiveCard"})

        assert result is None
        mock_http.post.assert_not_called()
        assert any("not configured" in m for m in captured)

    @pytest.mark.asyncio
    async def test_posts_full_card_in_envelope(self):
        """Wraps the FULL card dict in the adaptive-card message envelope verbatim."""
        card = {
            "type": "AdaptiveCard",
            "body": [{"type": "FactSet", "facts": [{"title": "Part", "value": "LM317T"}]}],
            "actions": [{"type": "Action.OpenUrl", "title": "Claim this line", "url": "http://x/1"}],
        }
        mock_resp = MagicMock(status_code=200, text="ok")
        mock_http = MagicMock()
        mock_http.post = AsyncMock(return_value=mock_resp)
        with (
            patch(
                "app.services.teams_notifications.get_credential_cached",
                return_value="https://outlook.office.com/webhook/test",
            ),
            patch("app.services.teams_notifications.http", mock_http),
        ):
            from app.services.teams_notifications import post_teams_channel_card

            await post_teams_channel_card(card)

        mock_http.post.assert_called_once()
        payload = mock_http.post.call_args.kwargs["json"]
        assert payload["type"] == "message"
        attachment = payload["attachments"][0]
        assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
        # The full card is carried verbatim — including FactSet + OpenUrl action.
        assert attachment["content"] is card


# ═══════════════════════════════════════════════════════════════════════
# notify_resource_requested — recipients & channel policy
# ═══════════════════════════════════════════════════════════════════════


class TestNotifyResourceRequested:
    def _seed(self, db, vendor_card_id):
        actor = _make_user(db, "actor@trioscs.com", "Actor Buyer", "buyer")
        recipient = _make_user(db, "buyer1@trioscs.com", "Buyer One", "buyer", alert=True)
        optout = _make_user(db, "buyer2@trioscs.com", "Buyer Two", "buyer", alert=False)
        _make_user(db, "buyer3@trioscs.com", "Inactive Buyer", "buyer", is_active=False)
        _make_user(db, "trader@trioscs.com", "A Trader", "trader")  # non-buyer, not salesperson
        salesperson = _make_user(db, "sales@trioscs.com", "Sales Person", "sales")
        plan, line = _make_plan_with_line(
            db, submitter_id=salesperson.id, creator_id=actor.id, vendor_card_id=vendor_card_id
        )
        return actor, recipient, optout, salesperson, plan, line

    @pytest.mark.asyncio
    async def test_recipients_inapp_and_channel_policy(self, db_session, test_vendor_card):
        from app.services.buyplan_notifications import notify_resource_requested

        actor, recipient, optout, salesperson, plan, line = self._seed(db_session, test_vendor_card.id)
        p_email, p_card, p_dm = _patch_channels()
        with p_email as mock_email, p_card as mock_card, p_dm as mock_dm:
            await notify_resource_requested(
                plan, db_session, line_id=line.id, actor_id=actor.id, reason="Vendor went bankrupt"
            )

        # In-app rows: one per recipient = {recipient, optout, salesperson}; NOT actor,
        # NOT the inactive buyer, NOT the unrelated trader.
        acts = db_session.query(ActivityLog).filter_by(buy_plan_id=plan.id).all()
        recipient_ids = {a.user_id for a in acts}
        assert recipient_ids == {recipient.id, optout.id, salesperson.id}
        assert actor.id not in recipient_ids

        # Every in-app row carries all five FKs + the canonical type.
        for a in acts:
            assert a.activity_type == ActivityType.RESOURCE_REQUESTED
            assert a.channel == "system"
            assert a.requisition_id == plan.requisition_id
            assert a.requirement_id == line.requirement_id
            assert a.vendor_card_id == test_vendor_card.id
            assert a.buy_plan_id == plan.id

        # Channel card posted exactly once (delivery floor — always).
        mock_card.assert_awaited_once()

        # Email + DM only to opted-in recipients = {recipient, salesperson} (optout excluded).
        emailed = {c.args[0].id for c in mock_email.await_args_list}
        dmed = {c.args[0].id for c in mock_dm.await_args_list}
        assert emailed == {recipient.id, salesperson.id}
        assert dmed == {recipient.id, salesperson.id}
        assert optout.id not in emailed
        assert optout.id not in dmed

    @pytest.mark.asyncio
    async def test_managers_and_admins_are_recipients_traders_excluded(self, db_session, test_vendor_card):
        # Managers/admins can ALSO claim the open pool, so the urgent alert must reach them;
        # sales/trader (who cannot cut/claim POs) must NOT receive it.
        from app.services.buyplan_notifications import notify_resource_requested

        actor = _make_user(db_session, "actor2@trioscs.com", "Actor", "buyer")
        manager = _make_user(db_session, "mgr@trioscs.com", "A Manager", "manager")
        admin = _make_user(db_session, "admin@trioscs.com", "An Admin", "admin")
        trader = _make_user(db_session, "trader2@trioscs.com", "A Trader", "trader")
        plan, line = _make_plan_with_line(
            db_session, submitter_id=actor.id, creator_id=actor.id, vendor_card_id=test_vendor_card.id
        )

        p_email, p_card, p_dm = _patch_channels()
        with p_email, p_card, p_dm:
            await notify_resource_requested(plan, db_session, line_id=line.id, actor_id=actor.id, reason="x")

        recipient_ids = {a.user_id for a in db_session.query(ActivityLog).filter_by(buy_plan_id=plan.id).all()}
        assert manager.id in recipient_ids
        assert admin.id in recipient_ids
        assert trader.id not in recipient_ids
        assert actor.id not in recipient_ids

    @pytest.mark.asyncio
    async def test_optout_still_gets_inapp_and_card_still_posts(self, db_session, test_vendor_card):
        from app.services.buyplan_notifications import notify_resource_requested

        actor, recipient, optout, salesperson, plan, line = self._seed(db_session, test_vendor_card.id)
        p_email, p_card, p_dm = _patch_channels()
        with p_email as mock_email, p_card as mock_card, p_dm as mock_dm:
            await notify_resource_requested(plan, db_session, line_id=line.id, actor_id=actor.id, reason="PO cut")

        # Opted-out buyer: in-app row STILL written ...
        optout_acts = db_session.query(ActivityLog).filter_by(buy_plan_id=plan.id, user_id=optout.id).all()
        assert len(optout_acts) == 1
        # ... but no email and no DM for them.
        assert optout.id not in {c.args[0].id for c in mock_email.await_args_list}
        assert optout.id not in {c.args[0].id for c in mock_dm.await_args_list}
        # ... and the channel card still fires regardless.
        mock_card.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_content_carries_vendor_and_reason(self, db_session, test_vendor_card):
        from app.services.buyplan_notifications import notify_resource_requested

        actor, recipient, optout, salesperson, plan, line = self._seed(db_session, test_vendor_card.id)
        p_email, p_card, p_dm = _patch_channels()
        with p_email as mock_email, p_card, p_dm as mock_dm:
            await notify_resource_requested(
                plan, db_session, line_id=line.id, actor_id=actor.id, reason="Vendor went bankrupt"
            )

        # Email body (positional arg 2) and DM text (positional arg 1) carry the
        # canceled vendor + reason + part.
        email_body = mock_email.await_args_list[0].args[2]
        dm_text = mock_dm.await_args_list[0].args[1]
        for blob in (email_body, dm_text):
            assert "Arrow Electronics" in blob
            assert "Vendor went bankrupt" in blob
            assert "LM317T" in blob

    @pytest.mark.asyncio
    async def test_salesperson_falls_back_to_requisition_creator(self, db_session, test_vendor_card):
        """When the plan has no submitted_by, the requisition creator stands in."""
        from app.services.buyplan_notifications import notify_resource_requested

        actor = _make_user(db_session, "actor@trioscs.com", "Actor Buyer", "buyer")
        creator = _make_user(db_session, "creator@trioscs.com", "Deal Creator", "sales")
        plan, line = _make_plan_with_line(
            db_session, submitter_id=None, creator_id=creator.id, vendor_card_id=test_vendor_card.id
        )
        p_email, p_card, p_dm = _patch_channels()
        with p_email, p_card, p_dm:
            await notify_resource_requested(plan, db_session, line_id=line.id, actor_id=actor.id, reason="x")

        recipient_ids = {a.user_id for a in db_session.query(ActivityLog).filter_by(buy_plan_id=plan.id).all()}
        assert creator.id in recipient_ids

    @pytest.mark.asyncio
    async def test_missing_line_is_a_no_op(self, db_session, test_vendor_card):
        from app.services.buyplan_notifications import notify_resource_requested

        actor, recipient, optout, salesperson, plan, line = self._seed(db_session, test_vendor_card.id)
        p_email, p_card, p_dm = _patch_channels()
        with p_email as mock_email, p_card as mock_card, p_dm as mock_dm:
            await notify_resource_requested(plan, db_session, line_id=999999, actor_id=actor.id, reason="x")

        assert db_session.query(ActivityLog).filter_by(buy_plan_id=plan.id).count() == 0
        mock_email.assert_not_awaited()
        mock_card.assert_not_awaited()
        mock_dm.assert_not_awaited()
