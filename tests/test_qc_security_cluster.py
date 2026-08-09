"""tests/test_qc_security_cluster.py — QC 2026-08-08 cluster 3 regressions.

- Attachment stored-XSS: served content type is derived from the (validated)
  extension, never the attacker-controlled client header; non-image/pdf types
  download with X-Content-Type-Options: nosniff.
- Approvals reassign() authority: a slot may only be handed to a user genuinely
  eligible for the gate at the amount.
- Approvals decide() prepayment lifecycle: a prepayment decided through the
  engine is stamped APPROVED + pay_token (or VOID), not left 'requested'.

The two XSS template fixes are structural (data-* + $el.dataset); their inertness
is proven in the QC session against the app Jinja env — asserted here at the
template-render level for the requisitions filter params.

Called by: pytest autodiscovery
Depends on: conftest fixtures (db_session, test_user)
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services.attachment_service import _INLINE_SAFE_TYPES, _trusted_content_type
from tests.conftest import engine  # noqa: F401

# ── Attachment stored-XSS ────────────────────────────────────────────────


def test_content_type_is_extension_derived_not_client_supplied():
    # A .csv the attacker labeled text/html serves as text/csv, and CSV is not
    # inline-safe, so it downloads.
    assert _trusted_content_type("evil.csv") == "text/csv"
    assert "text/csv" not in _INLINE_SAFE_TYPES
    # .txt with HTML content likewise never renders as HTML.
    assert _trusted_content_type("payload.txt") == "text/plain"
    assert "text/plain" not in _INLINE_SAFE_TYPES


def test_only_images_and_pdf_are_inline_safe():
    for name, ct in [("a.pdf", "application/pdf"), ("b.png", "image/png"), ("c.jpg", "image/jpeg")]:
        assert _trusted_content_type(name) == ct
        assert ct in _INLINE_SAFE_TYPES


def test_unknown_extension_falls_back_to_octet_stream():
    assert _trusted_content_type("weird.svg") == "application/octet-stream"
    assert _trusted_content_type(None) == "application/octet-stream"


# ── XSS template inertness (requisitions filter params) ──────────────────


def test_requisitions_filter_params_render_inert_in_data_attribute():
    from app.template_env import templates

    env = templates.env
    payload = "'; alert(document.cookie); //"
    rendered = env.from_string('data-status="{{ status }}"').render(status=payload)
    # HTML-escaped in the attribute — no raw quote to break out of; $el.dataset
    # reads it as a pure string, never evaluated.
    assert "alert(document.cookie)" in rendered
    assert "&#39;" in rendered
    assert "data-status=\"'" not in rendered  # the breakout quote never appears raw


# ── Approvals reassign() authority ───────────────────────────────────────


def _approver(db, email, azure, *, prepay=True, limit=None):
    from app.models import User

    u = User(
        email=email,
        name=email.split("@")[0],
        role="manager",
        azure_id=azure,
        is_active=True,
        can_approve_prepayments=prepay,
        prepayment_approval_limit=limit,
        created_at=datetime.now(UTC),
    )
    db.add(u)
    db.flush()
    return u


def _prepayment_request(db, requester, amount):
    from app.constants import BuyPlanLineStatus, PaymentMethod
    from app.models import Company, CustomerSite, Quote, Requisition
    from app.models.buy_plan import BuyPlan, BuyPlanLine
    from app.services.prepayment_service import create_prepayment

    company = Company(name="Test Co", is_active=True, created_at=datetime.now(UTC))
    db.add(company)
    db.flush()
    site = CustomerSite(company_id=company.id, site_name="HQ")
    db.add(site)
    db.flush()
    req = Requisition(name="REQ-QC", customer_name="Test Co", status="active", created_by=requester.id)
    db.add(req)
    db.flush()
    quote = Quote(
        requisition_id=req.id,
        customer_site_id=site.id,
        quote_number="Q-QC-1",
        status="sent",
        line_items=[],
        subtotal=Decimal("1000"),
        total_cost=Decimal("800"),
        total_margin_pct=Decimal("20"),
        created_by_id=requester.id,
        created_at=datetime.now(UTC),
    )
    db.add(quote)
    db.flush()
    bp = BuyPlan(quote_id=quote.id, requisition_id=req.id, status="draft", so_status="pending")
    db.add(bp)
    db.flush()
    line = BuyPlanLine(
        buy_plan_id=bp.id,
        status=BuyPlanLineStatus.PENDING_VERIFY.value,
        unit_cost=10.0,
        quantity=10,
        po_number="PO-QC-1",
        po_confirmed_at=datetime.now(UTC),
    )
    db.add(line)
    db.flush()
    _pp, request = create_prepayment(
        db,
        buy_plan_id=bp.id,
        buy_plan_line_id=line.id,
        vendor_card_id=None,
        payment_method=PaymentMethod.WIRE,
        total_incl_fees=amount,
        test_report_sent=False,
        buyer_remarks="x",
        created_by=requester,
    )
    db.commit()
    return request, _pp


def test_reassign_refuses_ineligible_target(db_session, test_user):
    from app.services.approvals.events import reassign

    eligible = _approver(db_session, "e@trioscs.com", "az-e", prepay=True)
    ineligible = _approver(db_session, "i@trioscs.com", "az-i", prepay=False)
    db_session.commit()
    request, _pp = _prepayment_request(db_session, test_user, Decimal("400"))

    with pytest.raises(ValueError, match="not eligible to approve gate"):
        reassign(db_session, request.id, from_user=eligible, to_user=ineligible, actor=test_user)


def test_reassign_refuses_target_over_amount_limit(db_session, test_user):
    from app.services.approvals.events import reassign

    eligible = _approver(db_session, "e2@trioscs.com", "az-e2", prepay=True)
    capped = _approver(db_session, "c@trioscs.com", "az-c", prepay=True, limit=Decimal("100"))
    db_session.commit()
    request, _pp = _prepayment_request(db_session, test_user, Decimal("2500"))

    with pytest.raises(ValueError, match="not eligible to approve gate"):
        reassign(db_session, request.id, from_user=eligible, to_user=capped, actor=test_user)


def test_reassign_allows_eligible_target(db_session, test_user):
    from app.constants import ApprovalRecipientStatus
    from app.services.approvals.events import reassign

    a = _approver(db_session, "a@trioscs.com", "az-a", prepay=True)
    # b is NOT eligible at routing time, so it isn't auto-added as a recipient;
    # it becomes eligible just before the reassign (the real delegation flow).
    b = _approver(db_session, "b@trioscs.com", "az-b", prepay=False)
    db_session.commit()
    request, _pp = _prepayment_request(db_session, test_user, Decimal("400"))

    b.can_approve_prepayments = True
    db_session.flush()
    new_recipient = reassign(db_session, request.id, from_user=a, to_user=b, actor=test_user)
    assert new_recipient.user_id == b.id
    assert new_recipient.status == ApprovalRecipientStatus.PENDING


# ── decide() stamps the prepayment lifecycle for every path ──────────────


def test_decide_approve_stamps_prepayment_and_mints_pay_token(db_session, test_user):
    from app.constants import PrepaymentStatus
    from app.models.quality_plan import Prepayment
    from app.services.approvals.service import decide

    approver = _approver(db_session, "ap@trioscs.com", "az-ap", prepay=True)
    db_session.commit()
    request, pp = _prepayment_request(db_session, test_user, Decimal("400"))

    decide(db_session, request.id, approver, "approve", comment=None)
    db_session.commit()

    fresh = db_session.get(Prepayment, pp.id)
    assert fresh.status == PrepaymentStatus.APPROVED.value  # no longer stuck 'requested'
    assert fresh.pay_token  # OK-TO-WIRE link exists
    assert fresh.approved_by_id == approver.id


def test_decide_reject_voids_prepayment(db_session, test_user):
    from app.constants import PrepaymentStatus
    from app.models.quality_plan import Prepayment
    from app.services.approvals.service import decide

    approver = _approver(db_session, "ap2@trioscs.com", "az-ap2", prepay=True)
    db_session.commit()
    request, pp = _prepayment_request(db_session, test_user, Decimal("400"))

    decide(db_session, request.id, approver, "reject", comment="bad docs")
    db_session.commit()

    fresh = db_session.get(Prepayment, pp.id)
    assert fresh.status == PrepaymentStatus.VOID.value
    assert fresh.pay_token is None  # never wire a rejected prepayment
