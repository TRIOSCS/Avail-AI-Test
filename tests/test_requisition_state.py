"""Tests for app/services/requisition_state.py — stored transitions + derived stage.

Covers:
- Allowed stored-status transitions succeed and update status
- Illegal transitions raise ValueError (mid-pipeline values are no longer statuses)
- ActivityLog created on transition
- No-op when status unchanged
- Won/Lost transitions require an outcome reason; reopening clears it
- W3.3 derivation: quoted > offers > rfqs_sent > open from data rows; stored
  non-open statuses outrank data; failed email contacts don't count; combined
  quotes count via QuoteRequisition membership; batch map has no N+1 semantics
  difference from the single-req path; attach_display_status never dirties
  the stored column.
"""

import os

os.environ["TESTING"] = "1"


import pytest

from app.constants import ActivityType, RequisitionStatus
from app.models import ActivityLog, Contact, Offer, Quote, QuoteRequisition, Requisition
from app.services.requisition_state import (
    ALLOWED_TRANSITIONS,
    PIPELINE_STAGES,
    OutcomeReasonRequired,
    attach_display_status,
    derived_status,
    derived_status_map,
    transition,
)


class TestTransition:
    @pytest.mark.parametrize(
        ("from_status", "to_status"),
        [
            ("open", "hotlist"),
            ("draft", "open"),
            ("won", "open"),
        ],
        ids=["open_to_hotlist", "draft_to_open", "won_to_open"],
    )
    def test_allowed_transition(self, db_session, test_requisition, test_user, from_status, to_status):
        test_requisition.status = from_status
        db_session.commit()

        transition(test_requisition, to_status, test_user, db_session)
        assert test_requisition.status == to_status

    def test_allowed_transition_with_enum(self, db_session, test_requisition, test_user):
        test_requisition.status = "open"
        db_session.commit()

        transition(test_requisition, RequisitionStatus.HOTLIST, test_user, db_session)
        assert test_requisition.status == "hotlist"

    def test_illegal_transition_raises(self, db_session, test_requisition, test_user):
        test_requisition.status = "draft"
        db_session.commit()

        with pytest.raises(ValueError, match="Invalid transition"):
            transition(test_requisition, "won", test_user, db_session)

    @pytest.mark.parametrize("dead_value", ["rfqs_sent", "offers", "quoted"])
    def test_mid_pipeline_values_are_not_statuses(self, db_session, test_requisition, test_user, dead_value):
        """The derived stages cannot be written through the state machine (W3.3)."""
        test_requisition.status = "open"
        db_session.commit()

        with pytest.raises(ValueError, match="Invalid transition"):
            transition(test_requisition, dead_value, test_user, db_session)

    def test_noop_when_same_status(self, db_session, test_requisition, test_user):
        test_requisition.status = "open"
        db_session.commit()

        transition(test_requisition, "open", test_user, db_session)
        assert test_requisition.status == "open"

    def test_creates_activity_log(self, db_session, test_requisition, test_user):
        test_requisition.status = "open"
        db_session.commit()

        transition(test_requisition, "hotlist", test_user, db_session)
        db_session.flush()

        logs = (
            db_session.query(ActivityLog)
            .filter_by(
                requisition_id=test_requisition.id,
                activity_type=ActivityType.STATUS_CHANGED,
            )
            .all()
        )
        assert len(logs) == 1
        assert "open → hotlist" in logs[0].subject

    def test_all_transitions_defined(self):
        """Every enum value has an entry in ALLOWED_TRANSITIONS — and nothing else."""
        for status in RequisitionStatus:
            assert status.value in ALLOWED_TRANSITIONS
        assert set(ALLOWED_TRANSITIONS) == {s.value for s in RequisitionStatus}

    def test_no_transition_targets_a_derived_stage(self):
        """No stored transition may target a derived pipeline stage."""
        for origin, targets in ALLOWED_TRANSITIONS.items():
            assert not (targets & set(PIPELINE_STAGES)), f"{origin} allows a derived stage"

    def test_none_actor(self, db_session, test_requisition):
        """Transition with actor=None: ActivityLog creation may fail (NOT NULL FK), but
        the status transition still succeeds because the exception is caught."""
        test_requisition.status = "open"
        db_session.commit()

        transition(test_requisition, "hotlist", None, db_session)
        # Transition still succeeds even if the log creation fails
        assert test_requisition.status == "hotlist"

    def test_none_initial_status_defaults_to_open(self, db_session, test_requisition, test_user):
        """When req.status is None, it defaults to 'open' for transition logic."""
        test_requisition.status = None

        transition(test_requisition, "hotlist", test_user, db_session)
        assert test_requisition.status == "hotlist"

    def test_activity_log_exception_suppressed(self, db_session, test_requisition, test_user):
        """Exception during activity log creation is suppressed (logged)."""
        from unittest.mock import patch

        test_requisition.status = "open"
        db_session.commit()

        # Mock db.add to raise on the ActivityLog
        original_add = db_session.add

        def flaky_add(obj):
            if isinstance(obj, ActivityLog):
                raise RuntimeError("DB add failed")
            return original_add(obj)

        with patch.object(db_session, "add", side_effect=flaky_add):
            transition(test_requisition, "hotlist", test_user, db_session)

        # Transition succeeded despite log failure
        assert test_requisition.status == "hotlist"


class TestTransitionEdgeCases:
    """Boundary and illegal transition edge cases."""

    def test_won_to_open_roundtrip(self, db_session, test_requisition, test_user):
        test_requisition.status = "won"
        db_session.commit()
        transition(test_requisition, "open", test_user, db_session)
        assert test_requisition.status == "open"
        transition(test_requisition, "hotlist", test_user, db_session)
        assert test_requisition.status == "hotlist"

    def test_rapid_double_transition(self, db_session, test_requisition, test_user):
        test_requisition.status = "open"
        db_session.commit()
        transition(test_requisition, "hotlist", test_user, db_session)
        transition(test_requisition, "open", test_user, db_session)
        assert test_requisition.status == "open"

    def test_every_illegal_transition_from_won(self, db_session, test_user):
        """Won can ONLY go to open.

        All others must fail.
        """
        illegal = {"lost", "hotlist", "draft", "cancelled", "rfqs_sent", "offers", "quoted"}
        for target in illegal:
            req = Requisition(name=f"test-{target}", status="won", created_by=test_user.id)
            db_session.add(req)
            db_session.flush()
            with pytest.raises(ValueError):
                transition(req, target, test_user, db_session)


class TestOutcomeReason:
    """Won/Lost close-reason rules (unchanged by W3.3)."""

    def test_won_requires_reason(self, db_session, test_requisition, test_user):
        test_requisition.status = "open"
        db_session.commit()
        with pytest.raises(OutcomeReasonRequired, match="reason is required"):
            transition(test_requisition, "won", test_user, db_session)

    def test_lost_requires_reason(self, db_session, test_requisition, test_user):
        test_requisition.status = "open"
        db_session.commit()
        with pytest.raises(OutcomeReasonRequired, match="reason is required"):
            transition(test_requisition, "lost", test_user, db_session)

    def test_won_persists_stripped_reason(self, db_session, test_requisition, test_user):
        test_requisition.status = "open"
        db_session.commit()
        transition(test_requisition, "won", test_user, db_session, reason="  Best price  ")
        assert test_requisition.status == "won"
        assert test_requisition.outcome_reason == "Best price"

    def test_non_terminal_transition_clears_outcome_reason(self, db_session, test_requisition, test_user):
        test_requisition.status = "won"
        test_requisition.outcome_reason = "stale"
        db_session.commit()
        transition(test_requisition, "open", test_user, db_session)
        assert test_requisition.outcome_reason is None


def _mk_req(db, user, name, status="open"):
    req = Requisition(name=name, status=status, created_by=user.id)
    db.add(req)
    db.flush()
    return req


def _mk_contact(db, req, user, *, status="sent", contact_type="email"):
    c = Contact(
        requisition_id=req.id,
        user_id=user.id,
        contact_type=contact_type,
        vendor_name="Arrow Electronics",
        status=status,
    )
    db.add(c)
    db.flush()
    return c


def _mk_offer(db, req, user):
    o = Offer(
        requisition_id=req.id,
        vendor_name="Arrow Electronics",
        mpn="NE555P",
        entered_by_id=user.id,
    )
    db.add(o)
    db.flush()
    return o


def _mk_quote_membership(db, req, user):
    q = Quote(
        quote_number=f"Q-TEST-{req.id}",
        requisition_id=req.id,
        created_by_id=user.id,
    )
    db.add(q)
    db.flush()
    # The after_insert listener guarantees the self QuoteRequisition row; make the
    # membership explicit for engines/sessions where the listener already ran.
    existing = db.query(QuoteRequisition).filter_by(quote_id=q.id, requisition_id=req.id).first()
    if not existing:
        db.add(QuoteRequisition(quote_id=q.id, requisition_id=req.id))
        db.flush()
    return q


class TestDerivedStatus:
    """W3.3 — the pipeline stage is derived from data, never stored."""

    def test_open_with_no_data_is_open(self, db_session, test_user):
        req = _mk_req(db_session, test_user, "bare")
        assert derived_status(db_session, req) == "open"

    def test_email_contact_derives_rfqs_sent(self, db_session, test_user):
        req = _mk_req(db_session, test_user, "rfq")
        _mk_contact(db_session, req, test_user)
        assert derived_status(db_session, req) == "rfqs_sent"

    def test_failed_email_contact_does_not_count(self, db_session, test_user):
        req = _mk_req(db_session, test_user, "rfq-failed")
        _mk_contact(db_session, req, test_user, status="failed")
        assert derived_status(db_session, req) == "open"

    def test_phone_contact_does_not_count(self, db_session, test_user):
        req = _mk_req(db_session, test_user, "call-log")
        _mk_contact(db_session, req, test_user, contact_type="phone")
        assert derived_status(db_session, req) == "open"

    def test_offer_outranks_rfq(self, db_session, test_user):
        req = _mk_req(db_session, test_user, "offer")
        _mk_contact(db_session, req, test_user)
        _mk_offer(db_session, req, test_user)
        assert derived_status(db_session, req) == "offers"

    def test_quote_outranks_offer(self, db_session, test_user):
        req = _mk_req(db_session, test_user, "quote")
        _mk_contact(db_session, req, test_user)
        _mk_offer(db_session, req, test_user)
        _mk_quote_membership(db_session, req, test_user)
        assert derived_status(db_session, req) == "quoted"

    def test_combined_quote_membership_counts(self, db_session, test_user):
        """A secondary req on a combined quote derives quoted via QuoteRequisition."""
        primary = _mk_req(db_session, test_user, "combined-primary")
        secondary = _mk_req(db_session, test_user, "combined-secondary")
        q = _mk_quote_membership(db_session, primary, test_user)
        db_session.add(QuoteRequisition(quote_id=q.id, requisition_id=secondary.id))
        db_session.flush()
        assert derived_status(db_session, secondary) == "quoted"

    @pytest.mark.parametrize("stored", ["draft", "hotlist", "won", "lost", "cancelled"])
    def test_stored_intent_outranks_data(self, db_session, test_user, stored):
        """Non-open stored statuses always win, whatever data exists."""
        req = _mk_req(db_session, test_user, f"intent-{stored}", status=stored)
        _mk_contact(db_session, req, test_user)
        _mk_offer(db_session, req, test_user)
        _mk_quote_membership(db_session, req, test_user)
        assert derived_status(db_session, req) == stored

    def test_batch_map_matches_single(self, db_session, test_user):
        bare = _mk_req(db_session, test_user, "b-bare")
        rfq = _mk_req(db_session, test_user, "b-rfq")
        _mk_contact(db_session, rfq, test_user)
        offer = _mk_req(db_session, test_user, "b-offer")
        _mk_offer(db_session, offer, test_user)
        quoted = _mk_req(db_session, test_user, "b-quote")
        _mk_quote_membership(db_session, quoted, test_user)

        result = derived_status_map(db_session, [bare.id, rfq.id, offer.id, quoted.id])
        assert result == {
            bare.id: "open",
            rfq.id: "rfqs_sent",
            offer.id: "offers",
            quoted.id: "quoted",
        }
        for r in (bare, rfq, offer, quoted):
            assert result[r.id] == derived_status(db_session, r)

    def test_batch_map_empty_ids(self, db_session):
        assert derived_status_map(db_session, []) == {}

    def test_attach_display_status_never_dirties_the_column(self, db_session, test_user):
        """display_status is a plain attribute; the ORM column must stay clean."""
        req = _mk_req(db_session, test_user, "attach")
        _mk_offer(db_session, req, test_user)
        db_session.commit()

        attach_display_status(db_session, [req])
        assert req.display_status == "offers"
        assert req.status == "open"
        assert req not in db_session.dirty

        db_session.commit()
        db_session.refresh(req)
        assert req.status == "open"

    def test_attach_display_status_passes_through_stored(self, db_session, test_user):
        won = _mk_req(db_session, test_user, "attach-won", status="won")
        bare = _mk_req(db_session, test_user, "attach-bare")
        attach_display_status(db_session, [won, bare])
        assert won.display_status == "won"
        assert bare.display_status == "open"
