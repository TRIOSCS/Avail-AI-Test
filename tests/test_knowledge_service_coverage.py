"""Tests for app/services/knowledge_service.py — comprehensive coverage.

Covers CRUD, Q&A, auto-capture, context building, and AI insight generation.

Called by: pytest
Depends on: conftest fixtures, app.services.knowledge_service
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import Company, Requisition, User
from app.services import knowledge_service


@pytest.fixture()
def requisition(db_session: Session, test_user: User) -> Requisition:
    req = Requisition(
        name="KNOW-TEST-REQ",
        customer_name="Test Co",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)
    return req


class TestCreateEntry:
    def test_basic_creation(self, db_session: Session, test_user: User):
        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="note",
            content="Test note content",
        )
        assert entry.id is not None
        assert entry.content == "Test note content"
        assert entry.entry_type == "note"
        assert entry.source == "manual"

    def test_creation_no_commit(self, db_session: Session, test_user: User):
        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="Fact",
            commit=False,
        )
        assert entry.id is not None

    def test_creation_with_all_fields(
        self, db_session: Session, test_user: User, requisition: Requisition
    ):
        expiry = datetime.now(timezone.utc) + timedelta(days=90)
        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="Price fact: $1.50",
            source="system",
            confidence=0.95,
            expires_at=expiry,
            mpn="LM317T",
            requisition_id=requisition.id,
            assigned_to_ids=[test_user.id],
        )
        assert entry.confidence == 0.95
        assert entry.mpn == "LM317T"
        assert entry.assigned_to_ids == [test_user.id]


class TestGetEntries:
    def test_get_all_entries(self, db_session: Session, test_user: User, requisition: Requisition):
        knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="note", content="N1", requisition_id=requisition.id
        )
        knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="note", content="N2", requisition_id=requisition.id
        )
        entries = knowledge_service.get_entries(db_session, requisition_id=requisition.id)
        assert len(entries) == 2

    def test_filter_by_entry_type(self, db_session: Session, test_user: User, requisition: Requisition):
        knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="note", content="N", requisition_id=requisition.id
        )
        knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="fact", content="F", requisition_id=requisition.id
        )
        notes = knowledge_service.get_entries(db_session, entry_type="note", requisition_id=requisition.id)
        assert len(notes) == 1
        assert notes[0].entry_type == "note"

    def test_filter_by_mpn(self, db_session: Session, test_user: User):
        knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="fact", content="F1", mpn="LM317T"
        )
        knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="fact", content="F2", mpn="STM32"
        )
        entries = knowledge_service.get_entries(db_session, mpn="LM317T")
        assert len(entries) == 1

    def test_exclude_expired(self, db_session: Session, test_user: User, requisition: Requisition):
        past = datetime.now(timezone.utc) - timedelta(days=1)
        future = datetime.now(timezone.utc) + timedelta(days=10)
        knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="fact", content="Expired",
            expires_at=past, requisition_id=requisition.id
        )
        knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="fact", content="Active",
            expires_at=future, requisition_id=requisition.id
        )
        active = knowledge_service.get_entries(db_session, include_expired=False, requisition_id=requisition.id)
        assert len(active) == 1
        assert active[0].content == "Active"

    def test_exclude_answers_from_listing(
        self, db_session: Session, test_user: User, requisition: Requisition
    ):
        q = knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="question",
            content="What's the lead time?", requisition_id=requisition.id
        )
        knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="answer",
            content="2 weeks", parent_id=q.id, requisition_id=requisition.id
        )
        # get_entries excludes entries with parent_id set
        entries = knowledge_service.get_entries(db_session, requisition_id=requisition.id)
        assert all(e.parent_id is None for e in entries)

    def test_filter_by_vendor_card_id(self, db_session: Session, test_user: User, test_vendor_card):
        knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="note", content="Vendor note",
            vendor_card_id=test_vendor_card.id
        )
        entries = knowledge_service.get_entries(db_session, vendor_card_id=test_vendor_card.id)
        assert len(entries) == 1

    def test_filter_by_company_id(self, db_session: Session, test_user: User, test_company: Company):
        knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="note", content="Co note",
            company_id=test_company.id
        )
        entries = knowledge_service.get_entries(db_session, company_id=test_company.id)
        assert len(entries) == 1

    def test_pagination(self, db_session: Session, test_user: User, requisition: Requisition):
        for i in range(5):
            knowledge_service.create_entry(
                db_session, user_id=test_user.id, entry_type="note", content=f"N{i}",
                requisition_id=requisition.id
            )
        page1 = knowledge_service.get_entries(db_session, requisition_id=requisition.id, limit=2, offset=0)
        page2 = knowledge_service.get_entries(db_session, requisition_id=requisition.id, limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
        assert page1[0].id != page2[0].id


class TestGetEntry:
    def test_get_existing(self, db_session: Session, test_user: User):
        entry = knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="note", content="Test"
        )
        fetched = knowledge_service.get_entry(db_session, entry.id)
        assert fetched is not None
        assert fetched.id == entry.id

    def test_get_nonexistent(self, db_session: Session):
        result = knowledge_service.get_entry(db_session, 99999)
        assert result is None


class TestUpdateEntry:
    def test_update_content(self, db_session: Session, test_user: User):
        entry = knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="note", content="Old"
        )
        updated = knowledge_service.update_entry(db_session, entry.id, test_user.id, content="New content")
        assert updated.content == "New content"

    def test_update_nonexistent_returns_none(self, db_session: Session, test_user: User):
        result = knowledge_service.update_entry(db_session, 99999, test_user.id, content="X")
        assert result is None


class TestDeleteEntry:
    def test_delete_existing(self, db_session: Session, test_user: User):
        entry = knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="note", content="Delete me"
        )
        result = knowledge_service.delete_entry(db_session, entry.id, test_user.id)
        assert result is True

    def test_delete_nonexistent(self, db_session: Session, test_user: User):
        result = knowledge_service.delete_entry(db_session, 99999, test_user.id)
        assert result is False


class TestPostQuestion:
    def test_post_question_creates_entry(self, db_session: Session, test_user: User, requisition: Requisition):
        q = knowledge_service.post_question(
            db_session,
            user_id=test_user.id,
            content="What is the lead time for LM317T?",
            assigned_to_ids=[test_user.id],
            requisition_id=requisition.id,
            mpn="LM317T",
        )
        assert q.entry_type == "question"
        assert q.assigned_to_ids == [test_user.id]
        assert q.mpn == "LM317T"


class TestPostAnswer:
    def test_post_answer_resolves_question(self, db_session: Session, test_user: User):
        q = knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="question", content="Q?"
        )
        answer = knowledge_service.post_answer(
            db_session, user_id=test_user.id, question_id=q.id, content="Answer here"
        )
        assert answer is not None
        assert answer.entry_type == "answer"
        assert answer.parent_id == q.id
        # Question should be resolved
        db_session.refresh(q)
        assert q.is_resolved is True

    def test_post_answer_to_non_question_returns_none(self, db_session: Session, test_user: User):
        fact = knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="fact", content="Fact"
        )
        result = knowledge_service.post_answer(
            db_session, user_id=test_user.id, question_id=fact.id, content="Not applicable"
        )
        assert result is None

    def test_post_answer_to_nonexistent_question(self, db_session: Session, test_user: User):
        result = knowledge_service.post_answer(
            db_session, user_id=test_user.id, question_id=99999, content="X"
        )
        assert result is None

    def test_post_answer_custom_via(self, db_session: Session, test_user: User):
        q = knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="question", content="Q?"
        )
        answer = knowledge_service.post_answer(
            db_session, user_id=test_user.id, question_id=q.id, content="Answer", answered_via="email"
        )
        assert answer.answered_via == "email"


class TestCaptureQuoteFact:
    def test_captures_line_items(self, db_session: Session, test_user: User, requisition: Requisition):
        mock_quote = MagicMock()
        mock_quote.quote_number = "Q-001"
        mock_quote.requisition_id = requisition.id
        mock_quote.line_items = [
            {"mpn": "LM317T", "unit_sell": 1.50, "qty": 100, "vendor_name": "Arrow"}
        ]
        entry = knowledge_service.capture_quote_fact(db_session, quote=mock_quote, user_id=test_user.id)
        assert entry is not None
        assert "LM317T" in entry.content
        assert "Q-001" in entry.content

    def test_returns_none_for_empty_line_items(self, db_session: Session, test_user: User):
        mock_quote = MagicMock()
        mock_quote.line_items = []
        result = knowledge_service.capture_quote_fact(db_session, quote=mock_quote, user_id=test_user.id)
        assert result is None

    def test_returns_none_for_no_price(self, db_session: Session, test_user: User):
        mock_quote = MagicMock()
        mock_quote.line_items = [{"mpn": "LM317T"}]  # no price
        result = knowledge_service.capture_quote_fact(db_session, quote=mock_quote, user_id=test_user.id)
        assert result is None

    def test_handles_exception_gracefully(self, db_session: Session, test_user: User):
        mock_quote = MagicMock()
        mock_quote.quote_number = "Q-001"
        mock_quote.line_items = MagicMock(side_effect=RuntimeError("DB error"))
        result = knowledge_service.capture_quote_fact(db_session, quote=mock_quote, user_id=test_user.id)
        assert result is None


class TestCaptureOfferFact:
    def test_captures_offer(self, db_session: Session, test_user: User, requisition: Requisition):
        mock_offer = MagicMock()
        mock_offer.mpn = "LM317T"
        mock_offer.unit_price = 0.75
        mock_offer.quantity = 500
        mock_offer.vendor_name = "Arrow"
        mock_offer.lead_time = "2 weeks"
        mock_offer.vendor_card_id = None
        mock_offer.requisition_id = requisition.id
        entry = knowledge_service.capture_offer_fact(db_session, offer=mock_offer, user_id=test_user.id)
        assert entry is not None
        assert "LM317T" in entry.content
        assert "Arrow" in entry.content

    def test_returns_none_for_empty_offer(self, db_session: Session):
        mock_offer = MagicMock()
        mock_offer.mpn = None
        mock_offer.unit_price = None
        mock_offer.quantity = None
        mock_offer.vendor_name = None
        mock_offer.lead_time = None
        mock_offer.vendor_card_id = None
        mock_offer.requisition_id = None
        result = knowledge_service.capture_offer_fact(db_session, offer=mock_offer)
        assert result is None


class TestCaptureRfqResponseFact:
    def test_captures_parts_with_price(
        self, db_session: Session, requisition: Requisition
    ):
        parsed = {
            "confidence": 0.9,
            "parts": [
                {"mpn": "LM317T", "status": "in stock", "unit_price": 0.50, "qty_available": 1000}
            ],
        }
        entries = knowledge_service.capture_rfq_response_fact(
            db_session, parsed=parsed, vendor_name="Arrow", requisition_id=requisition.id
        )
        assert len(entries) == 1
        assert "LM317T" in entries[0].content

    def test_captures_lead_time_only(
        self, db_session: Session, requisition: Requisition
    ):
        parsed = {
            "parts": [{"mpn": "STM32", "lead_time": "4 weeks"}],
        }
        entries = knowledge_service.capture_rfq_response_fact(
            db_session, parsed=parsed, vendor_name="Digi-Key", requisition_id=requisition.id
        )
        assert len(entries) == 1

    def test_handles_empty_parts(self, db_session: Session):
        entries = knowledge_service.capture_rfq_response_fact(
            db_session, parsed={"parts": []}, vendor_name="Arrow"
        )
        assert entries == []


class TestIsExpired:
    def test_not_expired_future(self):
        future = datetime.now(timezone.utc) + timedelta(days=30)
        now = datetime.now(timezone.utc)
        assert knowledge_service._is_expired(future, now) is False

    def test_expired_past(self):
        past = datetime.now(timezone.utc) - timedelta(days=1)
        now = datetime.now(timezone.utc)
        assert knowledge_service._is_expired(past, now) is True

    def test_no_expiry_returns_false(self):
        now = datetime.now(timezone.utc)
        assert knowledge_service._is_expired(None, now) is False

    def test_naive_datetime_handled(self):
        past_naive = datetime.utcnow() - timedelta(hours=1)
        now = datetime.now(timezone.utc)
        assert knowledge_service._is_expired(past_naive, now) is True


class TestGetCachedInsights:
    def test_returns_ai_insights_only(self, db_session: Session, test_user: User, requisition: Requisition):
        knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="note", content="N", requisition_id=requisition.id
        )
        knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="ai_insight", content="AI insight here",
            requisition_id=requisition.id
        )
        insights = knowledge_service.get_cached_insights(db_session, requisition.id)
        assert len(insights) == 1
        assert insights[0].entry_type == "ai_insight"

    def test_returns_empty_for_no_insights(self, db_session: Session, requisition: Requisition):
        insights = knowledge_service.get_cached_insights(db_session, requisition.id)
        assert insights == []


class TestBuildContext:
    def test_returns_empty_for_missing_req(self, db_session: Session):
        ctx = knowledge_service.build_context(db_session, requisition_id=99999)
        assert ctx == ""

    def test_returns_context_with_direct_entries(
        self, db_session: Session, test_user: User, requisition: Requisition
    ):
        entry = knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="fact",
            content="LM317T $0.50 from Arrow",
            requisition_id=requisition.id,
        )
        # Patch created_at directly on the object (SQLite stores naive, service uses strftime)
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()
        ctx = knowledge_service.build_context(db_session, requisition_id=requisition.id)
        assert "LM317T" in ctx
        assert "Direct knowledge" in ctx

    def test_empty_when_no_entries(self, db_session: Session, requisition: Requisition):
        ctx = knowledge_service.build_context(db_session, requisition_id=requisition.id)
        assert ctx == ""

    def test_expired_entries_marked_outdated(
        self, db_session: Session, test_user: User, requisition: Requisition
    ):
        past_expiry = datetime.now(timezone.utc) - timedelta(days=1)
        entry = knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="fact",
            content="Old price fact",
            expires_at=past_expiry,
            requisition_id=requisition.id,
        )
        # Make created_at compatible
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()
        ctx = knowledge_service.build_context(db_session, requisition_id=requisition.id)
        assert "[OUTDATED]" in ctx


class TestGenerateInsights:
    async def test_generate_insights_no_context(self, db_session: Session, requisition: Requisition):
        # No knowledge entries → no context → returns []
        result = await knowledge_service.generate_insights(db_session, requisition.id)
        assert result == []

    async def test_generate_insights_success(
        self, db_session: Session, test_user: User, requisition: Requisition
    ):
        entry = knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="fact",
            content="LM317T at $0.50",
            requisition_id=requisition.id,
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()

        mock_result = {
            "insights": [
                {"content": "LM317T price is competitive", "confidence": 0.9, "based_on_expired": False}
            ]
        }

        async def _mock_claude(*a, **kw):
            return mock_result

        with patch("app.utils.claude_client.claude_structured", new=_mock_claude):
            result = await knowledge_service.generate_insights(db_session, requisition.id)
        assert len(result) == 1
        assert result[0].entry_type == "ai_insight"

    async def test_generate_insights_claude_unavailable(
        self, db_session: Session, test_user: User, requisition: Requisition
    ):
        from app.utils.claude_errors import ClaudeUnavailableError

        entry = knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="fact",
            content="LM317T at $0.50",
            requisition_id=requisition.id,
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()

        async def _raise(*a, **kw):
            raise ClaudeUnavailableError("Not configured")

        with patch("app.utils.claude_client.claude_structured", new=_raise):
            result = await knowledge_service.generate_insights(db_session, requisition.id)
        assert result == []

    async def test_generate_insights_empty_result(
        self, db_session: Session, test_user: User, requisition: Requisition
    ):
        entry = knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="fact",
            content="LM317T at $0.50",
            requisition_id=requisition.id,
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()

        async def _mock_none(*a, **kw):
            return None

        with patch("app.utils.claude_client.claude_structured", new=_mock_none):
            result = await knowledge_service.generate_insights(db_session, requisition.id)
        assert result == []

    async def test_generate_insights_replaces_old(
        self, db_session: Session, test_user: User, requisition: Requisition
    ):
        # Seed an old ai_insight
        knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="ai_insight",
            content="Old insight",
            requisition_id=requisition.id,
        )
        # Add a real entry to build context
        entry = knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="fact",
            content="LM317T data",
            requisition_id=requisition.id,
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()

        mock_result = {
            "insights": [
                {"content": "New insight", "confidence": 0.85, "based_on_expired": False}
            ]
        }

        async def _mock_claude(*a, **kw):
            return mock_result

        with patch("app.utils.claude_client.claude_structured", new=_mock_claude):
            result = await knowledge_service.generate_insights(db_session, requisition.id)

        all_insights = knowledge_service.get_cached_insights(db_session, requisition.id)
        # Old one deleted, new one added
        assert len(all_insights) == 1
        assert all_insights[0].content == "New insight"


class TestBuildMpnContext:
    def test_returns_empty_for_unknown_mpn(self, db_session: Session):
        ctx = knowledge_service.build_mpn_context(db_session, mpn="UNKNOWN_XYZ")
        assert ctx == ""

    def test_returns_context_with_entries(self, db_session: Session, test_user: User):
        entry = knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="fact",
            content="LM317T $0.50 from Arrow", mpn="LM317T",
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()
        ctx = knowledge_service.build_mpn_context(db_session, mpn="LM317T")
        assert "LM317T" in ctx

    def test_excludes_ai_insights(self, db_session: Session, test_user: User):
        insight = knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="ai_insight",
            content="AI insight about LM317T", mpn="LM317T",
        )
        insight.created_at = datetime.now(timezone.utc)
        db_session.commit()
        ctx = knowledge_service.build_mpn_context(db_session, mpn="LM317T")
        # ai_insight entries are excluded from context
        assert ctx == "" or "ai_insight" not in ctx


class TestBuildVendorContext:
    def test_returns_empty_for_unknown_vendor(self, db_session: Session):
        ctx = knowledge_service.build_vendor_context(db_session, vendor_card_id=99999)
        assert ctx == ""

    def test_returns_context_with_entries(self, db_session: Session, test_user: User, test_vendor_card):
        entry = knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="note",
            content="Arrow reliable supplier", vendor_card_id=test_vendor_card.id,
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()
        ctx = knowledge_service.build_vendor_context(db_session, vendor_card_id=test_vendor_card.id)
        assert "Arrow reliable supplier" in ctx


class TestBuildPipelineContext:
    def test_returns_empty_with_no_requisitions(self, db_session: Session):
        ctx = knowledge_service.build_pipeline_context(db_session)
        assert ctx == ""

    def test_returns_context_with_active_requisitions(
        self, db_session: Session, test_user: User
    ):
        req = Requisition(
            name="PIPELINE-TEST-REQ",
            customer_name="Test Co",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()
        ctx = knowledge_service.build_pipeline_context(db_session)
        assert "active" in ctx.lower()


class TestBuildCompanyContext:
    def test_returns_empty_for_unknown_company(self, db_session: Session):
        ctx = knowledge_service.build_company_context(db_session, company_id=99999)
        assert ctx == ""

    def test_returns_context_with_entries(
        self, db_session: Session, test_user: User, test_company: Company
    ):
        entry = knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="note",
            content="Acme is a strategic customer", company_id=test_company.id,
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()
        ctx = knowledge_service.build_company_context(db_session, company_id=test_company.id)
        assert "Acme" in ctx or "strategic" in ctx


class TestGetCachedEntityInsights:
    def test_get_cached_mpn_insights_empty(self, db_session: Session):
        insights = knowledge_service.get_cached_mpn_insights(db_session, "LM317T")
        assert insights == []

    def test_get_cached_mpn_insights_returns_mpn_insights(self, db_session: Session, test_user: User):
        insight = knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="ai_insight",
            content="MPN insight", mpn="LM317T",
        )
        insights = knowledge_service.get_cached_mpn_insights(db_session, "LM317T")
        assert len(insights) >= 1

    def test_get_cached_vendor_insights_empty(self, db_session: Session, test_vendor_card):
        insights = knowledge_service.get_cached_vendor_insights(db_session, test_vendor_card.id)
        assert insights == []

    def test_get_cached_vendor_insights_returns_vendor_insights(
        self, db_session: Session, test_user: User, test_vendor_card
    ):
        knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="ai_insight",
            content="Vendor insight", vendor_card_id=test_vendor_card.id,
        )
        insights = knowledge_service.get_cached_vendor_insights(db_session, test_vendor_card.id)
        assert len(insights) >= 1

    def test_get_cached_pipeline_insights_empty(self, db_session: Session):
        insights = knowledge_service.get_cached_pipeline_insights(db_session)
        assert insights == []

    def test_get_cached_company_insights_empty(self, db_session: Session, test_company: Company):
        insights = knowledge_service.get_cached_company_insights(db_session, test_company.id)
        assert insights == []

    def test_get_cached_company_insights_returns_company_insights(
        self, db_session: Session, test_user: User, test_company: Company
    ):
        knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="ai_insight",
            content="Company insight", company_id=test_company.id,
        )
        insights = knowledge_service.get_cached_company_insights(db_session, test_company.id)
        assert len(insights) >= 1


class TestGenerateMpnInsights:
    async def test_empty_context_returns_empty(self, db_session: Session):
        result = await knowledge_service.generate_mpn_insights(db_session, "UNKNOWN_PART")
        assert result == []

    async def test_success(self, db_session: Session, test_user: User):
        entry = knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="fact",
            content="LM317T $0.50", mpn="LM317T",
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()

        mock_result = {"insights": [{"content": "MPN insight", "confidence": 0.8, "based_on_expired": False}]}

        async def _mock(*a, **kw):
            return mock_result

        with patch("app.utils.claude_client.claude_structured", new=_mock):
            result = await knowledge_service.generate_mpn_insights(db_session, "LM317T")
        assert len(result) == 1


class TestGenerateVendorInsights:
    async def test_empty_context_returns_empty(self, db_session: Session, test_vendor_card):
        result = await knowledge_service.generate_vendor_insights(db_session, 99999)
        assert result == []

    async def test_success(self, db_session: Session, test_user: User, test_vendor_card):
        entry = knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="note",
            content="Arrow supplier note", vendor_card_id=test_vendor_card.id,
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()

        mock_result = {"insights": [{"content": "Vendor insight", "confidence": 0.9, "based_on_expired": False}]}

        async def _mock(*a, **kw):
            return mock_result

        with patch("app.utils.claude_client.claude_structured", new=_mock):
            result = await knowledge_service.generate_vendor_insights(db_session, test_vendor_card.id)
        assert len(result) == 1


class TestGenerateCompanyInsights:
    async def test_empty_context_returns_empty(self, db_session: Session):
        result = await knowledge_service.generate_company_insights(db_session, 99999)
        assert result == []

    async def test_success(self, db_session: Session, test_user: User, test_company: Company):
        entry = knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="note",
            content="Acme is a strategic customer", company_id=test_company.id,
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()

        mock_result = {"insights": [{"content": "Company insight", "confidence": 0.7, "based_on_expired": False}]}

        async def _mock(*a, **kw):
            return mock_result

        with patch("app.utils.claude_client.claude_structured", new=_mock):
            result = await knowledge_service.generate_company_insights(db_session, test_company.id)
        assert len(result) == 1


class TestGeneratePipelineInsights:
    async def test_empty_pipeline_returns_empty(self, db_session: Session):
        result = await knowledge_service.generate_pipeline_insights(db_session)
        assert result == []

    async def test_success(self, db_session: Session, test_user: User):
        req = Requisition(
            name="PIPE-INSIGHT-REQ",
            customer_name="Test Co",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()

        mock_result = {"insights": [{"content": "Pipeline insight", "confidence": 0.8, "based_on_expired": False}]}

        async def _mock(*a, **kw):
            return mock_result

        with patch("app.utils.claude_client.claude_structured", new=_mock):
            result = await knowledge_service.generate_pipeline_insights(db_session)
        assert len(result) == 1
