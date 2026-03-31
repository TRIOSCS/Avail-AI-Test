"""test_coverage_status_machine.py — Comprehensive tests for app/services/status_machine.py.

Called by: pytest
Depends on: app.services.status_machine, app.constants
"""

import os

os.environ["TESTING"] = "1"

import pytest
from fastapi import HTTPException

from app.constants import (
    BuyPlanStatus,
    OfferStatus,
    QuoteStatus,
    RequisitionStatus,
    SourcingStatus,
)
from app.services.status_machine import (
    BUY_PLAN_TRANSITIONS,
    OFFER_TRANSITIONS,
    QUOTE_TRANSITIONS,
    SOURCING_TRANSITIONS,
    require_valid_transition,
    validate_transition,
)

# ── validate_transition ──────────────────────────────────────────────


class TestValidateTransitionOffer:
    def test_pending_review_to_active(self):
        assert validate_transition("offer", OfferStatus.PENDING_REVIEW, OfferStatus.ACTIVE) is True

    def test_pending_review_to_approved(self):
        assert validate_transition("offer", OfferStatus.PENDING_REVIEW, OfferStatus.APPROVED) is True

    def test_pending_review_to_rejected(self):
        assert validate_transition("offer", OfferStatus.PENDING_REVIEW, OfferStatus.REJECTED) is True

    def test_pending_review_to_sold(self):
        assert validate_transition("offer", OfferStatus.PENDING_REVIEW, OfferStatus.SOLD) is True

    def test_active_to_sold(self):
        assert validate_transition("offer", OfferStatus.ACTIVE, OfferStatus.SOLD) is True

    def test_active_to_won(self):
        assert validate_transition("offer", OfferStatus.ACTIVE, OfferStatus.WON) is True

    def test_active_to_expired(self):
        assert validate_transition("offer", OfferStatus.ACTIVE, OfferStatus.EXPIRED) is True

    def test_approved_to_won(self):
        assert validate_transition("offer", OfferStatus.APPROVED, OfferStatus.WON) is True

    def test_won_to_sold(self):
        assert validate_transition("offer", OfferStatus.WON, OfferStatus.SOLD) is True

    def test_expired_to_active(self):
        assert validate_transition("offer", OfferStatus.EXPIRED, OfferStatus.ACTIVE) is True

    def test_rejected_terminal_raises(self):
        with pytest.raises(ValueError, match="terminal"):
            validate_transition("offer", OfferStatus.REJECTED, OfferStatus.ACTIVE)

    def test_sold_terminal_raises(self):
        with pytest.raises(ValueError, match="terminal"):
            validate_transition("offer", OfferStatus.SOLD, OfferStatus.ACTIVE)

    def test_invalid_transition_no_raise(self):
        result = validate_transition("offer", OfferStatus.REJECTED, OfferStatus.ACTIVE, raise_on_invalid=False)
        assert result is False

    def test_same_status_always_valid(self):
        assert validate_transition("offer", OfferStatus.ACTIVE, OfferStatus.ACTIVE) is True

    def test_same_status_rejected_valid(self):
        assert validate_transition("offer", OfferStatus.REJECTED, OfferStatus.REJECTED) is True


class TestValidateTransitionQuote:
    def test_draft_to_sent(self):
        assert validate_transition("quote", QuoteStatus.DRAFT, QuoteStatus.SENT) is True

    def test_draft_to_won(self):
        assert validate_transition("quote", QuoteStatus.DRAFT, QuoteStatus.WON) is True

    def test_draft_to_lost(self):
        assert validate_transition("quote", QuoteStatus.DRAFT, QuoteStatus.LOST) is True

    def test_sent_to_revised(self):
        assert validate_transition("quote", QuoteStatus.SENT, QuoteStatus.REVISED) is True

    def test_sent_to_won(self):
        assert validate_transition("quote", QuoteStatus.SENT, QuoteStatus.WON) is True

    def test_won_to_draft(self):
        assert validate_transition("quote", QuoteStatus.WON, QuoteStatus.DRAFT) is True

    def test_lost_to_sent(self):
        assert validate_transition("quote", QuoteStatus.LOST, QuoteStatus.SENT) is True

    def test_revised_to_sent(self):
        assert validate_transition("quote", QuoteStatus.REVISED, QuoteStatus.SENT) is True


class TestValidateTransitionBuyPlan:
    def test_draft_to_pending(self):
        assert validate_transition("buy_plan", BuyPlanStatus.DRAFT, BuyPlanStatus.PENDING) is True

    def test_draft_to_cancelled(self):
        assert validate_transition("buy_plan", BuyPlanStatus.DRAFT, BuyPlanStatus.CANCELLED) is True

    def test_pending_to_active(self):
        assert validate_transition("buy_plan", BuyPlanStatus.PENDING, BuyPlanStatus.ACTIVE) is True

    def test_pending_to_cancelled(self):
        assert validate_transition("buy_plan", BuyPlanStatus.PENDING, BuyPlanStatus.CANCELLED) is True

    def test_pending_back_to_draft(self):
        assert validate_transition("buy_plan", BuyPlanStatus.PENDING, BuyPlanStatus.DRAFT) is True

    def test_active_to_completed(self):
        assert validate_transition("buy_plan", BuyPlanStatus.ACTIVE, BuyPlanStatus.COMPLETED) is True

    def test_active_to_halted(self):
        assert validate_transition("buy_plan", BuyPlanStatus.ACTIVE, BuyPlanStatus.HALTED) is True

    def test_halted_to_draft(self):
        assert validate_transition("buy_plan", BuyPlanStatus.HALTED, BuyPlanStatus.DRAFT) is True

    def test_completed_terminal_raises(self):
        with pytest.raises(ValueError):
            validate_transition("buy_plan", BuyPlanStatus.COMPLETED, BuyPlanStatus.DRAFT)

    def test_cancelled_to_draft_valid(self):
        assert validate_transition("buy_plan", BuyPlanStatus.CANCELLED, BuyPlanStatus.DRAFT) is True

    def test_cancelled_to_active_invalid(self):
        with pytest.raises(ValueError):
            validate_transition("buy_plan", BuyPlanStatus.CANCELLED, BuyPlanStatus.ACTIVE)


class TestValidateTransitionRequisition:
    def test_draft_to_active(self):
        assert validate_transition("requisition", RequisitionStatus.DRAFT, RequisitionStatus.ACTIVE) is True

    def test_draft_to_sourcing(self):
        assert validate_transition("requisition", RequisitionStatus.DRAFT, RequisitionStatus.SOURCING) is True

    def test_draft_to_cancelled(self):
        assert validate_transition("requisition", RequisitionStatus.DRAFT, RequisitionStatus.CANCELLED) is True

    def test_active_to_quoting(self):
        assert validate_transition("requisition", RequisitionStatus.ACTIVE, RequisitionStatus.QUOTING) is True

    def test_active_to_won(self):
        assert validate_transition("requisition", RequisitionStatus.ACTIVE, RequisitionStatus.WON) is True

    def test_active_to_lost(self):
        assert validate_transition("requisition", RequisitionStatus.ACTIVE, RequisitionStatus.LOST) is True

    def test_sourcing_to_offers(self):
        assert validate_transition("requisition", RequisitionStatus.SOURCING, RequisitionStatus.OFFERS) is True

    def test_offers_to_quoting(self):
        assert validate_transition("requisition", RequisitionStatus.OFFERS, RequisitionStatus.QUOTING) is True

    def test_quoting_to_won(self):
        assert validate_transition("requisition", RequisitionStatus.QUOTING, RequisitionStatus.WON) is True

    def test_won_to_archived(self):
        assert validate_transition("requisition", RequisitionStatus.WON, RequisitionStatus.ARCHIVED) is True

    def test_archived_to_active(self):
        assert validate_transition("requisition", RequisitionStatus.ARCHIVED, RequisitionStatus.ACTIVE) is True

    def test_cancelled_to_active(self):
        assert validate_transition("requisition", RequisitionStatus.CANCELLED, RequisitionStatus.ACTIVE) is True


class TestValidateTransitionRequirement:
    def test_open_to_sourcing(self):
        assert validate_transition("requirement", SourcingStatus.OPEN, SourcingStatus.SOURCING) is True

    def test_open_to_archived(self):
        assert validate_transition("requirement", SourcingStatus.OPEN, SourcingStatus.ARCHIVED) is True

    def test_sourcing_to_offered(self):
        assert validate_transition("requirement", SourcingStatus.SOURCING, SourcingStatus.OFFERED) is True

    def test_sourcing_back_to_open(self):
        assert validate_transition("requirement", SourcingStatus.SOURCING, SourcingStatus.OPEN) is True

    def test_offered_to_quoted(self):
        assert validate_transition("requirement", SourcingStatus.OFFERED, SourcingStatus.QUOTED) is True

    def test_quoted_to_won(self):
        assert validate_transition("requirement", SourcingStatus.QUOTED, SourcingStatus.WON) is True

    def test_quoted_to_lost(self):
        assert validate_transition("requirement", SourcingStatus.QUOTED, SourcingStatus.LOST) is True

    def test_won_to_archived(self):
        assert validate_transition("requirement", SourcingStatus.WON, SourcingStatus.ARCHIVED) is True

    def test_archived_terminal_raises(self):
        with pytest.raises(ValueError):
            validate_transition("requirement", SourcingStatus.ARCHIVED, SourcingStatus.OPEN)

    def test_lost_to_open(self):
        assert validate_transition("requirement", SourcingStatus.LOST, SourcingStatus.OPEN) is True


class TestValidateTransitionEdgeCases:
    def test_unknown_entity_type_returns_true(self):
        result = validate_transition("unknown_entity", "some_status", "any_status")
        assert result is True

    def test_none_current_status_allows_any(self):
        result = validate_transition("offer", None, OfferStatus.ACTIVE)
        assert result is True

    def test_unknown_current_status_allows_any(self):
        result = validate_transition("offer", "nonexistent_status", OfferStatus.ACTIVE)
        assert result is True

    def test_raise_on_invalid_false_returns_false(self):
        result = validate_transition("offer", OfferStatus.REJECTED, OfferStatus.ACTIVE, raise_on_invalid=False)
        assert result is False

    def test_raise_on_invalid_true_raises(self):
        with pytest.raises(ValueError):
            validate_transition("offer", OfferStatus.REJECTED, OfferStatus.ACTIVE, raise_on_invalid=True)


# ── require_valid_transition ──────────────────────────────────────────


class TestRequireValidTransition:
    def test_valid_transition_no_error(self):
        # Should not raise
        require_valid_transition("offer", OfferStatus.PENDING_REVIEW, OfferStatus.ACTIVE)

    def test_invalid_transition_raises_http_409(self):
        with pytest.raises(HTTPException) as exc_info:
            require_valid_transition("offer", OfferStatus.REJECTED, OfferStatus.ACTIVE)
        assert exc_info.value.status_code == 409

    def test_buy_plan_invalid_raises_409(self):
        with pytest.raises(HTTPException) as exc_info:
            require_valid_transition("buy_plan", BuyPlanStatus.COMPLETED, BuyPlanStatus.ACTIVE)
        assert exc_info.value.status_code == 409

    def test_requisition_valid_no_error(self):
        require_valid_transition("requisition", RequisitionStatus.ACTIVE, RequisitionStatus.QUOTING)

    def test_requirement_terminal_raises_409(self):
        with pytest.raises(HTTPException) as exc_info:
            require_valid_transition("requirement", SourcingStatus.ARCHIVED, SourcingStatus.OPEN)
        assert exc_info.value.status_code == 409


# ── Transition map completeness ───────────────────────────────────────


class TestTransitionMapCompleteness:
    def test_all_offer_statuses_in_transitions(self):
        for status in [
            OfferStatus.PENDING_REVIEW,
            OfferStatus.ACTIVE,
            OfferStatus.APPROVED,
            OfferStatus.WON,
            OfferStatus.REJECTED,
            OfferStatus.SOLD,
            OfferStatus.EXPIRED,
        ]:
            assert status in OFFER_TRANSITIONS

    def test_all_quote_statuses_in_transitions(self):
        for status in [
            QuoteStatus.DRAFT,
            QuoteStatus.SENT,
            QuoteStatus.REVISED,
            QuoteStatus.WON,
            QuoteStatus.LOST,
        ]:
            assert status in QUOTE_TRANSITIONS

    def test_all_buy_plan_statuses_in_transitions(self):
        for status in [
            BuyPlanStatus.DRAFT,
            BuyPlanStatus.PENDING,
            BuyPlanStatus.ACTIVE,
            BuyPlanStatus.HALTED,
            BuyPlanStatus.COMPLETED,
            BuyPlanStatus.CANCELLED,
        ]:
            assert status in BUY_PLAN_TRANSITIONS

    def test_terminal_states_have_empty_transitions(self):
        assert OFFER_TRANSITIONS[OfferStatus.REJECTED] == set()
        assert OFFER_TRANSITIONS[OfferStatus.SOLD] == set()
        assert BUY_PLAN_TRANSITIONS[BuyPlanStatus.COMPLETED] == set()
        assert SOURCING_TRANSITIONS[SourcingStatus.ARCHIVED] == set()

    def test_error_message_contains_entity_type(self):
        try:
            validate_transition("offer", OfferStatus.REJECTED, OfferStatus.ACTIVE)
        except ValueError as e:
            assert "offer" in str(e).lower()

    def test_error_message_contains_statuses(self):
        try:
            validate_transition("offer", OfferStatus.REJECTED, OfferStatus.ACTIVE)
        except ValueError as e:
            assert OfferStatus.REJECTED in str(e)
