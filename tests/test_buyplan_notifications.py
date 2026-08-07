"""test_buyplan_notifications.py — Tests for buy plan notification service.

Single-path delivery (W3.8/§5.5): every approval-lifecycle notify_* enqueues exactly
ONE ApprovalOutbox email per recipient — no direct Graph send, no Teams post, no
in-app ActivityLog row for the same event. Also covers the helpers (_plan_context,
_lines_html, _wrap_email, _send_email, _teams_dm, run_notify_bg, log_buyplan_activity)
and the still-in-app routine notify_completed.

Called by: pytest
Depends on: conftest.py, app.services.buyplan_notifications, app.models.approvals
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.constants import (
    ApprovalGateType,
    ApprovalRecipientStatus,
    ApprovalRequestStatus,
    ApprovalStepRule,
    ApprovalSubjectType,
)
from app.models import ActivityLog, User
from app.models.approvals import (
    ApprovalOutbox,
    ApprovalRequest,
    ApprovalStep,
    ApprovalStepRecipient,
)
from app.models.buy_plan import (
    BuyPlan,
    BuyPlanLine,
    VerificationGroupMember,
)

# ═══════════════════════════════════════════════════════════════════════
# HELPER FACTORIES
# ═══════════════════════════════════════════════════════════════════════


def _make_user(db, email="buyer@trioscs.com", name="Test Buyer", role="buyer"):
    u = User(
        email=email,
        name=name,
        role=role,
        azure_id=f"az-{email}",
        m365_connected=True,
        created_at=datetime.now(UTC),
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _make_plan(db, submitter_id, **overrides):
    """Create a minimal BuyPlan with required FKs."""
    from app.models import Company, CustomerSite, Quote, Requisition

    # Requisition
    req = Requisition(
        name="REQ-BP",
        status="open",
        created_by=submitter_id,
        created_at=datetime.now(UTC),
    )
    db.add(req)
    db.flush()

    # Company + site + quote
    co = Company(name="Acme Corp", is_active=True, created_at=datetime.now(UTC))
    db.add(co)
    db.flush()
    site = CustomerSite(company_id=co.id, site_name="Acme HQ")
    db.add(site)
    db.flush()
    q = Quote(
        requisition_id=req.id,
        customer_site_id=site.id,
        quote_number="Q-2026-0042",
        status="sent",
        line_items=[],
        subtotal=1000.0,
        total_cost=500.0,
        total_margin_pct=50.0,
        created_by_id=submitter_id,
        created_at=datetime.now(UTC),
    )
    db.add(q)
    db.flush()

    defaults = dict(
        quote_id=q.id,
        requisition_id=req.id,
        submitted_by_id=submitter_id,
        status="pending",
        so_status="pending",
        sales_order_number="SO-001",
    )
    defaults.update(overrides)
    plan = BuyPlan(**defaults)
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def _add_line(db, plan, offer_mock=None, buyer_id=None, quantity=100, unit_cost=1.50, po_number=None, **overrides):
    """Add a BuyPlanLine with an optional mock offer."""
    from app.models import Offer, Requirement

    req = db.query(Requirement).first()
    if not req:
        req = Requirement(
            requisition_id=plan.requisition_id,
            primary_mpn="LM317T",
            target_qty=1000,
            created_at=datetime.now(UTC),
        )
        db.add(req)
        db.flush()

    offer = Offer(
        requisition_id=plan.requisition_id,
        vendor_name="Arrow Electronics",
        mpn="LM317T",
        qty_available=1000,
        unit_price=1.50,
        entered_by_id=plan.submitted_by_id,
        status="active",
        lead_time="2 weeks",
        created_at=datetime.now(UTC),
    )
    db.add(offer)
    db.flush()

    line = BuyPlanLine(
        buy_plan_id=plan.id,
        requirement_id=req.id,
        offer_id=offer.id,
        quantity=quantity,
        unit_cost=unit_cost,
        buyer_id=buyer_id,
        po_number=po_number,
        **overrides,
    )
    db.add(line)
    db.commit()
    db.refresh(plan)
    return line


def _make_request(db, plan, *, status=ApprovalRequestStatus.REQUESTED, recipients=()):
    """Anchor ApprovalRequest for *plan* (+ one ANY step with PENDING recipients)."""
    req = ApprovalRequest(
        gate_type=ApprovalGateType.BUY_PLAN,
        subject_type=ApprovalSubjectType.BUY_PLAN,
        subject_id=plan.id,
        requested_by_id=plan.submitted_by_id,
        owner_id=plan.submitted_by_id,
        status=status,
    )
    db.add(req)
    db.flush()
    if recipients:
        step = ApprovalStep(request_id=req.id, seq=1, rule=ApprovalStepRule.ANY)
        db.add(step)
        db.flush()
        for user in recipients:
            db.add(
                ApprovalStepRecipient(
                    step_id=step.id,
                    user_id=user.id,
                    status=ApprovalRecipientStatus.PENDING,
                )
            )
    db.commit()
    return req


def _outbox_rows(db):
    return db.query(ApprovalOutbox).order_by(ApprovalOutbox.id).all()


# ═══════════════════════════════════════════════════════════════════════
# _plan_context
# ═══════════════════════════════════════════════════════════════════════


class TestPlanContext:
    def test_basic_context(self, db_session):
        from app.services.buyplan_notifications import _plan_context

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id)
        ctx = _plan_context(plan, db_session)

        assert ctx["submitter"].id == user.id
        assert ctx["submitter_name"] == "Test Buyer"
        assert ctx["customer_name"] == "Acme Corp"
        assert ctx["quote_number"] == "Q-2026-0042"

    def test_no_submitter(self, db_session):
        from app.services.buyplan_notifications import _plan_context

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id, submitted_by_id=None)
        ctx = _plan_context(plan, db_session)

        assert ctx["submitter"] is None
        assert ctx["submitter_name"] == "Unknown"

    def test_no_quote(self, db_session):
        from app.services.buyplan_notifications import _plan_context

        # Use a mock with quote_id=None and no requisition to test blank path.
        mock_plan = MagicMock(submitted_by_id=None, quote_id=None, requisition=None)
        ctx = _plan_context(mock_plan, db_session)

        assert ctx["customer_name"] == ""
        assert ctx["quote_number"] == ""
        assert ctx["submitter"] is None

    def test_plan_context_uses_requisition_customer_without_quote(self, db_session):
        """SO-origin plan (no quote) populates customer_name from requisition."""
        from app.models.sourcing import Requisition
        from app.services.buyplan_notifications import _plan_context

        req = Requisition(name="SO-Notif-Req", customer_name="Initech")
        db_session.add(req)
        db_session.flush()
        plan = BuyPlan(quote_id=None, requisition_id=req.id)
        db_session.add(plan)
        db_session.flush()
        plan.requisition = req
        ctx = _plan_context(plan, db_session)
        assert ctx["customer_name"] == "Initech"
        assert ctx["quote_number"] == ""

    def test_site_without_company(self, db_session):
        """When quote.customer_site exists but .company is None, falls back to
        site_name."""
        from app.services.buyplan_notifications import _plan_context

        user = _make_user(db_session, "site-test@trioscs.com", "Site User", "buyer")

        # Mock the quote to have customer_site with no company
        mock_site = MagicMock()
        mock_site.company = None
        mock_site.site_name = "Orphan HQ"

        mock_quote = MagicMock()
        mock_quote.quote_number = "Q-SITE-001"
        mock_quote.customer_site = mock_site

        # Mock plan referencing the quote
        mock_plan = MagicMock(submitted_by_id=user.id, quote_id=999)

        with patch.object(
            db_session,
            "get",
            side_effect=lambda model, pk: {
                "User": user,
                "Quote": mock_quote,
            }.get(model.__name__),
        ):
            ctx = _plan_context(mock_plan, db_session)

        assert ctx["customer_name"] == "Orphan HQ"


# ═══════════════════════════════════════════════════════════════════════
# _lines_html
# ═══════════════════════════════════════════════════════════════════════


class TestLinesHtml:
    @pytest.mark.parametrize("lines", [[], None], ids=["empty_lines", "none_lines"])
    def test_no_lines(self, lines):
        from app.services.buyplan_notifications import _lines_html

        plan = MagicMock(lines=lines)
        rows, total = _lines_html(plan)
        assert rows == ""
        assert total == 0.0

    def test_with_lines(self, db_session):
        from app.services.buyplan_notifications import _lines_html

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id)
        _add_line(db_session, plan, quantity=100, unit_cost=2.00)

        rows, total = _lines_html(plan)
        assert "LM317T" in rows
        assert "Arrow Electronics" in rows
        assert total == 200.0

    def test_line_no_offer(self):
        from app.services.buyplan_notifications import _lines_html

        line = MagicMock(offer=None, unit_cost=5.0, quantity=10)
        plan = MagicMock(lines=[line])
        rows, total = _lines_html(plan)
        assert "—" in rows  # dash for missing offer
        assert total == 50.0


# ═══════════════════════════════════════════════════════════════════════
# _wrap_email
# ═══════════════════════════════════════════════════════════════════════


class TestWrapEmail:
    def test_wraps_content(self):
        from app.services.buyplan_notifications import _wrap_email

        result = _wrap_email("Test Title", "<p>Body</p>")
        assert "Test Title" in result
        assert "<p>Body</p>" in result
        assert "automated alert from AVAIL" in result

    def test_escapes_title(self):
        from app.services.buyplan_notifications import _wrap_email

        result = _wrap_email("Title <script>", "<p>ok</p>")
        assert "&lt;script&gt;" in result


# ═══════════════════════════════════════════════════════════════════════
# _send_email (kept for the re-source broadcast)
# ═══════════════════════════════════════════════════════════════════════


class TestSendEmail:
    @pytest.mark.asyncio
    async def test_sends_email(self, db_session):
        from app.services.buyplan_notifications import _send_email

        user = _make_user(db_session)
        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock()

        with patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"):
            with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
                await _send_email(user, "Subject", "<b>body</b>", db_session)

        mock_gc.post_json.assert_awaited_once()
        call_args = mock_gc.post_json.call_args
        assert call_args[0][0] == "/me/sendMail"

    @pytest.mark.asyncio
    async def test_no_token(self, db_session):
        from app.services.buyplan_notifications import _send_email

        user = _make_user(db_session)

        with patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value=None):
            await _send_email(user, "Subject", "<b>body</b>", db_session)
        # Should return silently — no error

    @pytest.mark.asyncio
    async def test_send_error_logged(self, db_session):
        from app.services.buyplan_notifications import _send_email

        user = _make_user(db_session)

        with patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"):
            with patch("app.utils.graph_client.GraphClient", side_effect=Exception("fail")):
                await _send_email(user, "Subject", "<b>body</b>", db_session)
        # Should not raise — error is caught and logged


# ═══════════════════════════════════════════════════════════════════════
# _teams_dm (kept for the nudge + re-source seams)
# ═══════════════════════════════════════════════════════════════════════


class TestTeamsHelpers:
    @pytest.mark.asyncio
    async def test_teams_dm(self, db_session):
        from app.services.buyplan_notifications import _teams_dm

        user = _make_user(db_session)
        with patch("app.services.teams_notifications.send_teams_dm", new_callable=AsyncMock) as mock:
            await _teams_dm(user, "DM message", db_session)
        mock.assert_awaited_once_with(user, "DM message", db_session)


# ═══════════════════════════════════════════════════════════════════════
# run_notify_bg
# ═══════════════════════════════════════════════════════════════════════


class TestRunV3NotifyBg:
    @pytest.mark.asyncio
    async def test_runs_coro_factory(self, db_session):
        from app.services.buyplan_notifications import run_notify_bg

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id)

        coro_factory = AsyncMock()

        async def _run_coro(coro, **kwargs):
            await coro

        with patch("app.database.SessionLocal", return_value=db_session):
            with patch("app.services.buyplan_notifications.safe_background_task", side_effect=_run_coro):
                await run_notify_bg(coro_factory, plan.id, extra="arg")

        coro_factory.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handles_missing_plan(self, db_session):
        from app.services.buyplan_notifications import run_notify_bg

        coro_factory = AsyncMock()

        async def _run_coro(coro, **kwargs):
            await coro

        with patch("app.database.SessionLocal", return_value=db_session):
            with patch("app.services.buyplan_notifications.safe_background_task", side_effect=_run_coro):
                await run_notify_bg(coro_factory, 99999)

        coro_factory.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handles_exception(self, db_session):
        from app.services.buyplan_notifications import run_notify_bg

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id)

        coro_factory = AsyncMock(side_effect=Exception("boom"))

        async def _run_coro(coro, **kwargs):
            await coro

        with patch("app.database.SessionLocal", return_value=db_session):
            with patch("app.services.buyplan_notifications.safe_background_task", side_effect=_run_coro):
                await run_notify_bg(coro_factory, plan.id)  # should not raise


# ═══════════════════════════════════════════════════════════════════════
# notify_submitted — single delivery: outbox email per pending approver
# ═══════════════════════════════════════════════════════════════════════


class TestNotifySubmitted:
    @pytest.mark.asyncio
    async def test_enqueues_one_outbox_email_per_pending_approver(self, db_session):
        from app.services.buyplan_notifications import notify_submitted

        user = _make_user(db_session)
        mgr = _make_user(db_session, "mgr@trioscs.com", "Manager", "manager")
        plan = _make_plan(db_session, user.id)
        _add_line(db_session, plan, quantity=10, unit_cost=5.0)
        request = _make_request(db_session, plan, recipients=[mgr])

        await notify_submitted(plan, db_session)

        rows = _outbox_rows(db_session)
        assert len(rows) == 1
        assert rows[0].recipient_user_id == mgr.id
        assert rows[0].request_id == request.id
        assert rows[0].channel == "email"
        assert "Buy Plan Approval" in rows[0].payload["subject"]
        assert "Acme Corp" in rows[0].payload["html"]

    @pytest.mark.asyncio
    async def test_no_extra_delivery(self, db_session):
        """Single path: no direct Graph email, no Teams, no in-app ActivityLog row."""
        from app.services.buyplan_notifications import notify_submitted

        user = _make_user(db_session)
        mgr = _make_user(db_session, "mgr@trioscs.com", "Manager", "manager")
        plan = _make_plan(db_session, user.id)
        _make_request(db_session, plan, recipients=[mgr])

        with (
            patch("app.services.buyplan_notifications._send_email", new_callable=AsyncMock) as mock_email,
            patch("app.services.teams_notifications.post_teams_channel", new_callable=AsyncMock) as mock_teams,
            patch("app.services.teams_notifications.send_teams_dm", new_callable=AsyncMock) as mock_dm,
        ):
            await notify_submitted(plan, db_session)

        mock_email.assert_not_awaited()
        mock_teams.assert_not_awaited()
        mock_dm.assert_not_awaited()
        assert db_session.query(ActivityLog).count() == 0
        assert len(_outbox_rows(db_session)) == 1

    @pytest.mark.asyncio
    async def test_notes_in_email(self, db_session):
        from app.services.buyplan_notifications import notify_submitted

        user = _make_user(db_session)
        mgr = _make_user(db_session, "mgr@trioscs.com", "Manager", "manager")
        plan = _make_plan(db_session, user.id, salesperson_notes="Urgent deal")
        _make_request(db_session, plan, recipients=[mgr])

        await notify_submitted(plan, db_session)

        assert "Urgent deal" in _outbox_rows(db_session)[0].payload["html"]

    @pytest.mark.asyncio
    async def test_only_pending_recipients_enqueued(self, db_session):
        from app.services.buyplan_notifications import notify_submitted

        user = _make_user(db_session)
        mgr = _make_user(db_session, "mgr@trioscs.com", "Manager", "manager")
        done = _make_user(db_session, "done@trioscs.com", "Done", "manager")
        plan = _make_plan(db_session, user.id)
        request = _make_request(db_session, plan, recipients=[mgr, done])
        # Mark the second recipient already decided — no email for them.
        recip = (
            db_session.query(ApprovalStepRecipient)
            .join(ApprovalStep, ApprovalStepRecipient.step_id == ApprovalStep.id)
            .filter(ApprovalStep.request_id == request.id, ApprovalStepRecipient.user_id == done.id)
            .one()
        )
        recip.status = ApprovalRecipientStatus.APPROVED
        db_session.commit()

        await notify_submitted(plan, db_session)

        rows = _outbox_rows(db_session)
        assert [r.recipient_user_id for r in rows] == [mgr.id]

    @pytest.mark.asyncio
    async def test_no_open_request_skips(self, db_session):
        from app.services.buyplan_notifications import notify_submitted

        user = _make_user(db_session)
        _make_user(db_session, "mgr@trioscs.com", "Manager", "manager")
        plan = _make_plan(db_session, user.id)  # no ApprovalRequest at all

        await notify_submitted(plan, db_session)  # no crash

        assert _outbox_rows(db_session) == []


# ═══════════════════════════════════════════════════════════════════════
# notify_approved — single delivery: outbox email per buyer
# ═══════════════════════════════════════════════════════════════════════


class TestNotifyApproved:
    @pytest.mark.asyncio
    async def test_enqueues_buyer_email_only(self, db_session):
        from app.services.buyplan_notifications import notify_approved

        submitter = _make_user(db_session)
        buyer = _make_user(db_session, "buyer2@trioscs.com", "Buyer2", "buyer")
        plan = _make_plan(db_session, submitter.id)
        _add_line(db_session, plan, buyer_id=buyer.id)
        request = _make_request(db_session, plan, status=ApprovalRequestStatus.APPROVED)

        await notify_approved(plan, db_session)

        rows = _outbox_rows(db_session)
        assert len(rows) == 1
        assert rows[0].recipient_user_id == buyer.id
        assert rows[0].request_id == request.id
        assert "POs Required" in rows[0].payload["subject"]
        assert "LM317T" in rows[0].payload["html"]
        # The submitter's decision notice is decide()'s own outbox email — NOT here.
        assert all(r.recipient_user_id != submitter.id for r in rows)

    @pytest.mark.asyncio
    async def test_no_extra_delivery(self, db_session):
        from app.services.buyplan_notifications import notify_approved

        submitter = _make_user(db_session)
        buyer = _make_user(db_session, "buyer2@trioscs.com", "Buyer2", "buyer")
        plan = _make_plan(db_session, submitter.id)
        _add_line(db_session, plan, buyer_id=buyer.id)
        _make_request(db_session, plan, status=ApprovalRequestStatus.APPROVED)

        with (
            patch("app.services.buyplan_notifications._send_email", new_callable=AsyncMock) as mock_email,
            patch("app.services.teams_notifications.send_teams_dm", new_callable=AsyncMock) as mock_dm,
            patch("app.services.teams_notifications.post_teams_channel", new_callable=AsyncMock) as mock_teams,
        ):
            await notify_approved(plan, db_session)

        mock_email.assert_not_awaited()
        mock_dm.assert_not_awaited()
        mock_teams.assert_not_awaited()
        assert db_session.query(ActivityLog).count() == 0

    @pytest.mark.asyncio
    async def test_no_buyers_enqueues_nothing(self, db_session):
        from app.services.buyplan_notifications import notify_approved

        submitter = _make_user(db_session)
        plan = _make_plan(db_session, submitter.id)
        _make_request(db_session, plan, status=ApprovalRequestStatus.APPROVED)

        await notify_approved(plan, db_session)

        assert _outbox_rows(db_session) == []

    @pytest.mark.asyncio
    async def test_no_request_skips(self, db_session):
        from app.services.buyplan_notifications import notify_approved

        submitter = _make_user(db_session)
        buyer = _make_user(db_session, "buyer2@trioscs.com", "Buyer2", "buyer")
        plan = _make_plan(db_session, submitter.id)
        _add_line(db_session, plan, buyer_id=buyer.id)

        await notify_approved(plan, db_session)  # no crash

        assert _outbox_rows(db_session) == []

    @pytest.mark.asyncio
    async def test_opted_out_buyer_not_enqueued(self, db_session):
        """notify_buyplan_email_enabled=False suppresses the buyer's outbox row."""
        from app.services.buyplan_notifications import notify_approved

        submitter = _make_user(db_session)
        buyer = _make_user(db_session, "optout@trioscs.com", "OptOut", "buyer")
        buyer.notify_buyplan_email_enabled = False
        plan = _make_plan(db_session, submitter.id)
        _add_line(db_session, plan, buyer_id=buyer.id)
        _make_request(db_session, plan, status=ApprovalRequestStatus.APPROVED)

        await notify_approved(plan, db_session)

        assert _outbox_rows(db_session) == []


# (TestNotifyRejected deleted: the notify_rejected no-op seam died with the
# legacy-PENDING fallback — the reject event's single delivery is the engine
# decide() outbox email, and the router no longer dispatches anything on reject.)


# ═══════════════════════════════════════════════════════════════════════
# notify_so_rejected — single delivery: outbox email to the salesperson
# ═══════════════════════════════════════════════════════════════════════


class TestNotifySORejected:
    @pytest.mark.asyncio
    async def test_halt_enqueues_submitter_email(self, db_session):
        from app.services.buyplan_notifications import notify_so_rejected

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id, so_rejection_note="Deal on hold")
        request = _make_request(db_session, plan, status=ApprovalRequestStatus.CANCELLED)

        await notify_so_rejected(plan, db_session, action="halt")

        rows = _outbox_rows(db_session)
        assert len(rows) == 1
        assert rows[0].recipient_user_id == user.id
        assert rows[0].request_id == request.id
        assert "Halted" in rows[0].payload["subject"]
        assert "halted" in rows[0].payload["html"]
        assert "Deal on hold" in rows[0].payload["html"]

    @pytest.mark.asyncio
    async def test_reject_label(self, db_session):
        from app.services.buyplan_notifications import notify_so_rejected

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id, so_rejection_note="Invalid SO")
        _make_request(db_session, plan)

        await notify_so_rejected(plan, db_session, action="reject")

        html = _outbox_rows(db_session)[0].payload["html"]
        assert "rejected" in html
        assert "Invalid SO" in html

    @pytest.mark.asyncio
    async def test_no_note_omits_reason(self, db_session):
        from app.services.buyplan_notifications import notify_so_rejected

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id)
        _make_request(db_session, plan)

        await notify_so_rejected(plan, db_session, action="reject")

        assert "Reason:" not in _outbox_rows(db_session)[0].payload["html"]

    @pytest.mark.asyncio
    async def test_no_submitter_skips(self, db_session):
        from app.services.buyplan_notifications import notify_so_rejected

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id, submitted_by_id=None)
        _make_request(db_session, plan)

        await notify_so_rejected(plan, db_session, action="reject")

        assert _outbox_rows(db_session) == []

    @pytest.mark.asyncio
    async def test_no_extra_delivery(self, db_session):
        from app.services.buyplan_notifications import notify_so_rejected

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id)
        _make_request(db_session, plan)

        with (
            patch("app.services.buyplan_notifications._send_email", new_callable=AsyncMock) as mock_email,
            patch("app.services.teams_notifications.send_teams_dm", new_callable=AsyncMock) as mock_dm,
        ):
            await notify_so_rejected(plan, db_session, action="halt")

        mock_email.assert_not_awaited()
        mock_dm.assert_not_awaited()
        assert db_session.query(ActivityLog).count() == 0
        assert len(_outbox_rows(db_session)) == 1


# ═══════════════════════════════════════════════════════════════════════
# notify_po_confirmed — single delivery: outbox email per active ops member
# ═══════════════════════════════════════════════════════════════════════


class TestNotifyPOConfirmed:
    @pytest.mark.asyncio
    async def test_enqueues_ops_email(self, db_session):
        from app.services.buyplan_notifications import notify_po_confirmed

        user = _make_user(db_session)
        ops_user = _make_user(db_session, "ops@trioscs.com", "Ops", "buyer")
        plan = _make_plan(db_session, user.id)
        line = _add_line(db_session, plan, po_number="PO-001")
        request = _make_request(db_session, plan, status=ApprovalRequestStatus.APPROVED)

        vgm = VerificationGroupMember(user_id=ops_user.id, is_active=True)
        db_session.add(vgm)
        db_session.commit()

        await notify_po_confirmed(plan, db_session, line.id)

        rows = _outbox_rows(db_session)
        assert len(rows) == 1
        assert rows[0].recipient_user_id == ops_user.id
        assert rows[0].request_id == request.id
        assert "PO-001" in rows[0].payload["subject"]
        assert db_session.query(ActivityLog).count() == 0

    @pytest.mark.asyncio
    async def test_no_ops_members(self, db_session):
        from app.services.buyplan_notifications import notify_po_confirmed

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id)
        line = _add_line(db_session, plan)
        _make_request(db_session, plan)

        await notify_po_confirmed(plan, db_session, line.id)

        assert _outbox_rows(db_session) == []

    @pytest.mark.asyncio
    async def test_inactive_member_skipped(self, db_session):
        from app.services.buyplan_notifications import notify_po_confirmed

        user = _make_user(db_session)
        ops_user = _make_user(db_session, "ops@trioscs.com", "Ops", "buyer")
        plan = _make_plan(db_session, user.id)
        line = _add_line(db_session, plan)
        _make_request(db_session, plan)

        vgm = VerificationGroupMember(user_id=ops_user.id, is_active=False)
        db_session.add(vgm)
        db_session.commit()

        await notify_po_confirmed(plan, db_session, line.id)

        assert _outbox_rows(db_session) == []


# ═══════════════════════════════════════════════════════════════════════
# notify_po_rejected — single delivery: outbox email to the line's buyer
# ═══════════════════════════════════════════════════════════════════════


class TestNotifyPORejected:
    @pytest.mark.asyncio
    async def test_enqueues_buyer_email(self, db_session):
        from app.services.buyplan_notifications import notify_po_rejected

        submitter = _make_user(db_session, "sales@trioscs.com", "Sales", "sales")
        buyer = _make_user(db_session, "buyer1@trioscs.com", "Buyer One", "buyer")
        plan = _make_plan(db_session, submitter.id)
        line = _add_line(db_session, plan, buyer_id=buyer.id, po_rejection_note="PO total mismatch")
        request = _make_request(db_session, plan, status=ApprovalRequestStatus.APPROVED)

        await notify_po_rejected(plan, db_session, line_id=line.id)

        rows = _outbox_rows(db_session)
        assert len(rows) == 1
        assert rows[0].recipient_user_id == buyer.id
        assert rows[0].request_id == request.id
        assert "Kicked Back" in rows[0].payload["subject"]
        assert "PO total mismatch" in rows[0].payload["html"]
        assert db_session.query(ActivityLog).count() == 0

    @pytest.mark.asyncio
    async def test_no_buyer_skips(self, db_session):
        from app.services.buyplan_notifications import notify_po_rejected

        submitter = _make_user(db_session, "sales@trioscs.com", "Sales", "sales")
        plan = _make_plan(db_session, submitter.id)
        line = _add_line(db_session, plan, buyer_id=None)
        _make_request(db_session, plan)

        await notify_po_rejected(plan, db_session, line_id=line.id)

        assert _outbox_rows(db_session) == []


# ═══════════════════════════════════════════════════════════════════════
# notify_completed — routine, still in-app only (non-approval event)
# ═══════════════════════════════════════════════════════════════════════


class TestNotifyCompleted:
    @pytest.mark.asyncio
    async def test_completed_is_routine_no_email(self, db_session):
        """Completion is a routine event — in-app only, no email (Task 10 demotion)."""
        from app.services.buyplan_notifications import notify_completed

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id)

        with patch("app.services.buyplan_notifications._send_email", new_callable=AsyncMock) as mock_email:
            await notify_completed(plan, db_session)

        mock_email.assert_not_awaited()
        assert _outbox_rows(db_session) == []

    @pytest.mark.asyncio
    async def test_completed_creates_activity(self, db_session):
        from app.services.buyplan_notifications import notify_completed

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id)

        await notify_completed(plan, db_session)

        activities = db_session.query(ActivityLog).filter_by(activity_type="buyplan_completed").all()
        assert len(activities) == 1

    @pytest.mark.asyncio
    async def test_completed_no_submitter(self, db_session):
        from app.services.buyplan_notifications import notify_completed

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id, submitted_by_id=None)

        await notify_completed(plan, db_session)

        assert db_session.query(ActivityLog).count() == 0


# ═══════════════════════════════════════════════════════════════════════
# log_buyplan_activity (audit trail — kept)
# ═══════════════════════════════════════════════════════════════════════


class TestLogBuyplanActivity:
    def test_creates_activity_record(self, db_session):
        from app.services.buyplan_notifications import log_buyplan_activity

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id, status="active")

        log_buyplan_activity(db_session, user.id, plan, "buyplan_approved", "Manager approved")
        db_session.commit()

        activities = db_session.query(ActivityLog).filter_by(activity_type="buyplan_approved").all()
        assert len(activities) == 1
        act = activities[0]
        assert act.user_id == user.id
        assert act.channel == "system"
        assert act.requisition_id == plan.requisition_id
        assert f"Buy Plan #{plan.id}: Manager approved" == act.subject
        assert f"plan_id={plan.id}" in act.notes
        assert "status=active" in act.notes

    def test_no_detail(self, db_session):
        from app.services.buyplan_notifications import log_buyplan_activity

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id)

        log_buyplan_activity(db_session, user.id, plan, "buyplan_submitted")
        db_session.commit()

        act = db_session.query(ActivityLog).filter_by(activity_type="buyplan_submitted").first()
        assert act is not None
        assert act.subject == f"Buy Plan #{plan.id}"

    def test_different_activity_types(self, db_session):
        from app.services.buyplan_notifications import log_buyplan_activity

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id)

        log_buyplan_activity(db_session, user.id, plan, "buyplan_rejected", "Price too high")
        db_session.commit()

        act = db_session.query(ActivityLog).filter_by(activity_type="buyplan_rejected").first()
        assert act is not None
        assert "Price too high" in act.subject


# ═══════════════════════════════════════════════════════════════════════
# notify_stock_sale_approved — single delivery: outbox email to the DLs
# ═══════════════════════════════════════════════════════════════════════


class TestNotifyStockSaleApproved:
    @pytest.mark.asyncio
    async def test_enqueues_dl_email_with_admin_sender(self, db_session):
        from app.services.buyplan_notifications import notify_stock_sale_approved

        submitter = _make_user(db_session)
        admin = _make_user(db_session, "admin@trioscs.com", "Admin", "admin")
        admin.access_token = "fake-token"
        db_session.commit()

        plan = _make_plan(db_session, submitter.id, approved_by_id=admin.id)
        _add_line(db_session, plan, quantity=50, unit_cost=3.00)
        request = _make_request(db_session, plan, status=ApprovalRequestStatus.APPROVED)

        with patch("app.services.buyplan_notifications.settings") as mock_settings:
            mock_settings.admin_emails = ["admin@trioscs.com"]
            mock_settings.stock_sale_notify_emails = ["logistics@trioscs.com", "accounting@trioscs.com"]
            await notify_stock_sale_approved(plan, db_session)

        rows = _outbox_rows(db_session)
        assert len(rows) == 1
        assert rows[0].recipient_user_id == admin.id  # delegated Graph sender
        assert rows[0].request_id == request.id
        assert rows[0].payload["to"] == ["logistics@trioscs.com", "accounting@trioscs.com"]
        assert "Stock Sale Approved" in rows[0].payload["subject"]
        assert db_session.query(ActivityLog).count() == 0

    @pytest.mark.asyncio
    async def test_no_dl_configured_skips(self, db_session):
        from app.services.buyplan_notifications import notify_stock_sale_approved

        submitter = _make_user(db_session)
        plan = _make_plan(db_session, submitter.id)
        _make_request(db_session, plan)

        with patch("app.services.buyplan_notifications.settings") as mock_settings:
            mock_settings.admin_emails = []
            mock_settings.stock_sale_notify_emails = []
            await notify_stock_sale_approved(plan, db_session)

        assert _outbox_rows(db_session) == []

    @pytest.mark.asyncio
    async def test_no_admin_user_skips(self, db_session):
        from app.services.buyplan_notifications import notify_stock_sale_approved

        submitter = _make_user(db_session)
        plan = _make_plan(db_session, submitter.id)
        _make_request(db_session, plan)

        with patch("app.services.buyplan_notifications.settings") as mock_settings:
            mock_settings.admin_emails = ["ghost@trioscs.com"]  # no such user
            mock_settings.stock_sale_notify_emails = ["logistics@trioscs.com"]
            await notify_stock_sale_approved(plan, db_session)

        assert _outbox_rows(db_session) == []

    @pytest.mark.asyncio
    async def test_admin_without_token_still_enqueues(self, db_session):
        """The dispatcher re-checks the token at send time — enqueue must not block."""
        from app.services.buyplan_notifications import notify_stock_sale_approved

        submitter = _make_user(db_session)
        admin = _make_user(db_session, "admin@trioscs.com", "Admin", "admin")
        plan = _make_plan(db_session, submitter.id)
        _make_request(db_session, plan)

        with patch("app.services.buyplan_notifications.settings") as mock_settings:
            mock_settings.admin_emails = ["admin@trioscs.com"]
            mock_settings.stock_sale_notify_emails = ["logistics@trioscs.com"]
            await notify_stock_sale_approved(plan, db_session)

        rows = _outbox_rows(db_session)
        assert len(rows) == 1
        assert rows[0].recipient_user_id == admin.id


# ═══════════════════════════════════════════════════════════════════════
# notify_cancelled — single delivery: outbox email to the submitter
# ═══════════════════════════════════════════════════════════════════════


class TestNotifyCancelled:
    @pytest.mark.asyncio
    async def test_enqueues_submitter_email(self, db_session, test_user, test_quote, test_requisition):
        from app.models.buy_plan import BuyPlanStatus
        from app.services.buyplan_notifications import notify_cancelled

        plan = BuyPlan(
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            status=BuyPlanStatus.CANCELLED.value,
            submitted_by_id=test_user.id,
            cancelled_by_id=test_user.id,
            cancellation_reason="dupe order",
        )
        db_session.add(plan)
        db_session.commit()
        request = _make_request(db_session, plan, status=ApprovalRequestStatus.CANCELLED)

        with patch("app.services.teams_notifications.send_teams_dm", new_callable=AsyncMock) as mock_dm:
            await notify_cancelled(plan, db_session)

        mock_dm.assert_not_awaited()
        rows = _outbox_rows(db_session)
        assert len(rows) == 1
        assert rows[0].recipient_user_id == test_user.id
        assert rows[0].request_id == request.id
        assert "Cancelled" in rows[0].payload["subject"]
        assert "dupe order" in rows[0].payload["html"]
        assert db_session.query(ActivityLog).count() == 0

    @pytest.mark.asyncio
    async def test_no_submitter_skips(self, db_session):
        from app.services.buyplan_notifications import notify_cancelled

        user = _make_user(db_session)
        plan = _make_plan(db_session, user.id, submitted_by_id=None)
        _make_request(db_session, plan)

        await notify_cancelled(plan, db_session)

        assert _outbox_rows(db_session) == []
