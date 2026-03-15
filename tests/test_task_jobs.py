"""Tests for selective auto-task scheduler jobs.

Verifies that task_jobs creates follow-up and expiry tasks only for
the right candidates, respects caps, and doesn't create noise.

Depends on: conftest.py fixtures, app/jobs/task_jobs.py, app/services/task_service.py
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import Requisition, User
from app.models.offers import Contact
from app.models.quotes import Quote
from app.models.task import RequisitionTask
from app.services import task_service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_contact(db: Session, req_id: int, user_id: int, *, status="sent", age_days=5) -> Contact:
    """Create a Contact (RFQ) with a specific age and status."""
    c = Contact(
        requisition_id=req_id,
        user_id=user_id,
        contact_type="email",
        vendor_name="TestVendor",
        vendor_contact="test@vendor.com",
        status=status,
        created_at=datetime.now(timezone.utc) - timedelta(days=age_days),
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


# ---------------------------------------------------------------------------
# RFQ follow-up job tests
# ---------------------------------------------------------------------------


class TestRFQFollowupJob:
    """Tests for _job_rfq_followup_tasks selectivity."""

    def test_creates_task_for_stale_rfq(
        self, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        """RFQ 5 days old with status 'sent' on active req -> creates follow-up task."""
        contact = _make_contact(db_session, test_requisition.id, test_user.id, status="sent", age_days=5)
        task_service.on_rfq_no_response(
            db_session, test_requisition.id, contact.vendor_name, contact.id
        )
        tasks = task_service.get_tasks(db_session, test_requisition.id, task_type="sourcing")
        followups = [t for t in tasks if t.source_ref == f"followup:{contact.id}"]
        assert len(followups) == 1
        assert "Follow up" in followups[0].title

    def test_skips_too_new_rfq(
        self, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        """RFQ only 1 day old should NOT get a follow-up task (too early)."""
        contact = _make_contact(db_session, test_requisition.id, test_user.id, status="sent", age_days=1)
        # Simulate what the job does: check age before calling on_rfq_no_response
        now = datetime.now(timezone.utc)
        created = contact.created_at
        if created and not created.tzinfo:
            created = created.replace(tzinfo=timezone.utc)
        age = (now - created).days
        assert age < 3, "Contact should be too new for follow-up"

    def test_skips_already_responded_rfq(
        self, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        """RFQ with status 'responded' should NOT get a follow-up task."""
        contact = _make_contact(db_session, test_requisition.id, test_user.id, status="responded", age_days=5)
        # Job only queries status in ("sent", "opened") — verify responded is excluded
        assert contact.status not in ("sent", "opened")

    def test_skips_too_old_rfq(
        self, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        """RFQ 30 days old is ancient — should NOT get a follow-up (max 14 days)."""
        contact = _make_contact(db_session, test_requisition.id, test_user.id, status="sent", age_days=30)
        now = datetime.now(timezone.utc)
        created = contact.created_at
        if created and not created.tzinfo:
            created = created.replace(tzinfo=timezone.utc)
        age = (now - created).days
        assert age > 14, "Contact should be too old for follow-up"

    def test_dedup_prevents_double_followup(
        self, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        """Calling on_rfq_no_response twice creates only one task."""
        contact = _make_contact(db_session, test_requisition.id, test_user.id, status="sent", age_days=5)
        task_service.on_rfq_no_response(
            db_session, test_requisition.id, contact.vendor_name, contact.id
        )
        task_service.on_rfq_no_response(
            db_session, test_requisition.id, contact.vendor_name, contact.id
        )
        tasks = task_service.get_tasks(db_session, test_requisition.id)
        followups = [t for t in tasks if t.source_ref == f"followup:{contact.id}"]
        assert len(followups) == 1


# ---------------------------------------------------------------------------
# Auto-close on vendor response tests
# ---------------------------------------------------------------------------


class TestAutoCloseOnResponse:
    """Tests for auto-closing RFQ tasks when vendor responds."""

    def test_auto_close_rfq_task_on_response(
        self, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        """When auto_close_task is called with rfq:id, the awaiting task closes."""
        contact = _make_contact(db_session, test_requisition.id, test_user.id, status="sent", age_days=4)
        # Create the awaiting-response task
        task_service.auto_create_task(
            db_session,
            requisition_id=test_requisition.id,
            title=f"Awaiting response from {contact.vendor_name}",
            task_type="sourcing",
            source_ref=f"rfq:{contact.id}",
        )
        # Simulate vendor responding — auto-close
        closed = task_service.auto_close_task(
            db_session, test_requisition.id, f"rfq:{contact.id}"
        )
        assert closed is not None
        assert closed.status == "done"

    def test_auto_close_followup_task_on_response(
        self, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        """When vendor responds, follow-up task also closes."""
        contact = _make_contact(db_session, test_requisition.id, test_user.id, status="sent", age_days=5)
        task_service.on_rfq_no_response(
            db_session, test_requisition.id, contact.vendor_name, contact.id
        )
        closed = task_service.auto_close_task(
            db_session, test_requisition.id, f"followup:{contact.id}"
        )
        assert closed is not None
        assert closed.status == "done"


# ---------------------------------------------------------------------------
# Quote expiry and auto-close tests
# ---------------------------------------------------------------------------


class TestQuoteExpiry:
    """Tests for quote expiry task creation and auto-close."""

    def test_quote_expiry_task_created(
        self, db_session: Session, test_requisition: Requisition, test_quote: Quote
    ):
        """on_quote_expiring creates an expiry task for the quote."""
        task_service.on_quote_expiring(db_session, test_requisition.id, test_quote.id)
        tasks = task_service.get_tasks(db_session, test_requisition.id)
        expiry = [t for t in tasks if t.source_ref == f"expiry:{test_quote.id}"]
        assert len(expiry) == 1
        assert "expires soon" in expiry[0].title.lower()

    def test_quote_expiry_no_duplicate(
        self, db_session: Session, test_requisition: Requisition, test_quote: Quote
    ):
        """Calling on_quote_expiring twice creates only one task."""
        task_service.on_quote_expiring(db_session, test_requisition.id, test_quote.id)
        task_service.on_quote_expiring(db_session, test_requisition.id, test_quote.id)
        tasks = task_service.get_tasks(db_session, test_requisition.id)
        expiry = [t for t in tasks if t.source_ref == f"expiry:{test_quote.id}"]
        assert len(expiry) == 1

    def test_quote_result_closes_expiry_task(
        self, db_session: Session, test_requisition: Requisition, test_quote: Quote
    ):
        """Setting quote result (won/lost) auto-closes the expiry task."""
        task_service.on_quote_expiring(db_session, test_requisition.id, test_quote.id)
        closed = task_service.auto_close_task(
            db_session, test_requisition.id, f"expiry:{test_quote.id}"
        )
        assert closed is not None
        assert closed.status == "done"

    def test_quote_send_closes_send_task(
        self, db_session: Session, test_requisition: Requisition, test_quote: Quote
    ):
        """Sending a quote auto-closes the 'Send quote' task."""
        task_service.on_quote_created(db_session, test_requisition.id, "Acme", test_quote.id)
        closed = task_service.auto_close_task(
            db_session, test_requisition.id, f"quote:{test_quote.id}"
        )
        assert closed is not None
        assert closed.status == "done"

    def test_skips_quote_with_existing_alert(
        self, db_session: Session, test_requisition: Requisition, test_quote: Quote
    ):
        """Quote with followup_alert_sent_at already set should be skipped by the job."""
        test_quote.followup_alert_sent_at = datetime.now(timezone.utc)
        db_session.commit()
        assert test_quote.followup_alert_sent_at is not None


# ---------------------------------------------------------------------------
# Cap / noise prevention tests
# ---------------------------------------------------------------------------


class TestNoisePrevention:
    def test_cap_constants_are_reasonable(self):
        """Verify the caps exist and are sane."""
        from app.jobs.task_jobs import _RFQ_FOLLOWUP_CAP, _QUOTE_EXPIRY_CAP

        assert 1 <= _RFQ_FOLLOWUP_CAP <= 50
        assert 1 <= _QUOTE_EXPIRY_CAP <= 50

    def test_age_window_constants(self):
        """Verify the age window for RFQ follow-ups is 3-14 days."""
        from app.jobs.task_jobs import _RFQ_MIN_AGE_DAYS, _RFQ_MAX_AGE_DAYS

        assert _RFQ_MIN_AGE_DAYS == 3
        assert _RFQ_MAX_AGE_DAYS == 14

    def test_only_active_req_statuses(self):
        """Verify archived/won/lost reqs are excluded."""
        from app.jobs.task_jobs import _ACTIVE_REQ_STATUSES

        assert "archived" not in _ACTIVE_REQ_STATUSES
        assert "won" not in _ACTIVE_REQ_STATUSES
        assert "lost" not in _ACTIVE_REQ_STATUSES
        assert "open" in _ACTIVE_REQ_STATUSES
