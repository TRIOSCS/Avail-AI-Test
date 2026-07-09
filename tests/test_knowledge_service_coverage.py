"""Tests for app/services/knowledge_service.py — comprehensive coverage.

Covers CRUD, Q&A, auto-capture, context building, and AI insight generation.

Called by: pytest
Depends on: conftest fixtures, app.services.knowledge_service
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import Company, Requisition, User
from app.services import knowledge_service


@pytest.fixture()
def requisition(db_session: Session, test_user: User) -> Requisition:
    req = Requisition(
        name="KNOW-TEST-REQ",
        customer_name="Test Co",
        status="open",
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
        from app.models import KnowledgeEntry

        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="Fact",
            commit=False,
        )
        assert entry.id is not None  # flushed (id assigned) ...
        db_session.rollback()
        # ... but NOT committed: rollback discards it, proving commit=False held.
        assert db_session.get(KnowledgeEntry, entry.id) is None

    def test_creation_with_all_fields(self, db_session: Session, test_user: User, requisition: Requisition):
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


class TestCaptureQuoteFact:
    def test_captures_line_items(self, db_session: Session, test_user: User, requisition: Requisition):
        mock_quote = MagicMock()
        mock_quote.quote_number = "Q-001"
        mock_quote.requisition_id = requisition.id
        mock_quote.line_items = [{"mpn": "LM317T", "unit_sell": 1.50, "qty": 100, "vendor_name": "Arrow"}]
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

    def test_quote_fact_survives_caller_rollback(self, db_session: Session, test_user: User, requisition: Requisition):
        """Regression: create_quote/build_quote return WITHOUT committing after this call,
        so the captured fact must be durably committed here — it must survive the caller
        rolling back its transaction (pre-fix it was only flushed and vanished)."""
        from app.models.knowledge import KnowledgeEntry

        mock_quote = MagicMock()
        mock_quote.quote_number = "Q-DURABLE-1"
        mock_quote.requisition_id = requisition.id
        mock_quote.line_items = [{"mpn": "LM317T", "unit_sell": 1.50, "qty": 100, "vendor_name": "Arrow"}]
        entry = knowledge_service.capture_quote_fact(db_session, quote=mock_quote, user_id=test_user.id)
        assert entry is not None

        db_session.rollback()  # caller discards its own transaction without committing

        surviving = db_session.query(KnowledgeEntry).filter(KnowledgeEntry.content.like("%Q-DURABLE-1%")).all()
        assert len(surviving) == 1


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

    def test_offer_fact_survives_caller_rollback(self, db_session: Session, test_user: User, requisition: Requisition):
        """Regression: offer callers don't reliably commit after this call, so the fact
        must be durably committed here — it must survive a caller rollback."""
        from types import SimpleNamespace

        from app.models.knowledge import KnowledgeEntry

        offer = SimpleNamespace(
            mpn="LM317T-DURABLE",
            unit_price=0.75,
            quantity=500,
            vendor_name="Arrow",
            lead_time=None,
            vendor_card_id=None,
            requisition_id=requisition.id,
        )
        entry = knowledge_service.capture_offer_fact(db_session, offer=offer, user_id=test_user.id)
        assert entry is not None

        db_session.rollback()

        surviving = db_session.query(KnowledgeEntry).filter(KnowledgeEntry.mpn == "LM317T-DURABLE").all()
        assert len(surviving) == 1


class TestIsExpired:
    @pytest.mark.parametrize(
        ("make_expires_at", "expected"),
        [
            pytest.param(lambda: datetime.now(timezone.utc) + timedelta(days=30), False, id="not_expired_future"),
            pytest.param(lambda: datetime.now(timezone.utc) - timedelta(days=1), True, id="expired_past"),
            pytest.param(lambda: None, False, id="no_expiry_returns_false"),
            pytest.param(lambda: datetime.utcnow() - timedelta(hours=1), True, id="naive_datetime_handled"),
        ],
    )
    def test_is_expired(self, make_expires_at, expected):
        now = datetime.now(timezone.utc)
        assert knowledge_service._is_expired(make_expires_at(), now) is expected


class TestGetCachedInsights:
    def test_returns_ai_insights_only(self, db_session: Session, test_user: User, requisition: Requisition):
        knowledge_service.create_entry(
            db_session, user_id=test_user.id, entry_type="note", content="N", requisition_id=requisition.id
        )
        knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="ai_insight",
            content="AI insight here",
            requisition_id=requisition.id,
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

    def test_returns_context_with_direct_entries(self, db_session: Session, test_user: User, requisition: Requisition):
        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
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

    def test_expired_entries_marked_outdated(self, db_session: Session, test_user: User, requisition: Requisition):
        past_expiry = datetime.now(timezone.utc) - timedelta(days=1)
        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
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

    async def test_generate_insights_success(self, db_session: Session, test_user: User, requisition: Requisition):
        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="LM317T at $0.50",
            requisition_id=requisition.id,
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()

        mock_result = {
            "insights": [{"content": "LM317T price is competitive", "confidence": 0.9, "based_on_expired": False}]
        }

        async def _mock_claude(*a, **kw):
            return mock_result

        with patch("app.utils.claude_client.claude_structured", new=_mock_claude):
            result = await knowledge_service.generate_insights(db_session, requisition.id)
        assert len(result) == 1
        assert result[0].entry_type == "ai_insight"

    async def test_generate_insights_interactive_tightens_claude_call(
        self, db_session: Session, test_user: User, requisition: Requisition
    ):
        """P2.8: interactive=True must forward timeout=25 / max_attempts=1 to
        claude_structured so the HTMX request can't block for the full default 30s x
        3-retry worst case."""
        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="LM317T at $0.50",
            requisition_id=requisition.id,
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()

        mock_claude = AsyncMock(return_value={"insights": [{"content": "x", "confidence": 0.9}]})
        with patch("app.utils.claude_client.claude_structured", new=mock_claude):
            await knowledge_service.generate_insights(db_session, requisition.id, interactive=True)

        mock_claude.assert_awaited_once()
        kwargs = mock_claude.await_args.kwargs
        assert kwargs["timeout"] == 25
        assert kwargs["max_attempts"] == 1

    async def test_generate_insights_non_interactive_default_unchanged(
        self, db_session: Session, test_user: User, requisition: Requisition
    ):
        """The background job (knowledge_jobs) calls with the default interactive=False
        — claude_structured must NOT receive timeout/max_attempts overrides (preserves
        the original 30s/3-attempt behavior)."""
        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="LM317T at $0.50",
            requisition_id=requisition.id,
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()

        mock_claude = AsyncMock(return_value={"insights": [{"content": "x", "confidence": 0.9}]})
        with patch("app.utils.claude_client.claude_structured", new=mock_claude):
            await knowledge_service.generate_insights(db_session, requisition.id)

        mock_claude.assert_awaited_once()
        kwargs = mock_claude.await_args.kwargs
        assert "timeout" not in kwargs
        assert "max_attempts" not in kwargs

    async def test_generate_insights_claude_unavailable(
        self, db_session: Session, test_user: User, requisition: Requisition
    ):
        from app.utils.claude_errors import ClaudeUnavailableError

        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
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

    async def test_generate_insights_empty_result(self, db_session: Session, test_user: User, requisition: Requisition):
        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
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

    async def test_generate_insights_replaces_old(self, db_session: Session, test_user: User, requisition: Requisition):
        # Seed an old ai_insight
        knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="ai_insight",
            content="Old insight",
            requisition_id=requisition.id,
        )
        # Add a real entry to build context
        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="LM317T data",
            requisition_id=requisition.id,
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()

        mock_result = {"insights": [{"content": "New insight", "confidence": 0.85, "based_on_expired": False}]}

        async def _mock_claude(*a, **kw):
            return mock_result

        with patch("app.utils.claude_client.claude_structured", new=_mock_claude):
            result = await knowledge_service.generate_insights(db_session, requisition.id)

        all_insights = knowledge_service.get_cached_insights(db_session, requisition.id)
        # Old one deleted, new one added
        assert len(all_insights) == 1
        assert all_insights[0].content == "New insight"

    async def test_generate_insights_failure_preserves_cached(
        self, db_session: Session, test_user: User, requisition: Requisition
    ):
        # Regression: a failed AI regen must NOT wipe the previously-cached insights.
        from app.utils.claude_errors import ClaudeError

        knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="ai_insight",
            content="Cached insight worth keeping",
            requisition_id=requisition.id,
        )
        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="LM317T data",
            requisition_id=requisition.id,
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()

        async def _raise(*a, **kw):
            raise ClaudeError("AI regen blew up")

        with patch("app.utils.claude_client.claude_structured", new=_raise):
            result = await knowledge_service.generate_insights(db_session, requisition.id)
        assert result == []

        surviving = knowledge_service.get_cached_insights(db_session, requisition.id)
        assert len(surviving) == 1
        assert surviving[0].content == "Cached insight worth keeping"

    async def test_generate_insights_empty_result_preserves_cached(
        self, db_session: Session, test_user: User, requisition: Requisition
    ):
        # Regression: an empty AI result must NOT wipe the previously-cached insights.
        knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="ai_insight",
            content="Cached insight worth keeping",
            requisition_id=requisition.id,
        )
        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="LM317T data",
            requisition_id=requisition.id,
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()

        async def _mock_empty(*a, **kw):
            return {"insights": []}

        with patch("app.utils.claude_client.claude_structured", new=_mock_empty):
            result = await knowledge_service.generate_insights(db_session, requisition.id)
        assert result == []

        surviving = knowledge_service.get_cached_insights(db_session, requisition.id)
        assert len(surviving) == 1
        assert surviving[0].content == "Cached insight worth keeping"


class TestBuildMpnContext:
    def test_returns_empty_for_unknown_mpn(self, db_session: Session):
        ctx = knowledge_service.build_mpn_context(db_session, mpn="UNKNOWN_XYZ")
        assert ctx == ""

    def test_returns_context_with_entries(self, db_session: Session, test_user: User):
        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="LM317T $0.50 from Arrow",
            mpn="LM317T",
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()
        ctx = knowledge_service.build_mpn_context(db_session, mpn="LM317T")
        assert "LM317T" in ctx

    def test_excludes_ai_insights(self, db_session: Session, test_user: User):
        insight = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="ai_insight",
            content="AI insight about LM317T",
            mpn="LM317T",
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
            db_session,
            user_id=test_user.id,
            entry_type="note",
            content="Arrow reliable supplier",
            vendor_card_id=test_vendor_card.id,
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()
        ctx = knowledge_service.build_vendor_context(db_session, vendor_card_id=test_vendor_card.id)
        assert "Arrow reliable supplier" in ctx


class TestBuildPipelineContext:
    def test_returns_empty_with_no_requisitions(self, db_session: Session):
        ctx = knowledge_service.build_pipeline_context(db_session)
        assert ctx == ""

    def test_returns_context_with_active_requisitions(self, db_session: Session, test_user: User):
        req = Requisition(
            name="PIPELINE-TEST-REQ",
            customer_name="Test Co",
            status="open",
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

    def test_returns_context_with_entries(self, db_session: Session, test_user: User, test_company: Company):
        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="note",
            content="Acme is a strategic customer",
            company_id=test_company.id,
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()
        ctx = knowledge_service.build_company_context(db_session, company_id=test_company.id)
        assert "Acme" in ctx or "strategic" in ctx


class TestGetCachedEntityInsights:
    def test_get_cached_vendor_insights_empty(self, db_session: Session, test_vendor_card):
        insights = knowledge_service.get_cached_vendor_insights(db_session, test_vendor_card.id)
        assert insights == []

    def test_get_cached_vendor_insights_returns_vendor_insights(
        self, db_session: Session, test_user: User, test_vendor_card
    ):
        knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="ai_insight",
            content="Vendor insight",
            vendor_card_id=test_vendor_card.id,
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
            db_session,
            user_id=test_user.id,
            entry_type="ai_insight",
            content="Company insight",
            company_id=test_company.id,
        )
        insights = knowledge_service.get_cached_company_insights(db_session, test_company.id)
        assert len(insights) >= 1


class TestGenerateMpnInsights:
    async def test_empty_context_returns_empty(self, db_session: Session):
        result = await knowledge_service.generate_mpn_insights(db_session, "UNKNOWN_PART")
        assert result == []

    async def test_success(self, db_session: Session, test_user: User):
        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="LM317T $0.50",
            mpn="LM317T",
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
            db_session,
            user_id=test_user.id,
            entry_type="note",
            content="Arrow supplier note",
            vendor_card_id=test_vendor_card.id,
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
            db_session,
            user_id=test_user.id,
            entry_type="note",
            content="Acme is a strategic customer",
            company_id=test_company.id,
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
            status="open",
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


class TestKnowledgeEntryAuthzAndSavepoint:
    """Update/delete enforce creator-only; capture_quote_fact is savepoint-isolated."""

    def test_capture_quote_fact_failure_is_savepoint_isolated(self, db_session, test_user):
        """A create failure inside capture_quote_fact rolls back only its savepoint and
        does NOT poison the caller's transaction (the just-created quote survives)."""
        from types import SimpleNamespace
        from unittest.mock import patch

        from sqlalchemy import text

        from app.services import knowledge_service

        quote = SimpleNamespace(
            line_items=[{"mpn": "LM317T", "unit_sell": 1.5, "qty": 10, "vendor_name": "Arrow"}],
            quote_number="Q-KS-1",
            requisition_id=None,
        )
        with patch.object(knowledge_service, "create_entry", side_effect=RuntimeError("boom")):
            result = knowledge_service.capture_quote_fact(db_session, quote=quote, user_id=test_user.id)
        assert result is None
        # The outer transaction is intact (not aborted) — a follow-up query works.
        assert db_session.execute(text("SELECT 1")).scalar() == 1
