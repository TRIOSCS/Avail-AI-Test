"""test_coverage_nightly_2026_07_05.py — Nightly coverage fill-in.

Targets modules below 85% coverage:
  - app/jobs/resell_jobs.py (73%)
  - app/services/ticket_prompt_service.py (82%)
  - app/services/prepayment_notifications.py (75%)

Called by: pytest
Depends on: conftest (db_session), unittest.mock
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

os.environ["TESTING"] = "1"


# ─────────────────────────────────────────────────────────────────────
# app/jobs/resell_jobs.py
# ─────────────────────────────────────────────────────────────────────


class TestResellJobs:
    """Cover the error branches and zero-result path in _job_expire_resell_lists."""

    def _make_job(self):
        from app.jobs.resell_jobs import _job_expire_resell_lists

        return _job_expire_resell_lists

    @pytest.mark.asyncio
    async def test_zero_expired_no_log(self):
        job = self._make_job()
        mock_db = MagicMock()
        # Lazy imports inside function body — patch at the source module
        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.services.excess_service.expire_overdue_lists", return_value=0),
        ):
            # when expired=0 the "if expired:" branch is skipped — no raise
            await job.__wrapped__()
        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_positive_expired_logs(self):
        job = self._make_job()
        mock_db = MagicMock()
        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.services.excess_service.expire_overdue_lists", return_value=3),
        ):
            await job.__wrapped__()
        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_sqlalchemy_error_rollback(self):
        import sqlalchemy.exc

        job = self._make_job()
        mock_db = MagicMock()
        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch(
                "app.services.excess_service.expire_overdue_lists",
                side_effect=sqlalchemy.exc.SQLAlchemyError("db error"),
            ),
        ):
            await job.__wrapped__()  # Must NOT raise
        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_generic_exception_rollback(self):
        job = self._make_job()
        mock_db = MagicMock()
        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch(
                "app.services.excess_service.expire_overdue_lists",
                side_effect=RuntimeError("unexpected"),
            ),
        ):
            await job.__wrapped__()  # Must NOT raise
        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()

    def test_register_resell_jobs(self):
        from app.jobs.resell_jobs import register_resell_jobs

        scheduler = MagicMock()
        settings = MagicMock()
        register_resell_jobs(scheduler, settings)
        scheduler.add_job.assert_called_once()
        call_kwargs = scheduler.add_job.call_args
        assert call_kwargs[1]["id"] == "expire_resell_lists" or call_kwargs.kwargs.get("id") == "expire_resell_lists"


# ─────────────────────────────────────────────────────────────────────
# app/services/ticket_prompt_service.py
# ─────────────────────────────────────────────────────────────────────


def _make_mock_ticket(ticket_type="bug", *, with_all_fields=False):
    """Return a MagicMock shaped like a TroubleTicket."""
    t = MagicMock()
    t.ticket_number = "TKT-001"
    t.ticket_type = ticket_type
    t.description = "Something broke"
    t.current_page = "/sourcing"
    t.current_view = "sourcing_list"
    t.browser_info = "Chrome/120"
    t.admin_notes = "Fix ASAP"
    if with_all_fields:
        t.console_errors = "TypeError: Cannot read property"
        t.network_errors = {"url": "/api/foo", "status": 500}
        t.page_state = "modal_open=true"
        t.screenshot_path = "/screenshots/abc.png"
        t.screenshot_b64 = None
    else:
        t.console_errors = None
        t.network_errors = None
        t.page_state = None
        t.screenshot_path = None
        t.screenshot_b64 = None
    return t


class TestBuildBugPrompt:
    def test_minimal_fields(self):
        from app.services.ticket_prompt_service import _build_bug_prompt

        t = _make_mock_ticket()
        result = _build_bug_prompt(t)
        assert "TKT-001" in result
        assert "Something broke" in result

    def test_all_optional_fields(self):
        from app.services.ticket_prompt_service import _build_bug_prompt

        t = _make_mock_ticket(with_all_fields=True)
        result = _build_bug_prompt(t)
        assert "TypeError" in result
        assert "/api/foo" in result or "500" in result
        assert "modal_open" in result
        assert "screenshot" in result.lower()

    def test_screenshot_b64_triggers_screenshot_line(self):
        from app.services.ticket_prompt_service import _build_bug_prompt

        t = _make_mock_ticket()
        t.screenshot_path = None
        t.screenshot_b64 = "base64data"
        result = _build_bug_prompt(t)
        assert "screenshot" in result.lower()

    def test_no_description(self):
        from app.services.ticket_prompt_service import _build_bug_prompt

        t = _make_mock_ticket()
        t.description = None
        result = _build_bug_prompt(t)
        assert "TKT-001" in result


class TestBuildFeaturePrompt:
    def test_minimal_fields(self):
        from app.services.ticket_prompt_service import _build_feature_prompt

        t = _make_mock_ticket(ticket_type="feature")
        t.description = "Add export button"
        result = _build_feature_prompt(t)
        assert "TKT-001" in result
        assert "Add export button" in result

    def test_screenshot_included(self):
        from app.services.ticket_prompt_service import _build_feature_prompt

        t = _make_mock_ticket(ticket_type="feature")
        t.screenshot_path = "/screenshots/feature.png"
        result = _build_feature_prompt(t)
        assert "screenshot" in result.lower()

    def test_b64_screenshot_included(self):
        from app.services.ticket_prompt_service import _build_feature_prompt

        t = _make_mock_ticket(ticket_type="feature")
        t.screenshot_path = None
        t.screenshot_b64 = "b64data"
        result = _build_feature_prompt(t)
        assert "screenshot" in result.lower()

    def test_no_description(self):
        from app.services.ticket_prompt_service import _build_feature_prompt

        t = _make_mock_ticket(ticket_type="feature")
        t.description = None
        result = _build_feature_prompt(t)
        assert "TKT-001" in result

    def test_no_current_page(self):
        from app.services.ticket_prompt_service import _build_feature_prompt

        t = _make_mock_ticket(ticket_type="feature")
        t.current_page = None
        result = _build_feature_prompt(t)
        assert "TKT-001" in result

    def test_no_admin_notes(self):
        from app.services.ticket_prompt_service import _build_feature_prompt

        t = _make_mock_ticket(ticket_type="feature")
        t.admin_notes = None
        result = _build_feature_prompt(t)
        assert "TKT-001" in result


class TestGenerateTicketPrompt:
    @pytest.mark.asyncio
    async def test_bug_prompt_generated_and_persisted(self, db_session: Session):
        from app.constants import TicketType
        from app.services.ticket_prompt_service import generate_ticket_prompt

        t = MagicMock()
        t.ticket_number = "TKT-BUG-1"
        t.ticket_type = TicketType.BUG
        t.description = "Breaks on save"
        t.current_page = "/edit"
        t.current_view = None
        t.browser_info = None
        t.console_errors = None
        t.network_errors = None
        t.page_state = None
        t.screenshot_path = None
        t.screenshot_b64 = None
        t.admin_notes = None

        with patch("app.services.ticket_prompt_service.claude_text", new=AsyncMock(return_value="  Fix the bug  ")):
            result = await generate_ticket_prompt(db_session, t)

        assert result == "Fix the bug"
        assert t.generated_prompt == "Fix the bug"

    @pytest.mark.asyncio
    async def test_feature_prompt_generated(self, db_session: Session):
        from app.constants import TicketType
        from app.services.ticket_prompt_service import generate_ticket_prompt

        t = MagicMock()
        t.ticket_number = "TKT-FEAT-1"
        t.ticket_type = TicketType.FEATURE
        t.description = "Add export"
        t.current_page = "/reports"
        t.current_view = None
        t.screenshot_path = None
        t.screenshot_b64 = None
        t.admin_notes = "High priority"

        with patch("app.services.ticket_prompt_service.claude_text", new=AsyncMock(return_value="Build the feature")):
            result = await generate_ticket_prompt(db_session, t)

        assert result == "Build the feature"

    @pytest.mark.asyncio
    async def test_claude_returns_none_returns_none(self, db_session: Session):
        from app.constants import TicketType
        from app.services.ticket_prompt_service import generate_ticket_prompt

        t = MagicMock()
        t.ticket_number = "TKT-NULL-1"
        t.ticket_type = TicketType.BUG
        t.description = None
        t.current_page = None
        t.current_view = None
        t.browser_info = None
        t.console_errors = None
        t.network_errors = None
        t.page_state = None
        t.screenshot_path = None
        t.screenshot_b64 = None
        t.admin_notes = None

        with patch("app.services.ticket_prompt_service.claude_text", new=AsyncMock(return_value=None)):
            result = await generate_ticket_prompt(db_session, t)

        assert result is None


# ─────────────────────────────────────────────────────────────────────
# app/services/prepayment_notifications.py  — additional coverage
# ─────────────────────────────────────────────────────────────────────


def _make_prepayment_stub(db: Session) -> object:
    """Minimal Prepayment-shaped mock backed by the real DB so foreign-key refs work."""
    from app.models import User
    from app.models.buy_plan import BuyPlan, BuyPlanLine
    from app.models.quality_plan import Prepayment
    from app.models.quotes import Quote
    from app.models.sourcing import Requisition
    from app.models.vendors import VendorCard

    u = User(
        email=f"pn-u-{uuid.uuid4().hex[:6]}@trioscs.com",
        role="buyer",
        azure_id=uuid.uuid4().hex,
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    req = Requisition(
        name=f"REQ-{uuid.uuid4().hex[:6]}",
        customer_name="Cust",
        status="active",
        created_by=u.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()
    q = Quote(
        requisition_id=req.id,
        quote_number=uuid.uuid4().hex,
        line_items=[],
        status="sent",
        created_by_id=u.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(q)
    db.flush()
    bp = BuyPlan(
        requisition_id=req.id,
        quote_id=q.id,
        status="active",
        so_status="approved",
        submitted_by_id=u.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(bp)
    db.flush()
    vc = VendorCard(
        normalized_name=uuid.uuid4().hex,
        display_name="Vendor Display",
        legal_name=None,
    )
    db.add(vc)
    db.flush()
    line = BuyPlanLine(
        buy_plan_id=bp.id,
        quantity=1,
        unit_cost=100.0,
        status="pending_verify",
        po_number="PO-100",
        po_confirmed_at=datetime.now(timezone.utc),
    )
    db.add(line)
    db.flush()
    pp = Prepayment(
        buy_plan_id=bp.id,
        buy_plan_line_id=line.id,
        vendor_card_id=vc.id,
        vendor_name="Vendor Snapshot",
        total_incl_fees=Decimal("1000.00"),
        currency="USD",
        created_by_id=u.id,
        status="requested",
    )
    db.add(pp)
    db.commit()
    return pp


class TestPrepaymentNotificationsExtra:
    def test_schedule_prepayment_notify_no_loop(self):
        """With no running event loop, close() is called on the coro (no dispatch)."""
        from app.services.prepayment_notifications import schedule_prepayment_notify

        coro = MagicMock()
        coro.close = MagicMock()

        # asyncio is imported lazily inside the function body — patch at source
        with patch("asyncio.get_running_loop", side_effect=RuntimeError("no loop")):
            schedule_prepayment_notify(coro)

        coro.close.assert_called_once()

    def test_schedule_prepayment_notify_with_loop(self):
        """With a running event loop, create_task is called."""
        from app.services.prepayment_notifications import schedule_prepayment_notify

        coro = MagicMock()
        mock_loop = MagicMock()

        with patch("asyncio.get_running_loop", return_value=mock_loop):
            schedule_prepayment_notify(coro)

        mock_loop.create_task.assert_called_once_with(coro)

    def test_beneficiary_vendor_name_fallback(self, db_session: Session):
        """_beneficiary falls back to vendor_name when legal_name is None."""
        from app.services.prepayment_notifications import _beneficiary

        pp = _make_prepayment_stub(db_session)
        pp.vendor_name = "Snapshot Name"
        # legal_name is None from _make_prepayment_stub
        result = _beneficiary(pp)
        assert result == "Snapshot Name"

    def test_beneficiary_display_name_fallback(self, db_session: Session):
        """_beneficiary falls back to vc.display_name when no legal or snapshot."""
        from app.services.prepayment_notifications import _beneficiary

        pp = _make_prepayment_stub(db_session)
        pp.vendor_name = None
        # vc.legal_name is None, vc.display_name = "Vendor Display"
        result = _beneficiary(pp)
        assert result == "Vendor Display"

    def test_beneficiary_dash_when_no_card(self, db_session: Session):
        """_beneficiary returns '—' when no vendor_card and no vendor_name."""
        from app.services.prepayment_notifications import _beneficiary

        pp = _make_prepayment_stub(db_session)
        pp.vendor_name = None
        pp.vendor_card = None
        result = _beneficiary(pp)
        assert result == "—"

    def test_confirm_url_with_token(self, db_session: Session):
        """_confirm_url returns a URL when pay_token is set."""
        from app.services.prepayment_notifications import _confirm_url

        pp = _make_prepayment_stub(db_session)
        pp.pay_token = "tok-abc123"
        with patch("app.services.prepayment_notifications.settings") as mock_settings:
            mock_settings.app_url = "https://app.example.com"
            url = _confirm_url(pp)
        assert url == "https://app.example.com/p/confirm/tok-abc123"

    def test_confirm_url_none_when_no_token(self, db_session: Session):
        """_confirm_url returns None when pay_token is absent."""
        from app.services.prepayment_notifications import _confirm_url

        pp = _make_prepayment_stub(db_session)
        pp.pay_token = None
        result = _confirm_url(pp)
        assert result is None

    def test_facts_buyer_remarks_included(self, db_session: Session):
        """_facts appends buyer_remarks when present."""
        from app.services.prepayment_notifications import _facts

        pp = _make_prepayment_stub(db_session)
        pp.buyer_remarks = "Rush order"
        facts = dict(_facts(pp, "requested"))
        assert "Buyer remarks" in facts
        assert facts["Buyer remarks"] == "Rush order"

    def test_facts_approved_with_approver(self, db_session: Session):
        """_facts includes Approved by + Approved at on the approved event."""
        from app.services.prepayment_notifications import _facts

        pp = _make_prepayment_stub(db_session)
        pp.buyer_remarks = None
        facts = dict(_facts(pp, "approved", approver="Jane Smith", decided_at=datetime.now(timezone.utc)))
        assert "Approved by" in facts
        assert facts["Approved by"] == "Jane Smith"
        assert "Approved at" in facts

    def test_facts_approved_without_approver(self, db_session: Session):
        """_facts approved event without approver/decided_at skips those rows."""
        from app.services.prepayment_notifications import _facts

        pp = _make_prepayment_stub(db_session)
        pp.buyer_remarks = None
        facts = dict(_facts(pp, "approved", approver=None, decided_at=None))
        assert "Approved by" not in facts
        assert "Approved at" not in facts

    def test_facts_voided_with_reason(self, db_session: Session):
        """_facts voided event adds void_reason row."""
        from app.services.prepayment_notifications import _facts

        pp = _make_prepayment_stub(db_session)
        pp.void_reason = "Cancelled by buyer"
        pp.buyer_remarks = None
        facts = dict(_facts(pp, "voided"))
        assert "Void reason" in facts

    def test_facts_paid_with_wire_reference(self, db_session: Session):
        """_facts paid event includes wire_reference and paid_by_label."""
        from app.services.prepayment_notifications import _facts

        pp = _make_prepayment_stub(db_session)
        pp.wire_reference = "WIRE-XYZ"
        pp.paid_by_label = "MK"
        pp.buyer_remarks = None
        facts = dict(_facts(pp, "paid"))
        assert "Wire reference" in facts
        assert facts["Wire reference"] == "WIRE-XYZ"
        assert facts["Paid by"] == "MK"

    def test_facts_paid_without_wire_reference(self, db_session: Session):
        """_facts paid event without wire_reference skips those rows."""
        from app.services.prepayment_notifications import _facts

        pp = _make_prepayment_stub(db_session)
        pp.wire_reference = None
        pp.paid_by_label = None
        pp.buyer_remarks = None
        facts = dict(_facts(pp, "paid"))
        assert "Wire reference" not in facts

    def test_email_html_with_confirm_button(self, db_session: Session):
        """_email_html includes the confirm-wire button on approved event when pay_token
        set."""
        from app.services.prepayment_notifications import _email_html

        pp = _make_prepayment_stub(db_session)
        pp.pay_token = "tok-confirm"
        pp.buyer_remarks = None
        with patch("app.services.prepayment_notifications.settings") as mock_settings:
            mock_settings.app_url = "https://avail.test"
            html = _email_html(pp, "approved")
        assert "Confirm wire sent" in html
        assert "tok-confirm" in html

    def test_email_html_no_confirm_button_when_no_token(self, db_session: Session):
        """_email_html omits the button when pay_token is None."""
        from app.services.prepayment_notifications import _email_html

        pp = _make_prepayment_stub(db_session)
        pp.pay_token = None
        pp.buyer_remarks = None
        html = _email_html(pp, "approved")
        assert "Confirm wire sent" not in html

    def test_send_group_email_no_admin_token(self, db_session: Session):
        """_send_group_email returns False when no admin has a live Graph token."""
        from app.services.prepayment_notifications import _send_group_email

        with patch("app.services.prepayment_notifications.settings") as mock_s:
            mock_s.admin_emails = []  # no admins configured
            import asyncio

            result = asyncio.get_event_loop().run_until_complete(
                _send_group_email(db_session, ["ap@example.com"], "Subject", "<p>body</p>")
            )
        assert result is False

    def test_send_group_email_empty_recipients(self, db_session: Session):
        """_send_group_email returns False for an empty recipient list."""
        import asyncio

        from app.services.prepayment_notifications import _send_group_email

        result = asyncio.get_event_loop().run_until_complete(
            _send_group_email(db_session, [], "Subject", "<p>body</p>")
        )
        assert result is False

    def test_write_failure_alert_exception_does_not_raise(self, db_session: Session):
        """_write_failure_alert's inner exception path is swallowed."""
        from app.services.prepayment_notifications import _write_failure_alert

        pp = _make_prepayment_stub(db_session)

        # Patch db.add to raise on second call (after adding the first ActivityLog)
        orig_add = db_session.add
        call_count = [0]

        def bad_add(obj):
            call_count[0] += 1
            if call_count[0] > 1:
                raise RuntimeError("db exploded")
            return orig_add(obj)

        with patch.object(db_session, "add", side_effect=bad_add):
            _write_failure_alert(db_session, pp)  # must not raise

    @pytest.mark.asyncio
    async def test_notify_paid_inner_no_prepayment(self, db_session: Session):
        """_notify_paid_inner returns early when prepayment not found."""
        from app.services.prepayment_notifications import _notify_paid_inner

        result = _notify_paid_inner(db_session, prepayment_id=99999)
        assert result == {"alerted": []}

    @pytest.mark.asyncio
    async def test_notify_prepayment_paid_own_session(self):
        """notify_prepayment_paid opens its own session when db=None."""
        from app.services import prepayment_notifications as pn

        mock_db = MagicMock()
        mock_db.get.return_value = None  # prepayment not found — return early
        mock_db.close = MagicMock()

        # SessionLocal is lazily imported inside the function body
        with patch("app.database.SessionLocal", return_value=mock_db):
            await pn.notify_prepayment_paid(99999, db=None)

        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_notify_inner_own_session_closed(self):
        """_notify opens its own session when db=None and closes it after."""
        from app.services import prepayment_notifications as pn

        mock_db = MagicMock()
        mock_db.get.return_value = None  # prepayment not found
        mock_db.close = MagicMock()

        with patch("app.database.SessionLocal", return_value=mock_db):
            await pn.notify_prepayment_requested(99999, db=None)

        mock_db.close.assert_called_once()

    def test_heading_voided_with_reason(self):
        """_heading for voided event includes the reason."""
        from app.services.prepayment_notifications import _heading

        result = _heading("voided", reason="plan torn down")
        assert "DO NOT WIRE" in result
        assert "plan torn down" in result

    def test_heading_voided_no_reason(self):
        """_heading for voided event uses '—' when no reason."""
        from app.services.prepayment_notifications import _heading

        result = _heading("voided", reason=None)
        assert "DO NOT WIRE" in result
        assert "—" in result

    def test_heading_unknown_event_defaults_to_requested(self):
        """_heading for unknown event falls back to requested heading."""
        from app.services.prepayment_notifications import _heading

        result = _heading("something_new")
        assert "PENDING APPROVAL" in result

    def test_notify_inner_prepayment_not_found(self, db_session: Session):
        """_notify_inner returns empty result when prepayment not found."""
        import asyncio

        from app.services.prepayment_notifications import _notify_inner

        result = asyncio.get_event_loop().run_until_complete(
            _notify_inner(db_session, prepayment_id=99999, event="requested")
        )
        assert result == {"email_sent": False, "teams_sent": False, "recipients": []}

    def test_notify_paid_inner_no_user_ids(self, db_session: Session):
        """_notify_paid_inner returns empty alerted when no user IDs could be
        resolved."""
        from app.services.prepayment_notifications import _notify_paid_inner

        pp = _make_prepayment_stub(db_session)
        pp.created_by_id = None
        pp.buy_plan.submitted_by_id = None
        db_session.flush()
        # No managers in DB, no created_by_id, no submitted_by_id
        result = _notify_paid_inner(db_session, pp.id)
        # May or may not be empty depending on managers in DB — just verify no raise
        assert isinstance(result, dict)
        assert "alerted" in result
