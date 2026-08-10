"""tests/test_remediation_p1.py — QC 2026-08-10 P1 (money-path) regressions.

- P1-1a a VOIDED prepayment no longer blocks a fresh request on the line (the
  guard keys on the prepayment's own status, not the ApprovalRequest row that
  teardown leaves APPROVED forever); a live one still blocks.
- P1-1b sending a PO back voids that line's live prepayment (money-safety sweep).
- P1-4 the requester's approval-decision email carries the decision, amount,
  reason (previously discarded), and a deep link back.

Called by: pytest autodiscovery
Depends on: conftest fixtures + tests.test_prepayment builders.
"""

from decimal import Decimal

import pytest

from tests.conftest import engine  # noqa: F401
from tests.test_prepayment import _make_approver, _make_buy_plan, _make_po_line

# ── P1-4 · requester decision email (pure function) ──────────────────────


def test_decision_email_carries_reason_gate_and_link():
    from app.services.approvals.notifications import _build_email_html

    subject, html = _build_email_html(
        {
            "decision": "rejected",
            "comment": "Vendor docs incomplete — resend the test report.",
            "gate_type": "prepayment",
            "request_id": 4021,
            "amount": "12500.00",
        }
    )
    assert "[AVAIL]" in subject and "Prepayment" in subject and "4021" in subject
    assert "REJECTED" in html
    assert "Vendor docs incomplete" in html  # the reason is NO LONGER discarded
    assert "12,500" in html or "12500" in html  # amount shown
    assert "/v2/approvals" in html  # deep link back


def test_decision_email_escapes_reason():
    from app.services.approvals.notifications import _build_email_html

    _subject, html = _build_email_html(
        {"decision": "rejected", "comment": "<script>alert(1)</script>", "gate_type": "buy_plan", "request_id": 1}
    )
    assert "<script>" not in html and "&lt;script&gt;" in html


# ── P1-1a · voided prepayment stops blocking a re-request ─────────────────


def _seed_line_with_approver(db, test_user):
    _make_approver(db, email="mgr@trioscs.com", name="Mgr", azure_id="az-mgr", limit=None)
    db.commit()
    bp = _make_buy_plan(db, test_user)
    line = _make_po_line(db, bp)
    db.commit()
    return bp, line


def test_voided_prepayment_allows_a_new_request(db_session, test_user):
    from app.constants import PrepaymentStatus
    from app.services.prepayment_service import create_prepayment

    bp, line = _seed_line_with_approver(db_session, test_user)
    pp, _req = create_prepayment(
        db_session,
        buy_plan_id=bp.id,
        buy_plan_line_id=line.id,
        vendor_card_id=None,
        payment_method="wire",
        total_incl_fees=Decimal("400.00"),
        test_report_sent=False,
        buyer_remarks="x",
        created_by=test_user,
    )
    db_session.commit()

    # Void it (teardown leaves the ApprovalRequest APPROVED — the old bug).
    pp.status = PrepaymentStatus.VOID.value
    db_session.commit()

    # A fresh request on the SAME line must now succeed (was a permanent 400).
    pp2, _req2 = create_prepayment(
        db_session,
        buy_plan_id=bp.id,
        buy_plan_line_id=line.id,
        vendor_card_id=None,
        payment_method="wire",
        total_incl_fees=Decimal("400.00"),
        test_report_sent=False,
        buyer_remarks="retry",
        created_by=test_user,
    )
    assert pp2.id != pp.id
    assert pp2.status == PrepaymentStatus.REQUESTED.value


def test_live_prepayment_still_blocks_a_duplicate(db_session, test_user):
    from app.services.prepayment_service import create_prepayment

    bp, line = _seed_line_with_approver(db_session, test_user)
    create_prepayment(
        db_session,
        buy_plan_id=bp.id,
        buy_plan_line_id=line.id,
        vendor_card_id=None,
        payment_method="wire",
        total_incl_fees=Decimal("400.00"),
        test_report_sent=False,
        buyer_remarks="x",
        created_by=test_user,
    )
    db_session.commit()

    with pytest.raises(ValueError, match="already awaiting approval, approved, or paid"):
        create_prepayment(
            db_session,
            buy_plan_id=bp.id,
            buy_plan_line_id=line.id,
            vendor_card_id=None,
            payment_method="wire",
            total_incl_fees=Decimal("400.00"),
            test_report_sent=False,
            buyer_remarks="dup",
            created_by=test_user,
        )


# ── P1-1b · PO send-back voids the line's live prepayment ────────────────


def test_po_reject_voids_the_lines_prepayment(db_session, test_user):
    from app.constants import PrepaymentStatus
    from app.services.buyplan_workflow.buyplan_po import verify_po
    from app.services.prepayment_service import create_prepayment

    bp, line = _seed_line_with_approver(db_session, test_user)
    pp, _req = create_prepayment(
        db_session,
        buy_plan_id=bp.id,
        buy_plan_line_id=line.id,
        vendor_card_id=None,
        payment_method="wire",
        total_incl_fees=Decimal("400.00"),
        test_report_sent=False,
        buyer_remarks="x",
        created_by=test_user,
    )
    # Simulate an approved (about-to-wire) prepayment.
    pp.status = PrepaymentStatus.APPROVED.value
    # verify_po requires the PO-approval right.
    test_user.can_approve_purchase_orders = True
    db_session.commit()

    verify_po(bp.id, line.id, "reject", test_user, db_session, rejection_note="wrong vendor")
    db_session.commit()

    db_session.refresh(pp)
    # Sending the PO back must stand the wire down — a manager can't wire to a pulled PO.
    assert pp.status == PrepaymentStatus.VOID.value
