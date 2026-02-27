"""
test_buy_plan_schemas.py — Buy Plan V3 Schema Validation Tests

Covers request validation (required fields, blank rejection, enum constraints)
and response schema structure for buy plan submit, approval, SO/PO verification,
issue flagging, line edits, splits, and offer comparison.

Called by: pytest
Depends on: app.schemas.buy_plan
"""

import pytest
from datetime import datetime, timezone
from pydantic import ValidationError

from app.schemas.buy_plan import (
    BuyPlanLineEdit,
    BuyPlanLineIssue,
    BuyPlanLineOverride,
    BuyPlanV3Approval,
    BuyPlanV3Submit,
    POConfirmation,
    POVerificationRequest,
    SOVerificationRequest,
    VerificationGroupUpdate,
    # Response schemas
    AIFlag,
    BuyPlanLineResponse,
    BuyPlanV3ListItem,
    BuyPlanV3Response,
    OfferComparisonItem,
    OfferComparisonResponse,
    VerificationGroupMemberResponse,
)


# ── BuyPlanV3Submit ──────────────────────────────────────────────────


class TestBuyPlanV3Submit:
    def test_valid_submit(self):
        s = BuyPlanV3Submit(sales_order_number="SO-2026-001")
        assert s.sales_order_number == "SO-2026-001"
        assert s.line_edits is None
        assert s.salesperson_notes is None

    def test_so_number_required(self):
        with pytest.raises(ValidationError) as exc:
            BuyPlanV3Submit(sales_order_number="")
        assert "Sales Order" in str(exc.value)

    def test_so_number_whitespace_only(self):
        with pytest.raises(ValidationError):
            BuyPlanV3Submit(sales_order_number="   ")

    def test_so_number_stripped(self):
        s = BuyPlanV3Submit(sales_order_number="  SO-123  ")
        assert s.sales_order_number == "SO-123"

    def test_with_line_edits(self):
        s = BuyPlanV3Submit(
            sales_order_number="SO-001",
            line_edits=[
                BuyPlanLineEdit(requirement_id=1, offer_id=10, quantity=500),
                BuyPlanLineEdit(requirement_id=1, offer_id=11, quantity=500),
            ],
        )
        assert len(s.line_edits) == 2

    def test_with_customer_po(self):
        s = BuyPlanV3Submit(
            sales_order_number="SO-001",
            customer_po_number="CPO-42",
        )
        assert s.customer_po_number == "CPO-42"


# ── BuyPlanLineEdit ─────────────────────────────────────────────────


class TestBuyPlanLineEdit:
    def test_valid_line_edit(self):
        e = BuyPlanLineEdit(requirement_id=1, offer_id=10, quantity=500)
        assert e.requirement_id == 1
        assert e.offer_id == 10
        assert e.quantity == 500

    def test_quantity_must_be_positive(self):
        with pytest.raises(ValidationError):
            BuyPlanLineEdit(requirement_id=1, offer_id=10, quantity=0)

    def test_quantity_negative_rejected(self):
        with pytest.raises(ValidationError):
            BuyPlanLineEdit(requirement_id=1, offer_id=10, quantity=-5)

    def test_split_lines_same_requirement(self):
        """Multiple edits can share the same requirement_id (split)."""
        edits = [
            BuyPlanLineEdit(requirement_id=1, offer_id=10, quantity=600),
            BuyPlanLineEdit(requirement_id=1, offer_id=11, quantity=400),
        ]
        assert edits[0].requirement_id == edits[1].requirement_id
        assert edits[0].quantity + edits[1].quantity == 1000

    def test_with_sales_note(self):
        e = BuyPlanLineEdit(
            requirement_id=1, offer_id=10, quantity=500,
            sales_note="Prefer this vendor, faster lead time",
        )
        assert e.sales_note is not None


# ── BuyPlanV3Approval ───────────────────────────────────────────────


class TestBuyPlanV3Approval:
    def test_approve(self):
        a = BuyPlanV3Approval(action="approve")
        assert a.action == "approve"

    def test_reject(self):
        a = BuyPlanV3Approval(action="reject", notes="Margin too low")
        assert a.action == "reject"
        assert a.notes == "Margin too low"

    def test_invalid_action(self):
        with pytest.raises(ValidationError):
            BuyPlanV3Approval(action="maybe")

    def test_with_line_overrides(self):
        a = BuyPlanV3Approval(
            action="approve",
            line_overrides=[
                BuyPlanLineOverride(line_id=1, offer_id=20, manager_note="Better vendor"),
            ],
        )
        assert len(a.line_overrides) == 1
        assert a.line_overrides[0].offer_id == 20


# ── SOVerificationRequest ───────────────────────────────────────────


class TestSOVerification:
    def test_approve(self):
        v = SOVerificationRequest(action="approve")
        assert v.action == "approve"

    def test_reject_requires_note(self):
        with pytest.raises(ValidationError) as exc:
            SOVerificationRequest(action="reject")
        assert "note is required" in str(exc.value)

    def test_reject_with_note(self):
        v = SOVerificationRequest(action="reject", rejection_note="Wrong SO #")
        assert v.rejection_note == "Wrong SO #"

    def test_halt_requires_note(self):
        with pytest.raises(ValidationError):
            SOVerificationRequest(action="halt")

    def test_halt_with_note(self):
        v = SOVerificationRequest(action="halt", rejection_note="Fraud suspected")
        assert v.action == "halt"

    def test_reject_blank_note_rejected(self):
        with pytest.raises(ValidationError):
            SOVerificationRequest(action="reject", rejection_note="   ")

    def test_invalid_action(self):
        with pytest.raises(ValidationError):
            SOVerificationRequest(action="pause")


# ── POConfirmation ───────────────────────────────────────────────────


class TestPOConfirmation:
    def test_valid(self):
        c = POConfirmation(
            po_number="PO-2026-0042",
            estimated_ship_date=datetime(2026, 3, 15, tzinfo=timezone.utc),
        )
        assert c.po_number == "PO-2026-0042"
        assert c.estimated_ship_date.year == 2026

    def test_po_number_required(self):
        with pytest.raises(ValidationError):
            POConfirmation(
                po_number="",
                estimated_ship_date=datetime(2026, 3, 15, tzinfo=timezone.utc),
            )

    def test_po_number_whitespace_rejected(self):
        with pytest.raises(ValidationError):
            POConfirmation(
                po_number="   ",
                estimated_ship_date=datetime(2026, 3, 15, tzinfo=timezone.utc),
            )

    def test_ship_date_required(self):
        with pytest.raises(ValidationError):
            POConfirmation(po_number="PO-001")


# ── POVerificationRequest ───────────────────────────────────────────


class TestPOVerification:
    def test_approve(self):
        v = POVerificationRequest(action="approve")
        assert v.action == "approve"

    def test_reject_requires_note(self):
        with pytest.raises(ValidationError) as exc:
            POVerificationRequest(action="reject")
        assert "note is required" in str(exc.value)

    def test_reject_with_note(self):
        v = POVerificationRequest(action="reject", rejection_note="Wrong amount")
        assert v.rejection_note == "Wrong amount"


# ── BuyPlanLineIssue ────────────────────────────────────────────────


class TestBuyPlanLineIssue:
    def test_sold_out(self):
        i = BuyPlanLineIssue(issue_type="sold_out")
        assert i.issue_type == "sold_out"

    def test_price_changed(self):
        i = BuyPlanLineIssue(issue_type="price_changed", note="Up 20%")
        assert i.note == "Up 20%"

    def test_other_requires_note(self):
        with pytest.raises(ValidationError) as exc:
            BuyPlanLineIssue(issue_type="other")
        assert "note is required" in str(exc.value)

    def test_other_with_note(self):
        i = BuyPlanLineIssue(issue_type="other", note="Vendor unresponsive")
        assert i.note == "Vendor unresponsive"

    def test_invalid_issue_type(self):
        with pytest.raises(ValidationError):
            BuyPlanLineIssue(issue_type="unknown_problem")


# ── VerificationGroupUpdate ─────────────────────────────────────────


class TestVerificationGroupUpdate:
    def test_add(self):
        u = VerificationGroupUpdate(user_id=1, action="add")
        assert u.action == "add"

    def test_remove(self):
        u = VerificationGroupUpdate(user_id=1, action="remove")
        assert u.action == "remove"

    def test_invalid_action(self):
        with pytest.raises(ValidationError):
            VerificationGroupUpdate(user_id=1, action="toggle")


# ── Response Schemas ─────────────────────────────────────────────────


class TestResponseSchemas:
    def test_line_response(self):
        lr = BuyPlanLineResponse(
            id=1, buy_plan_id=1, quantity=500,
            status="awaiting_po", mpn="LM317T", vendor_name="Arrow",
        )
        assert lr.mpn == "LM317T"
        assert lr.status == "awaiting_po"

    def test_plan_response_with_lines(self):
        pr = BuyPlanV3Response(
            id=1, quote_id=1, requisition_id=1,
            status="pending", so_status="pending",
            lines=[
                BuyPlanLineResponse(id=1, buy_plan_id=1, quantity=600),
                BuyPlanLineResponse(id=2, buy_plan_id=1, quantity=400),
            ],
            line_count=2, vendor_count=2,
        )
        assert len(pr.lines) == 2
        assert pr.line_count == 2

    def test_plan_response_allows_extra_fields(self):
        """extra='allow' prevents breaking frontends when new fields added."""
        pr = BuyPlanV3Response(
            id=1, quote_id=1, requisition_id=1,
            future_field="should not error",
        )
        assert pr.id == 1

    def test_list_item(self):
        li = BuyPlanV3ListItem(
            id=1, quote_id=1, requisition_id=1,
            status="pending", ai_flag_count=2,
        )
        assert li.ai_flag_count == 2

    def test_ai_flag(self):
        f = AIFlag(type="stale_offer", severity="warning", message="Offer >5 days old")
        assert f.type == "stale_offer"

    def test_offer_comparison(self):
        oc = OfferComparisonResponse(
            requirement_id=1, mpn="LM317T", target_qty=1000,
            selected_offer_ids=[10],
            offers=[
                OfferComparisonItem(
                    offer_id=10, vendor_name="Arrow", unit_price=0.50,
                    ai_score=85.0, is_selected=True,
                ),
                OfferComparisonItem(
                    offer_id=11, vendor_name="Digi-Key", unit_price=0.55,
                    ai_score=72.0, is_selected=False,
                ),
            ],
        )
        assert len(oc.offers) == 2
        assert oc.offers[0].is_selected is True

    def test_verification_group_member(self):
        m = VerificationGroupMemberResponse(
            id=1, user_id=5, user_name="Ops User", is_active=True,
        )
        assert m.user_name == "Ops User"
