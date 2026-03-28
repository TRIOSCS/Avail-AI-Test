"""Comprehensive tests for app/services/knowledge_service.py.

Covers: CRUD operations, Q&A threading, auto-capture (quote/offer/RFQ),
context builders (requisition/MPN/vendor/pipeline/company), AI insight
generation, and cached insight getters.

Called by: pytest
Depends on: conftest.py fixtures, app/models/knowledge.py,
            app/services/knowledge_service.py
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.models.knowledge import KnowledgeEntry
from app.models.offers import Offer
from app.models.sourcing import Requirement, Requisition
from app.services.knowledge_service import (
    build_company_context,
    build_context,
    build_mpn_context,
    build_pipeline_context,
    build_vendor_context,
    capture_offer_fact,
    capture_quote_fact,
    capture_rfq_response_fact,
    create_entry,
    delete_entry,
    generate_company_insights,
    generate_insights,
    generate_mpn_insights,
    generate_pipeline_insights,
    generate_vendor_insights,
    get_cached_company_insights,
    get_cached_insights,
    get_cached_mpn_insights,
    get_cached_pipeline_insights,
    get_cached_vendor_insights,
    get_entries,
    get_entry,
    post_answer,
    post_question,
    update_entry,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_requisition(db, *, name="REQ-1", status="active", customer_name="Acme"):
    req = Requisition(name=name, status=status, customer_name=customer_name)
    db.add(req)
    db.flush()
    return req


def _make_requirement(db, req, *, mpn="LM317T", target_qty=100):
    r = Requirement(requisition_id=req.id, primary_mpn=mpn, target_qty=target_qty)
    db.add(r)
    db.flush()
    return r


def _make_entry(db, user_id, **kwargs):
    defaults = {"entry_type": "fact", "content": "test fact", "source": "manual"}
    defaults.update(kwargs)
    return create_entry(db, user_id=user_id, **defaults)


# ═══════════════════════════════════════════════════════════════════════════
# CRUD: create_entry
# ═══════════════════════════════════════════════════════════════════════════


class TestCreateEntry:
    def test_basic_create(self, db_session, test_user):
        entry = create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="Test fact",
            source="manual",
        )
        assert entry.id is not None
        assert entry.entry_type == "fact"
        assert entry.content == "Test fact"
        assert entry.source == "manual"
        assert entry.created_by == test_user.id

    def test_create_with_all_links(self, db_session, test_user, test_vendor_card, test_company):
        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        entry = create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="note",
            content="Linked note",
            source="manual",
            mpn="LM317T",
            vendor_card_id=test_vendor_card.id,
            company_id=test_company.id,
            requisition_id=req.id,
            requirement_id=r.id,
        )
        assert entry.mpn == "LM317T"
        assert entry.vendor_card_id == test_vendor_card.id
        assert entry.company_id == test_company.id
        assert entry.requisition_id == req.id
        assert entry.requirement_id == r.id

    def test_create_with_confidence_and_expiry(self, db_session, test_user):
        exp = datetime.now(timezone.utc) + timedelta(days=30)
        entry = create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="ai_insight",
            content="AI insight",
            source="ai_generated",
            confidence=0.9,
            expires_at=exp,
        )
        assert entry.confidence == 0.9
        assert entry.expires_at is not None

    def test_create_with_parent(self, db_session, test_user):
        parent = _make_entry(db_session, test_user.id)
        child = create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="answer",
            content="Answer text",
            source="manual",
            parent_id=parent.id,
        )
        assert child.parent_id == parent.id

    def test_create_with_assigned_to_ids(self, db_session, test_user):
        entry = create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="question",
            content="Who knows?",
            source="manual",
            assigned_to_ids=[1, 2, 3],
        )
        assert entry.assigned_to_ids == [1, 2, 3]

    def test_commit_false_flushes_but_does_not_commit(self, db_session, test_user):
        entry = create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="transient",
            source="manual",
            commit=False,
        )
        assert entry.id is not None
        db_session.rollback()
        assert db_session.get(KnowledgeEntry, entry.id) is None

    def test_commit_true_persists(self, db_session, test_user):
        entry = create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="durable",
            source="manual",
            commit=True,
        )
        assert db_session.get(KnowledgeEntry, entry.id) is not None


# ═══════════════════════════════════════════════════════════════════════════
# CRUD: get_entries
# ═══════════════════════════════════════════════════════════════════════════


class TestGetEntries:
    def test_empty(self, db_session):
        assert get_entries(db_session) == []

    def test_returns_entries(self, db_session, test_user):
        _make_entry(db_session, test_user.id)
        result = get_entries(db_session)
        assert len(result) == 1

    def test_filter_by_requisition_id(self, db_session, test_user):
        req = _make_requisition(db_session)
        _make_entry(db_session, test_user.id, requisition_id=req.id)
        _make_entry(db_session, test_user.id, content="other")
        result = get_entries(db_session, requisition_id=req.id)
        assert len(result) == 1

    def test_filter_by_company_id(self, db_session, test_user, test_company):
        _make_entry(db_session, test_user.id, company_id=test_company.id)
        _make_entry(db_session, test_user.id, content="other")
        result = get_entries(db_session, company_id=test_company.id)
        assert len(result) == 1

    def test_filter_by_vendor_card_id(self, db_session, test_user, test_vendor_card):
        _make_entry(db_session, test_user.id, vendor_card_id=test_vendor_card.id)
        result = get_entries(db_session, vendor_card_id=test_vendor_card.id)
        assert len(result) == 1

    def test_filter_by_mpn(self, db_session, test_user):
        _make_entry(db_session, test_user.id, mpn="ABC123")
        _make_entry(db_session, test_user.id, mpn="XYZ789")
        result = get_entries(db_session, mpn="ABC123")
        assert len(result) == 1

    def test_filter_by_entry_type(self, db_session, test_user):
        _make_entry(db_session, test_user.id, entry_type="fact")
        _make_entry(db_session, test_user.id, entry_type="note", content="note")
        result = get_entries(db_session, entry_type="fact")
        assert len(result) == 1

    def test_exclude_expired(self, db_session, test_user):
        past = datetime.now(timezone.utc) - timedelta(days=1)
        future = datetime.now(timezone.utc) + timedelta(days=30)
        _make_entry(db_session, test_user.id, expires_at=past, content="expired")
        _make_entry(db_session, test_user.id, expires_at=future, content="valid")
        _make_entry(db_session, test_user.id, content="no expiry")
        result = get_entries(db_session, include_expired=False)
        assert len(result) == 2

    def test_include_expired(self, db_session, test_user):
        past = datetime.now(timezone.utc) - timedelta(days=1)
        _make_entry(db_session, test_user.id, expires_at=past, content="expired")
        result = get_entries(db_session, include_expired=True)
        assert len(result) == 1

    def test_excludes_answers_from_top_level(self, db_session, test_user):
        parent = _make_entry(db_session, test_user.id, entry_type="question", content="Q?")
        create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="answer",
            content="A",
            source="manual",
            parent_id=parent.id,
        )
        result = get_entries(db_session)
        # Only parent should appear
        assert len(result) == 1
        assert result[0].entry_type == "question"

    def test_pagination(self, db_session, test_user):
        for i in range(5):
            _make_entry(db_session, test_user.id, content=f"fact {i}")
        result = get_entries(db_session, limit=2, offset=0)
        assert len(result) == 2
        result2 = get_entries(db_session, limit=2, offset=2)
        assert len(result2) == 2

    def test_order_by_created_at_desc(self, db_session, test_user):
        _make_entry(db_session, test_user.id, content="first")
        _make_entry(db_session, test_user.id, content="second")
        result = get_entries(db_session)
        # Most recent first
        assert result[0].content == "second"


# ═══════════════════════════════════════════════════════════════════════════
# CRUD: get_entry
# ═══════════════════════════════════════════════════════════════════════════


class TestGetEntry:
    def test_found(self, db_session, test_user):
        e = _make_entry(db_session, test_user.id)
        result = get_entry(db_session, e.id)
        assert result is not None
        assert result.id == e.id

    def test_not_found(self, db_session):
        assert get_entry(db_session, 99999) is None

    def test_loads_answers(self, db_session, test_user):
        parent = _make_entry(db_session, test_user.id, entry_type="question", content="Q?")
        create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="answer",
            content="A",
            source="manual",
            parent_id=parent.id,
        )
        result = get_entry(db_session, parent.id)
        assert len(result.answers) == 1


# ═══════════════════════════════════════════════════════════════════════════
# CRUD: update_entry
# ═══════════════════════════════════════════════════════════════════════════


class TestUpdateEntry:
    def test_updates_fields(self, db_session, test_user):
        e = _make_entry(db_session, test_user.id, content="original")
        updated = update_entry(db_session, e.id, test_user.id, content="updated")
        assert updated.content == "updated"

    def test_returns_none_for_missing(self, db_session, test_user):
        assert update_entry(db_session, 99999, test_user.id, content="x") is None

    def test_ignores_none_values(self, db_session, test_user):
        e = _make_entry(db_session, test_user.id, content="original")
        updated = update_entry(db_session, e.id, test_user.id, content=None)
        assert updated.content == "original"

    def test_ignores_nonexistent_attributes(self, db_session, test_user):
        e = _make_entry(db_session, test_user.id, content="original")
        updated = update_entry(db_session, e.id, test_user.id, nonexistent_field="val")
        assert updated is not None
        assert updated.content == "original"


# ═══════════════════════════════════════════════════════════════════════════
# CRUD: delete_entry
# ═══════════════════════════════════════════════════════════════════════════


class TestDeleteEntry:
    def test_deletes_existing(self, db_session, test_user):
        e = _make_entry(db_session, test_user.id)
        assert delete_entry(db_session, e.id, test_user.id) is True
        assert db_session.get(KnowledgeEntry, e.id) is None

    def test_returns_false_for_missing(self, db_session, test_user):
        assert delete_entry(db_session, 99999, test_user.id) is False


# ═══════════════════════════════════════════════════════════════════════════
# Q&A: post_question / post_answer
# ═══════════════════════════════════════════════════════════════════════════


class TestQA:
    def test_post_question(self, db_session, test_user):
        q = post_question(
            db_session,
            user_id=test_user.id,
            content="What vendor is best?",
            assigned_to_ids=[test_user.id],
        )
        assert q.entry_type == "question"
        assert q.assigned_to_ids == [test_user.id]

    def test_post_question_with_links(self, db_session, test_user, test_vendor_card, test_company):
        req = _make_requisition(db_session)
        r = _make_requirement(db_session, req)
        q = post_question(
            db_session,
            user_id=test_user.id,
            content="Q?",
            assigned_to_ids=[],
            mpn="LM317T",
            vendor_card_id=test_vendor_card.id,
            company_id=test_company.id,
            requisition_id=req.id,
            requirement_id=r.id,
        )
        assert q.mpn == "LM317T"
        assert q.vendor_card_id == test_vendor_card.id

    def test_post_answer_marks_resolved(self, db_session, test_user):
        q = post_question(
            db_session,
            user_id=test_user.id,
            content="Question?",
            assigned_to_ids=[test_user.id],
        )
        a = post_answer(
            db_session,
            user_id=test_user.id,
            question_id=q.id,
            content="Answer!",
        )
        assert a is not None
        assert a.entry_type == "answer"
        assert a.parent_id == q.id
        db_session.refresh(q)
        assert q.is_resolved is True

    def test_post_answer_inherits_links(self, db_session, test_user):
        req = _make_requisition(db_session)
        q = post_question(
            db_session,
            user_id=test_user.id,
            content="Q?",
            assigned_to_ids=[],
            mpn="ABC",
            requisition_id=req.id,
        )
        a = post_answer(
            db_session,
            user_id=test_user.id,
            question_id=q.id,
            content="A!",
            answered_via="teams",
        )
        assert a.mpn == "ABC"
        assert a.requisition_id == req.id
        assert a.answered_via == "teams"

    def test_post_answer_returns_none_for_nonexistent(self, db_session, test_user):
        assert post_answer(db_session, user_id=test_user.id, question_id=99999, content="A") is None

    def test_post_answer_returns_none_for_non_question(self, db_session, test_user):
        e = _make_entry(db_session, test_user.id, entry_type="fact")
        assert post_answer(db_session, user_id=test_user.id, question_id=e.id, content="A") is None


# ═══════════════════════════════════════════════════════════════════════════
# Auto-capture: capture_quote_fact
# ═══════════════════════════════════════════════════════════════════════════


class TestCaptureQuoteFact:
    def test_captures_quote_fact(self, db_session, test_user):
        req = _make_requisition(db_session)
        quote = SimpleNamespace(
            quote_number="Q-001",
            requisition_id=req.id,
            line_items=[
                {"mpn": "LM317T", "unit_sell": 1.50, "qty": 100, "vendor_name": "Arrow"},
            ],
        )
        entry = capture_quote_fact(db_session, quote=quote, user_id=test_user.id)
        assert entry is not None
        assert "LM317T" in entry.content
        assert "$1.50" in entry.content
        assert "x100" in entry.content
        assert "Arrow" in entry.content
        assert entry.entry_type == "fact"
        assert entry.source == "system"

    def test_multiple_line_items(self, db_session, test_user):
        req = _make_requisition(db_session)
        quote = SimpleNamespace(
            quote_number="Q-002",
            requisition_id=req.id,
            line_items=[
                {"mpn": "MPN-A", "unit_sell": 1.00, "qty": 50},
                {"part_number": "MPN-B", "sell_price": 2.00},
            ],
        )
        entry = capture_quote_fact(db_session, quote=quote, user_id=test_user.id)
        assert entry is not None
        assert "MPN-A" in entry.content
        assert "MPN-B" in entry.content

    def test_empty_line_items_returns_none(self, db_session, test_user):
        quote = SimpleNamespace(quote_number="Q-003", requisition_id=1, line_items=[])
        assert capture_quote_fact(db_session, quote=quote, user_id=test_user.id) is None

    def test_none_line_items_returns_none(self, db_session, test_user):
        quote = SimpleNamespace(quote_number="Q-004", requisition_id=1, line_items=None)
        assert capture_quote_fact(db_session, quote=quote, user_id=test_user.id) is None

    def test_line_items_without_price_skipped(self, db_session, test_user):
        quote = SimpleNamespace(
            quote_number="Q-005",
            requisition_id=1,
            line_items=[{"mpn": "ABC", "qty": 100}],  # no price
        )
        assert capture_quote_fact(db_session, quote=quote, user_id=test_user.id) is None

    def test_exception_returns_none(self, db_session, test_user):
        # Pass object without expected attributes
        result = capture_quote_fact(db_session, quote=object(), user_id=test_user.id)
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# Auto-capture: capture_offer_fact
# ═══════════════════════════════════════════════════════════════════════════


class TestCaptureOfferFact:
    def test_captures_offer(self, db_session, test_user):
        offer = SimpleNamespace(
            mpn="LM317T",
            unit_price=0.50,
            quantity=500,
            vendor_name="Arrow",
            lead_time="2 weeks",
            vendor_card_id=None,
            requisition_id=None,
        )
        entry = capture_offer_fact(db_session, offer=offer, user_id=test_user.id)
        assert entry is not None
        assert "LM317T" in entry.content
        assert "$0.50" in entry.content
        assert "Arrow" in entry.content
        assert "2 weeks" in entry.content

    def test_offer_without_user_id_uses_zero(self, db_session):
        offer = SimpleNamespace(
            mpn="XYZ",
            unit_price=1.00,
            quantity=None,
            vendor_name="Vendor",
            lead_time=None,
            vendor_card_id=None,
            requisition_id=None,
        )
        entry = capture_offer_fact(db_session, offer=offer, user_id=None)
        assert entry is not None

    def test_empty_offer_returns_none(self, db_session):
        offer = SimpleNamespace(
            mpn=None,
            unit_price=None,
            quantity=None,
            vendor_name=None,
            lead_time=None,
        )
        # All empty => no content_parts => None
        assert capture_offer_fact(db_session, offer=offer) is None

    def test_exception_returns_none(self, db_session):
        # Will fail on getattr
        result = capture_offer_fact(db_session, offer="not-an-object")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# Auto-capture: capture_rfq_response_fact
# ═══════════════════════════════════════════════════════════════════════════


class TestCaptureRfqResponseFact:
    def test_captures_parts(self, db_session):
        parsed = {
            "parts": [
                {
                    "mpn": "LM317T",
                    "status": "available",
                    "unit_price": 0.50,
                    "qty_available": 1000,
                    "lead_time_weeks": "4",
                }
            ],
            "confidence": 0.95,
        }
        entries = capture_rfq_response_fact(db_session, parsed=parsed, vendor_name="Arrow")
        assert len(entries) == 1
        assert "Arrow" in entries[0].content
        assert "LM317T" in entries[0].content
        assert entries[0].confidence == 0.95

    def test_multiple_parts(self, db_session):
        parsed = {
            "parts": [
                {"mpn": "A", "status": "available", "unit_price": 1.0, "qty_available": 100},
                {"mpn": "B", "status": "no stock"},
            ],
        }
        entries = capture_rfq_response_fact(db_session, parsed=parsed, vendor_name="Vendor X")
        assert len(entries) == 2

    def test_price_fact_expiry(self, db_session):
        parsed = {"parts": [{"mpn": "A", "unit_price": 1.0}]}
        entries = capture_rfq_response_fact(db_session, parsed=parsed, vendor_name="V")
        assert entries[0].expires_at is not None

    def test_lead_time_fact_expiry(self, db_session):
        parsed = {"parts": [{"mpn": "A", "lead_time": "6 weeks"}]}
        entries = capture_rfq_response_fact(db_session, parsed=parsed, vendor_name="V")
        assert entries[0].expires_at is not None

    def test_empty_parts_returns_empty(self, db_session):
        assert capture_rfq_response_fact(db_session, parsed={"parts": []}, vendor_name="V") == []

    def test_no_parts_key_returns_empty(self, db_session):
        assert capture_rfq_response_fact(db_session, parsed={}, vendor_name="V") == []

    def test_exception_returns_empty(self, db_session):
        # parts not iterable
        result = capture_rfq_response_fact(db_session, parsed={"parts": None}, vendor_name="V")
        assert result == []


# ═══════════════════════════════════════════════════════════════════════════
# Context builders
# ═══════════════════════════════════════════════════════════════════════════


class TestBuildContext:
    def test_empty_for_missing_requisition(self, db_session):
        assert build_context(db_session, requisition_id=99999) == ""

    def test_empty_when_no_entries(self, db_session):
        req = _make_requisition(db_session)
        db_session.commit()
        assert build_context(db_session, requisition_id=req.id) == ""

    def test_includes_direct_knowledge(self, db_session, test_user):
        req = _make_requisition(db_session)
        create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="Direct fact about this req",
            source="manual",
            requisition_id=req.id,
        )
        ctx = build_context(db_session, requisition_id=req.id)
        assert "Direct fact about this req" in ctx
        assert "Direct knowledge" in ctx

    def test_marks_expired_as_outdated(self, db_session, test_user):
        req = _make_requisition(db_session)
        past = datetime.now(timezone.utc) - timedelta(days=1)
        create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="Old fact",
            source="manual",
            requisition_id=req.id,
            expires_at=past,
        )
        ctx = build_context(db_session, requisition_id=req.id)
        assert "[OUTDATED]" in ctx

    def test_excludes_ai_insights(self, db_session, test_user):
        req = _make_requisition(db_session)
        create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="ai_insight",
            content="AI generated",
            source="ai_generated",
            requisition_id=req.id,
        )
        ctx = build_context(db_session, requisition_id=req.id)
        assert ctx == ""

    def test_includes_mpn_knowledge_from_other_reqs(self, db_session, test_user):
        req1 = _make_requisition(db_session, name="REQ-1")
        _make_requirement(db_session, req1, mpn="LM317T")

        req2 = _make_requisition(db_session, name="REQ-2")
        create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="Cross-req MPN fact",
            source="manual",
            mpn="LM317T",
            requisition_id=req2.id,
        )
        db_session.commit()

        ctx = build_context(db_session, requisition_id=req1.id)
        assert "Cross-req MPN fact" in ctx
        assert "Same MPNs" in ctx


class TestBuildMpnContext:
    def test_empty_for_unknown_mpn(self, db_session):
        assert build_mpn_context(db_session, mpn="NONEXISTENT") == ""

    def test_includes_entries(self, db_session, test_user):
        create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="MPN fact",
            source="manual",
            mpn="LM317T",
        )
        ctx = build_mpn_context(db_session, mpn="LM317T")
        assert "MPN fact" in ctx

    def test_includes_offers(self, db_session, test_user):
        req = _make_requisition(db_session)
        offer = Offer(
            requisition_id=req.id,
            vendor_name="Arrow",
            mpn="LM317T",
            unit_price=0.50,
            qty_available=1000,
            status="active",
        )
        db_session.add(offer)
        db_session.commit()
        ctx = build_mpn_context(db_session, mpn="LM317T")
        assert "Arrow" in ctx
        assert "Offer history" in ctx

    def test_includes_requisitions(self, db_session, test_user):
        req = _make_requisition(db_session, name="REQ-MPN")
        _make_requirement(db_session, req, mpn="LM317T")
        db_session.commit()
        ctx = build_mpn_context(db_session, mpn="LM317T")
        assert "REQ-MPN" in ctx


class TestBuildVendorContext:
    def test_empty_for_missing_vendor(self, db_session):
        assert build_vendor_context(db_session, vendor_card_id=99999) == ""

    def test_includes_vendor_header(self, db_session, test_vendor_card):
        ctx = build_vendor_context(db_session, vendor_card_id=test_vendor_card.id)
        assert "Arrow Electronics" in ctx

    def test_includes_knowledge_entries(self, db_session, test_user, test_vendor_card):
        create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="Vendor fact",
            source="manual",
            vendor_card_id=test_vendor_card.id,
        )
        ctx = build_vendor_context(db_session, vendor_card_id=test_vendor_card.id)
        assert "Vendor fact" in ctx

    def test_includes_offers(self, db_session, test_vendor_card):
        req = _make_requisition(db_session)
        offer = Offer(
            requisition_id=req.id,
            vendor_name="Arrow",
            vendor_card_id=test_vendor_card.id,
            mpn="LM317T",
            unit_price=0.50,
            qty_available=100,
            status="active",
        )
        db_session.add(offer)
        db_session.commit()
        ctx = build_vendor_context(db_session, vendor_card_id=test_vendor_card.id)
        assert "Recent offers" in ctx


class TestBuildPipelineContext:
    def test_empty_when_no_requisitions(self, db_session):
        assert build_pipeline_context(db_session) == ""

    def test_includes_status_breakdown(self, db_session):
        _make_requisition(db_session, status="active")
        _make_requisition(db_session, name="REQ-2", status="active")
        db_session.commit()
        ctx = build_pipeline_context(db_session)
        assert "Pipeline status breakdown" in ctx
        assert "active" in ctx

    def test_includes_active_reqs(self, db_session):
        req = _make_requisition(db_session, name="REQ-ACT", status="active")
        db_session.commit()
        ctx = build_pipeline_context(db_session)
        assert "Active requisitions" in ctx
        assert "REQ-ACT" in ctx


class TestBuildCompanyContext:
    def test_empty_for_missing_company(self, db_session):
        assert build_company_context(db_session, company_id=99999) == ""

    def test_includes_company_profile(self, db_session, test_company):
        ctx = build_company_context(db_session, company_id=test_company.id)
        assert "Acme Electronics" in ctx
        assert "Company profile" in ctx

    def test_includes_knowledge_entries(self, db_session, test_user, test_company):
        create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="note",
            content="Company note",
            source="manual",
            company_id=test_company.id,
        )
        ctx = build_company_context(db_session, company_id=test_company.id)
        assert "Company note" in ctx


# ═══════════════════════════════════════════════════════════════════════════
# AI Insight Generation (mocked)
# ═══════════════════════════════════════════════════════════════════════════


class TestGenerateInsights:
    @pytest.mark.asyncio
    async def test_empty_context_returns_empty(self, db_session):
        result = await generate_insights(db_session, requisition_id=99999)
        assert result == []

    @pytest.mark.asyncio
    async def test_generates_insights(self, db_session, test_user):
        req = _make_requisition(db_session)
        create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="Price data for AI",
            source="manual",
            requisition_id=req.id,
        )

        mock_result = {
            "insights": [
                {"content": "Price is trending up", "confidence": 0.9, "based_on_expired": False},
                {"content": "Consider vendor X", "confidence": 0.85, "based_on_expired": False},
            ]
        }

        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock) as mock_ai:
            mock_ai.return_value = mock_result
            entries = await generate_insights(db_session, requisition_id=req.id)
            assert len(entries) == 2
            assert entries[0].content == "Price is trending up"
            assert entries[0].entry_type == "ai_insight"
            assert entries[0].source == "ai_generated"

    @pytest.mark.asyncio
    async def test_deletes_old_insights_before_generating(self, db_session, test_user):
        req = _make_requisition(db_session)
        # Create direct knowledge + old insight
        create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="Context fact",
            source="manual",
            requisition_id=req.id,
        )
        old = create_entry(
            db_session,
            user_id=None,
            entry_type="ai_insight",
            content="Old insight",
            source="ai_generated",
            requisition_id=req.id,
        )
        old_id = old.id

        mock_result = {
            "insights": [
                {"content": "New insight", "confidence": 0.9, "based_on_expired": False},
            ]
        }

        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock) as mock_ai:
            mock_ai.return_value = mock_result
            entries = await generate_insights(db_session, requisition_id=req.id)
            assert len(entries) == 1
            # Verify old insight was deleted (check by content since SQLite may reuse IDs)
            remaining = db_session.get(KnowledgeEntry, old_id)
            assert remaining is None or remaining.content != "Old insight"

    @pytest.mark.asyncio
    async def test_claude_unavailable_returns_empty(self, db_session, test_user):
        from app.utils.claude_errors import ClaudeUnavailableError

        req = _make_requisition(db_session)
        create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="fact",
            source="manual",
            requisition_id=req.id,
        )

        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock) as mock_ai:
            mock_ai.side_effect = ClaudeUnavailableError("Not configured")
            result = await generate_insights(db_session, requisition_id=req.id)
            assert result == []

    @pytest.mark.asyncio
    async def test_claude_error_returns_empty(self, db_session, test_user):
        from app.utils.claude_errors import ClaudeError

        req = _make_requisition(db_session)
        create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="fact",
            source="manual",
            requisition_id=req.id,
        )

        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock) as mock_ai:
            mock_ai.side_effect = ClaudeError("API error")
            result = await generate_insights(db_session, requisition_id=req.id)
            assert result == []

    @pytest.mark.asyncio
    async def test_no_insights_key_returns_empty(self, db_session, test_user):
        req = _make_requisition(db_session)
        create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="fact",
            source="manual",
            requisition_id=req.id,
        )

        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock) as mock_ai:
            mock_ai.return_value = {}
            result = await generate_insights(db_session, requisition_id=req.id)
            assert result == []

    @pytest.mark.asyncio
    async def test_caps_at_five_insights(self, db_session, test_user):
        req = _make_requisition(db_session)
        create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="fact",
            source="manual",
            requisition_id=req.id,
        )

        mock_result = {
            "insights": [{"content": f"Insight {i}", "confidence": 0.8, "based_on_expired": False} for i in range(10)]
        }

        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock) as mock_ai:
            mock_ai.return_value = mock_result
            entries = await generate_insights(db_session, requisition_id=req.id)
            assert len(entries) == 5


class TestGenerateMpnInsights:
    @pytest.mark.asyncio
    async def test_empty_context_returns_empty(self, db_session):
        result = await generate_mpn_insights(db_session, mpn="NONEXISTENT")
        assert result == []

    @pytest.mark.asyncio
    async def test_generates_mpn_insights(self, db_session, test_user):
        create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="MPN data",
            source="manual",
            mpn="LM317T",
        )

        mock_result = {
            "insights": [
                {"content": "MPN insight", "confidence": 0.85, "based_on_expired": False},
            ]
        }

        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock) as mock_ai:
            mock_ai.return_value = mock_result
            entries = await generate_mpn_insights(db_session, mpn="LM317T")
            assert len(entries) == 1
            assert entries[0].mpn == "LM317T"

    @pytest.mark.asyncio
    async def test_claude_unavailable(self, db_session, test_user):
        from app.utils.claude_errors import ClaudeUnavailableError

        create_entry(db_session, user_id=test_user.id, entry_type="fact", content="fact", source="manual", mpn="X")
        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock) as m:
            m.side_effect = ClaudeUnavailableError("err")
            assert await generate_mpn_insights(db_session, mpn="X") == []


class TestGenerateVendorInsights:
    @pytest.mark.asyncio
    async def test_empty_context_returns_empty(self, db_session):
        result = await generate_vendor_insights(db_session, vendor_card_id=99999)
        assert result == []

    @pytest.mark.asyncio
    async def test_generates_vendor_insights(self, db_session, test_user, test_vendor_card):
        create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="Vendor data",
            source="manual",
            vendor_card_id=test_vendor_card.id,
        )
        mock_result = {
            "insights": [
                {"content": "Vendor insight", "confidence": 0.8, "based_on_expired": False},
            ]
        }
        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock) as m:
            m.return_value = mock_result
            entries = await generate_vendor_insights(db_session, vendor_card_id=test_vendor_card.id)
            assert len(entries) == 1
            assert entries[0].vendor_card_id == test_vendor_card.id

    @pytest.mark.asyncio
    async def test_claude_error(self, db_session, test_user, test_vendor_card):
        from app.utils.claude_errors import ClaudeError

        create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="fact",
            source="manual",
            vendor_card_id=test_vendor_card.id,
        )
        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock) as m:
            m.side_effect = ClaudeError("API error")
            assert await generate_vendor_insights(db_session, vendor_card_id=test_vendor_card.id) == []


class TestGeneratePipelineInsights:
    @pytest.mark.asyncio
    async def test_empty_context_returns_empty(self, db_session):
        result = await generate_pipeline_insights(db_session)
        assert result == []

    @pytest.mark.asyncio
    async def test_generates_pipeline_insights(self, db_session):
        _make_requisition(db_session, status="active")
        db_session.commit()
        mock_result = {
            "insights": [
                {"content": "Pipeline insight", "confidence": 0.8, "based_on_expired": False},
            ]
        }
        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock) as m:
            m.return_value = mock_result
            entries = await generate_pipeline_insights(db_session)
            assert len(entries) == 1
            assert entries[0].mpn == "__pipeline__"

    @pytest.mark.asyncio
    async def test_claude_unavailable(self, db_session):
        from app.utils.claude_errors import ClaudeUnavailableError

        _make_requisition(db_session, status="active")
        db_session.commit()
        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock) as m:
            m.side_effect = ClaudeUnavailableError("err")
            assert await generate_pipeline_insights(db_session) == []


class TestGenerateCompanyInsights:
    @pytest.mark.asyncio
    async def test_empty_context_returns_empty(self, db_session):
        result = await generate_company_insights(db_session, company_id=99999)
        assert result == []

    @pytest.mark.asyncio
    async def test_generates_company_insights(self, db_session, test_user, test_company):
        create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="note",
            content="Company note",
            source="manual",
            company_id=test_company.id,
        )
        mock_result = {
            "insights": [
                {"content": "Company insight", "confidence": 0.8, "based_on_expired": False},
            ]
        }
        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock) as m:
            m.return_value = mock_result
            entries = await generate_company_insights(db_session, company_id=test_company.id)
            assert len(entries) == 1
            assert entries[0].company_id == test_company.id

    @pytest.mark.asyncio
    async def test_no_result_returns_empty(self, db_session, test_user, test_company):
        create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="note",
            content="note",
            source="manual",
            company_id=test_company.id,
        )
        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock) as m:
            m.return_value = None
            assert await generate_company_insights(db_session, company_id=test_company.id) == []


# ═══════════════════════════════════════════════════════════════════════════
# Cached insight getters
# ═══════════════════════════════════════════════════════════════════════════


class TestCachedInsightGetters:
    def test_get_cached_insights(self, db_session, test_user):
        req = _make_requisition(db_session)
        create_entry(
            db_session,
            user_id=None,
            entry_type="ai_insight",
            content="Cached insight",
            source="ai_generated",
            requisition_id=req.id,
        )
        result = get_cached_insights(db_session, requisition_id=req.id)
        assert len(result) == 1

    def test_get_cached_insights_empty(self, db_session):
        assert get_cached_insights(db_session, requisition_id=99999) == []

    def test_get_cached_mpn_insights(self, db_session, test_user):
        create_entry(
            db_session,
            user_id=None,
            entry_type="ai_insight",
            content="MPN cached",
            source="ai_generated",
            mpn="LM317T",
        )
        result = get_cached_mpn_insights(db_session, mpn="LM317T")
        assert len(result) == 1

    def test_get_cached_mpn_insights_excludes_req_bound(self, db_session, test_user):
        req = _make_requisition(db_session)
        create_entry(
            db_session,
            user_id=None,
            entry_type="ai_insight",
            content="Req-bound insight",
            source="ai_generated",
            mpn="LM317T",
            requisition_id=req.id,
        )
        result = get_cached_mpn_insights(db_session, mpn="LM317T")
        assert len(result) == 0

    def test_get_cached_vendor_insights(self, db_session, test_vendor_card):
        create_entry(
            db_session,
            user_id=None,
            entry_type="ai_insight",
            content="Vendor cached",
            source="ai_generated",
            vendor_card_id=test_vendor_card.id,
        )
        result = get_cached_vendor_insights(db_session, vendor_card_id=test_vendor_card.id)
        assert len(result) == 1

    def test_get_cached_pipeline_insights(self, db_session):
        create_entry(
            db_session,
            user_id=None,
            entry_type="ai_insight",
            content="Pipeline cached",
            source="ai_generated",
            mpn="__pipeline__",
        )
        result = get_cached_pipeline_insights(db_session)
        assert len(result) == 1

    def test_get_cached_company_insights(self, db_session, test_company):
        create_entry(
            db_session,
            user_id=None,
            entry_type="ai_insight",
            content="Company cached",
            source="ai_generated",
            company_id=test_company.id,
        )
        result = get_cached_company_insights(db_session, company_id=test_company.id)
        assert len(result) == 1
