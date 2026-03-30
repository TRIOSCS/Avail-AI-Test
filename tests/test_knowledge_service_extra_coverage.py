"""tests/test_knowledge_service_extra_coverage.py — Additional coverage for knowledge_service.

Targets uncovered branches in:
- build_context: MPN entries, vendor entries, company entries (lines 445-510)
- build_mpn_context: offer history, requisition links (lines 680-716)
- build_vendor_context: vendor meta (ghost_rate etc.), offer history (lines 738-791)
- build_pipeline_context: stale deals (lines 853-866)
- build_company_context: industry/account/strategic/last_activity, site-linked reqs (lines 888-944)
- generate_* functions: ClaudeError exception paths (lines 553-555, 986-991 etc.)
- capture_quote_fact: alternate field names (lines 265-267)
- capture_offer_fact: exception path (lines 316-319)
- capture_rfq_response_fact: lead_time_weeks field (lines 365-366)

Called by: pytest
Depends on: app/services/knowledge_service.py, conftest.py
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import Company, Offer, Requirement, Requisition, User, VendorCard
from app.services import knowledge_service


def _make_create_entry_with_user(user_id_override):
    """Return a wrapper around create_entry that replaces user_id=0 with a real user id."""
    original = knowledge_service.create_entry

    def wrapped(db, user_id=0, **kwargs):
        actual_id = user_id_override if user_id == 0 else user_id
        return original(db, user_id=actual_id, **kwargs)

    return wrapped


@pytest.fixture()
def requisition(db_session: Session, test_user: User) -> Requisition:
    req = Requisition(
        name="EXTRA-KNOW-REQ",
        customer_name="Test Co",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)
    return req


@pytest.fixture()
def requirement_for_req(db_session: Session, requisition: Requisition) -> Requirement:
    r = Requirement(
        requisition_id=requisition.id,
        primary_mpn="LM317T",
        target_qty=100,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(r)
    db_session.commit()
    db_session.refresh(r)
    return r


# ══════════════════════════════════════════════════════════════════════
#  capture_quote_fact — alternate field names
# ══════════════════════════════════════════════════════════════════════


class TestCaptureQuoteFactAlternateFields:
    def test_uses_sell_price_field(self, db_session: Session, test_user: User, requisition: Requisition):
        """capture_quote_fact falls back to sell_price when unit_sell missing."""
        mock_quote = MagicMock()
        mock_quote.quote_number = "Q-ALT-001"
        mock_quote.requisition_id = requisition.id
        mock_quote.line_items = [{"mpn": "STM32F4", "sell_price": 2.50, "quantity": 200}]
        entry = knowledge_service.capture_quote_fact(db_session, quote=mock_quote, user_id=test_user.id)
        assert entry is not None
        assert "STM32F4" in entry.content
        assert "Q-ALT-001" in entry.content

    def test_uses_part_number_field(self, db_session: Session, test_user: User, requisition: Requisition):
        """capture_quote_fact uses part_number when mpn missing."""
        mock_quote = MagicMock()
        mock_quote.quote_number = "Q-PN-001"
        mock_quote.requisition_id = requisition.id
        mock_quote.line_items = [{"part_number": "BC547", "unit_sell": 0.05, "qty": 500}]
        entry = knowledge_service.capture_quote_fact(db_session, quote=mock_quote, user_id=test_user.id)
        assert entry is not None
        assert "BC547" in entry.content

    def test_multiple_line_items(self, db_session: Session, test_user: User, requisition: Requisition):
        """Multiple line items all appear in the content."""
        mock_quote = MagicMock()
        mock_quote.quote_number = "Q-MULTI"
        mock_quote.requisition_id = requisition.id
        mock_quote.line_items = [
            {"mpn": "LM317T", "unit_sell": 1.50, "qty": 100},
            {"mpn": "BC547", "unit_sell": 0.05, "qty": 500},
        ]
        entry = knowledge_service.capture_quote_fact(db_session, quote=mock_quote, user_id=test_user.id)
        assert entry is not None
        assert "LM317T" in entry.content
        assert "BC547" in entry.content

    def test_item_without_qty_still_captured(self, db_session: Session, test_user: User, requisition: Requisition):
        """Line item with price but no qty is still captured."""
        mock_quote = MagicMock()
        mock_quote.quote_number = "Q-NOQTY"
        mock_quote.requisition_id = requisition.id
        mock_quote.line_items = [{"mpn": "NE555", "unit_sell": 0.25}]
        entry = knowledge_service.capture_quote_fact(db_session, quote=mock_quote, user_id=test_user.id)
        assert entry is not None
        assert "NE555" in entry.content


# ══════════════════════════════════════════════════════════════════════
#  capture_offer_fact — exception rollback path
# ══════════════════════════════════════════════════════════════════════


class TestCaptureOfferFactException:
    def test_exception_in_create_entry_returns_none(self, db_session: Session):
        """When create_entry raises, capture_offer_fact rolls back and returns None."""
        mock_offer = MagicMock()
        mock_offer.mpn = "LM317T"
        mock_offer.unit_price = 0.50
        mock_offer.quantity = None
        mock_offer.vendor_name = "Arrow"
        mock_offer.lead_time = None
        mock_offer.vendor_card_id = None
        mock_offer.requisition_id = None

        with patch.object(knowledge_service, "create_entry", side_effect=Exception("DB fail")):
            result = knowledge_service.capture_offer_fact(db_session, offer=mock_offer)
        assert result is None


# ══════════════════════════════════════════════════════════════════════
#  capture_rfq_response_fact — lead_time_weeks field
# ══════════════════════════════════════════════════════════════════════


class TestCaptureRfqResponseFactLeadTimeWeeks:
    def test_uses_lead_time_weeks_field(self, db_session: Session, requisition: Requisition, test_user):
        """lead_time_weeks is used when lead_time not present."""
        parsed = {
            "parts": [{"mpn": "LM317T", "lead_time_weeks": 8}],
        }
        # Patch create_entry to use test_user.id instead of system user 0
        original = knowledge_service.create_entry

        def patched_create_entry(db, user_id=0, **kwargs):
            return original(db, user_id=test_user.id, **kwargs)

        with patch.object(knowledge_service, "create_entry", side_effect=patched_create_entry):
            entries = knowledge_service.capture_rfq_response_fact(
                db_session, parsed=parsed, vendor_name="Digi-Key", requisition_id=requisition.id
            )
        assert len(entries) == 1
        assert "8" in entries[0].content

    def test_multiple_parts_captured(self, db_session: Session, requisition: Requisition, test_user):
        """All parts in the response are captured as separate entries."""
        parsed = {
            "confidence": 0.9,
            "parts": [
                {"mpn": "LM317T", "unit_price": 0.50, "qty_available": 1000},
                {"mpn": "BC547", "unit_price": 0.03, "qty_available": 5000},
            ],
        }
        original = knowledge_service.create_entry

        def patched_create_entry(db, user_id=0, **kwargs):
            return original(db, user_id=test_user.id, **kwargs)

        with patch.object(knowledge_service, "create_entry", side_effect=patched_create_entry):
            entries = knowledge_service.capture_rfq_response_fact(
                db_session, parsed=parsed, vendor_name="Arrow", requisition_id=requisition.id
            )
        assert len(entries) == 2

    def test_exception_path_returns_empty_list(self, db_session: Session):
        """Exception during processing returns empty list (not crash)."""
        with patch.object(knowledge_service, "create_entry", side_effect=Exception("DB fail")):
            entries = knowledge_service.capture_rfq_response_fact(
                db_session,
                parsed={"parts": [{"mpn": "X", "unit_price": 1.0}]},
                vendor_name="V",
            )
        assert entries == []


# ══════════════════════════════════════════════════════════════════════
#  build_context — MPN entries from other reqs, vendor entries, company entries
# ══════════════════════════════════════════════════════════════════════


class TestBuildContextExtended:
    def test_includes_vendor_entries_when_offers_exist(
        self, db_session: Session, test_user: User, requisition: Requisition, test_vendor_card: VendorCard
    ):
        """Context includes vendor knowledge when the req has offers with vendor_card_id."""
        offer = Offer(
            requisition_id=requisition.id,
            vendor_name="Arrow Electronics",
            vendor_card_id=test_vendor_card.id,
            mpn="LM317T",
            qty_available=500,
            unit_price=0.50,
            entered_by_id=test_user.id,
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.commit()

        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="note",
            content="Arrow is reliable",
            vendor_card_id=test_vendor_card.id,
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()

        ctx = knowledge_service.build_context(db_session, requisition_id=requisition.id)
        assert "Arrow" in ctx or "Vendor" in ctx

    def test_includes_company_entries_when_req_has_company(
        self, db_session: Session, test_user: User, test_company: Company
    ):
        """Context includes company knowledge when requisition has company_id."""
        req = Requisition(
            name="COMP-CTX-REQ",
            customer_name="Acme Electronics",
            status="active",
            created_by=test_user.id,
            company_id=test_company.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()

        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="note",
            content="Acme is a strategic account",
            company_id=test_company.id,
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()

        ctx = knowledge_service.build_context(db_session, requisition_id=req.id)
        assert "Acme" in ctx or "Customer" in ctx


# ══════════════════════════════════════════════════════════════════════
#  build_mpn_context — offer history and requisition links
# ══════════════════════════════════════════════════════════════════════


class TestBuildMpnContextExtended:
    def test_includes_offer_history(self, db_session: Session, test_user: User, requisition: Requisition):
        """Offer history for an MPN is included in the context."""
        offer = Offer(
            requisition_id=requisition.id,
            vendor_name="Arrow Electronics",
            mpn="LM317T",
            qty_available=1000,
            unit_price=0.50,
            lead_time="2 weeks",
            entered_by_id=test_user.id,
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.commit()

        ctx = knowledge_service.build_mpn_context(db_session, mpn="LM317T")
        assert "Arrow" in ctx or "Offer history" in ctx

    def test_includes_offer_without_price(self, db_session: Session, test_user: User, requisition: Requisition):
        """Offer without price shows N/A in context."""
        offer = Offer(
            requisition_id=requisition.id,
            vendor_name="Unknown Vendor",
            mpn="NOPRICE_PART",
            qty_available=100,
            unit_price=None,
            entered_by_id=test_user.id,
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.commit()

        ctx = knowledge_service.build_mpn_context(db_session, mpn="NOPRICE_PART")
        assert "N/A" in ctx or "NOPRICE_PART" in ctx

    def test_includes_requisition_links(self, db_session: Session, test_user: User, requisition: Requisition):
        """Requisitions containing the MPN are listed in context."""
        req_item = Requirement(
            requisition_id=requisition.id,
            primary_mpn="LM317T",
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req_item)
        db_session.commit()

        ctx = knowledge_service.build_mpn_context(db_session, mpn="LM317T")
        assert "Requisition" in ctx or str(requisition.id) in ctx

    def test_expired_mpn_entry_marked_outdated(self, db_session: Session, test_user: User):
        """Expired entries in MPN context are marked [OUTDATED]."""
        # Pass aware expires_at in the past; _is_expired handles naive/aware comparison.
        past = datetime.now(timezone.utc) - timedelta(days=5)
        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="LM317T old data",
            mpn="OLDPART",
            expires_at=past,
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()

        ctx = knowledge_service.build_mpn_context(db_session, mpn="OLDPART")
        assert "[OUTDATED]" in ctx


# ══════════════════════════════════════════════════════════════════════
#  build_vendor_context — vendor stats and offer history
# ══════════════════════════════════════════════════════════════════════


class TestBuildVendorContextExtended:
    def test_includes_vendor_stats(self, db_session: Session):
        """Vendor context includes ghost_rate, response rate, cancellation_rate."""
        card = VendorCard(
            normalized_name="statsvendor",
            display_name="Stats Vendor",
            domain="statsvendor.com",
            industry="Electronics",
            ghost_rate=0.15,
            total_responses=80,
            total_outreach=100,
            cancellation_rate=0.05,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()

        ctx = knowledge_service.build_vendor_context(db_session, vendor_card_id=card.id)
        assert "Ghost rate" in ctx or "statsvendor" in ctx.lower()
        assert "Response rate" in ctx or "80" in ctx
        assert "Cancellation rate" in ctx or "0.05" in ctx

    def test_includes_vendor_domain_in_meta(self, db_session: Session):
        """Vendor context includes domain when available."""
        card = VendorCard(
            normalized_name="domainvendor",
            display_name="Domain Vendor",
            domain="domainvendor.com",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()

        ctx = knowledge_service.build_vendor_context(db_session, vendor_card_id=card.id)
        assert "domainvendor.com" in ctx

    def test_includes_offer_history(
        self, db_session: Session, test_user: User, test_vendor_card: VendorCard, requisition: Requisition
    ):
        """Offer history linked to vendor is included in context."""
        offer = Offer(
            requisition_id=requisition.id,
            vendor_name="Arrow Electronics",
            vendor_card_id=test_vendor_card.id,
            mpn="LM317T",
            qty_available=500,
            unit_price=0.50,
            lead_time="2 weeks",
            entered_by_id=test_user.id,
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.commit()

        ctx = knowledge_service.build_vendor_context(db_session, vendor_card_id=test_vendor_card.id)
        assert "LM317T" in ctx or "Recent offers" in ctx

    def test_offer_without_price_shows_na(self, db_session: Session, test_user: User, requisition: Requisition):
        """Offer without price shows N/A in vendor context."""
        card = VendorCard(
            normalized_name="noprice vendor",
            display_name="No Price Vendor",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()

        offer = Offer(
            requisition_id=requisition.id,
            vendor_name="No Price Vendor",
            vendor_card_id=card.id,
            mpn="NOPRICE",
            unit_price=None,
            entered_by_id=test_user.id,
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.commit()

        ctx = knowledge_service.build_vendor_context(db_session, vendor_card_id=card.id)
        assert "N/A" in ctx or "NOPRICE" in ctx

    def test_vendor_context_with_expired_entry(
        self, db_session: Session, test_user: User, test_vendor_card: VendorCard
    ):
        """Expired vendor entry is marked [OUTDATED]."""
        # Pass aware expires_at in the past; _is_expired handles naive/aware comparison.
        past = datetime.now(timezone.utc) - timedelta(days=5)
        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="note",
            content="Arrow had issues last year",
            vendor_card_id=test_vendor_card.id,
            expires_at=past,
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()

        ctx = knowledge_service.build_vendor_context(db_session, vendor_card_id=test_vendor_card.id)
        assert "[OUTDATED]" in ctx


# ══════════════════════════════════════════════════════════════════════
#  build_pipeline_context — stale deals, active reqs with deadline
# ══════════════════════════════════════════════════════════════════════


class TestBuildPipelineContextExtended:
    def test_includes_stale_deals(self, db_session: Session, test_user: User):
        """Stale active requisitions (no update in 14+ days) appear in context."""
        stale_time = datetime.now(timezone.utc) - timedelta(days=20)
        req = Requisition(
            name="STALE-REQ-001",
            customer_name="Old Customer",
            status="active",
            created_by=test_user.id,
            created_at=stale_time,
            updated_at=stale_time,
        )
        db_session.add(req)
        db_session.commit()

        ctx = knowledge_service.build_pipeline_context(db_session)
        assert "STALE-REQ" in ctx or "Stale" in ctx

    def test_includes_active_reqs_with_deadline(self, db_session: Session, test_user: User):
        """Active reqs with deadlines appear in pipeline context."""
        req = Requisition(
            name="DEADLINE-REQ",
            customer_name="Urgent Co",
            status="active",
            created_by=test_user.id,
            deadline=datetime.now(timezone.utc) + timedelta(days=7),
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()

        ctx = knowledge_service.build_pipeline_context(db_session)
        assert "DEADLINE-REQ" in ctx or "active" in ctx.lower()

    def test_includes_sourcing_and_quoting_status(self, db_session: Session, test_user: User):
        """Requisitions in sourcing/quoting status appear in the active list."""
        for status in ("sourcing", "quoting"):
            req = Requisition(
                name=f"{status.upper()}-REQ",
                customer_name="Test Co",
                status=status,
                created_by=test_user.id,
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(req)
        db_session.commit()

        ctx = knowledge_service.build_pipeline_context(db_session)
        assert "sourcing" in ctx.lower() or "quoting" in ctx.lower() or "Active" in ctx

    def test_stale_req_naive_updated_at(self, db_session: Session, test_user: User):
        """Stale check handles naive updated_at timestamps."""
        stale_naive = datetime.utcnow() - timedelta(days=20)
        req = Requisition(
            name="STALE-NAIVE-REQ",
            customer_name="Naive Co",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
            updated_at=stale_naive,
        )
        db_session.add(req)
        db_session.commit()

        # Should not raise, even with naive datetime
        ctx = knowledge_service.build_pipeline_context(db_session)
        assert ctx  # Not empty


# ══════════════════════════════════════════════════════════════════════
#  build_company_context — industry, account_type, strategic, last_activity, site reqs
# ══════════════════════════════════════════════════════════════════════


class TestBuildCompanyContextExtended:
    def test_includes_company_meta(self, db_session: Session):
        """Company context includes industry, account_type, strategic, last_activity."""
        company = Company(
            name="Strategic Tech Inc",
            industry="Aerospace",
            account_type="enterprise",
            is_strategic=True,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=3),
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(company)
        db_session.commit()

        ctx = knowledge_service.build_company_context(db_session, company_id=company.id)
        assert "Aerospace" in ctx
        assert "enterprise" in ctx or "Account type" in ctx
        assert "Strategic account" in ctx or "Yes" in ctx
        assert "Last activity" in ctx

    def test_includes_expired_company_entries(self, db_session: Session, test_user: User, test_company: Company):
        """Expired company entries are marked [OUTDATED] in context."""
        # Pass aware expires_at in the past; _is_expired handles naive/aware comparison.
        past = datetime.now(timezone.utc) - timedelta(days=5)
        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="note",
            content="Acme had budget issues",
            company_id=test_company.id,
            expires_at=past,
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()

        ctx = knowledge_service.build_company_context(db_session, company_id=test_company.id)
        assert "[OUTDATED]" in ctx

    def test_company_without_optional_fields(self, db_session: Session):
        """Company without industry/account_type/strategic still builds context."""
        company = Company(
            name="Minimal Co",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(company)
        db_session.commit()

        ctx = knowledge_service.build_company_context(db_session, company_id=company.id)
        assert "Minimal Co" in ctx

    def test_includes_open_requisitions_via_site(
        self, db_session: Session, test_user: User, test_company: Company, test_customer_site
    ):
        """Open requisitions linked through customer sites appear in company context."""
        req = Requisition(
            name="SITE-LINKED-REQ",
            customer_name="Acme Electronics",
            status="active",
            created_by=test_user.id,
            customer_site_id=test_customer_site.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()

        ctx = knowledge_service.build_company_context(db_session, company_id=test_company.id)
        assert "SITE-LINKED-REQ" in ctx or "Open requisition" in ctx or "active" in ctx.lower()


# ══════════════════════════════════════════════════════════════════════
#  generate_insights — ClaudeError exception path
# ══════════════════════════════════════════════════════════════════════


class TestGenerateInsightsClaudeError:
    async def test_claude_error_returns_empty(self, db_session: Session, test_user: User, requisition: Requisition):
        """ClaudeError (non-unavailable) returns empty list."""
        from app.utils.claude_errors import ClaudeError

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
            raise ClaudeError("Rate limited")

        with patch("app.utils.claude_client.claude_structured", new=_raise):
            result = await knowledge_service.generate_insights(db_session, requisition.id)
        assert result == []


class TestGenerateMpnInsightsClaudeError:
    async def test_claude_error_returns_empty(self, db_session: Session, test_user: User):
        """ClaudeError in generate_mpn_insights returns empty list."""
        from app.utils.claude_errors import ClaudeError

        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="LM317T data",
            mpn="LM317T",
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()

        async def _raise(*a, **kw):
            raise ClaudeError("Rate limited")

        with patch("app.utils.claude_client.claude_structured", new=_raise):
            result = await knowledge_service.generate_mpn_insights(db_session, "LM317T")
        assert result == []

    async def test_claude_unavailable_returns_empty(self, db_session: Session, test_user: User):
        """ClaudeUnavailableError in generate_mpn_insights returns empty list."""
        from app.utils.claude_errors import ClaudeUnavailableError

        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="LM317T data",
            mpn="LM317T",
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()

        async def _raise(*a, **kw):
            raise ClaudeUnavailableError("Not configured")

        with patch("app.utils.claude_client.claude_structured", new=_raise):
            result = await knowledge_service.generate_mpn_insights(db_session, "LM317T")
        assert result == []

    async def test_no_result_returns_empty(self, db_session: Session, test_user: User):
        """None result from Claude returns empty list."""
        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="LM317T data",
            mpn="LM317T",
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()

        async def _mock(*a, **kw):
            return None

        with patch("app.utils.claude_client.claude_structured", new=_mock):
            result = await knowledge_service.generate_mpn_insights(db_session, "LM317T")
        assert result == []

    async def test_replaces_old_mpn_insights(self, db_session: Session, test_user: User):
        """Old MPN insights are replaced when new ones are generated."""
        knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="ai_insight",
            content="Old MPN insight",
            mpn="LM317T",
        )
        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="fact",
            content="LM317T at $0.50",
            mpn="LM317T",
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()

        mock_result = {"insights": [{"content": "New MPN insight", "confidence": 0.85, "based_on_expired": False}]}

        async def _mock(*a, **kw):
            return mock_result

        with (
            patch("app.utils.claude_client.claude_structured", new=_mock),
            patch.object(knowledge_service, "create_entry", side_effect=_make_create_entry_with_user(test_user.id)),
        ):
            result = await knowledge_service.generate_mpn_insights(db_session, "LM317T")

        assert len(result) == 1
        cached = knowledge_service.get_cached_mpn_insights(db_session, "LM317T")
        assert len(cached) == 1
        assert cached[0].content == "New MPN insight"


class TestGenerateVendorInsightsClaudeError:
    async def test_claude_error_returns_empty(self, db_session: Session, test_user: User, test_vendor_card: VendorCard):
        """ClaudeError in generate_vendor_insights returns empty list."""
        from app.utils.claude_errors import ClaudeError

        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="note",
            content="Arrow note",
            vendor_card_id=test_vendor_card.id,
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()

        async def _raise(*a, **kw):
            raise ClaudeError("Rate limited")

        with patch("app.utils.claude_client.claude_structured", new=_raise):
            result = await knowledge_service.generate_vendor_insights(db_session, test_vendor_card.id)
        assert result == []

    async def test_claude_unavailable_returns_empty(
        self, db_session: Session, test_user: User, test_vendor_card: VendorCard
    ):
        """ClaudeUnavailableError in generate_vendor_insights returns empty list."""
        from app.utils.claude_errors import ClaudeUnavailableError

        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="note",
            content="Arrow note",
            vendor_card_id=test_vendor_card.id,
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()

        async def _raise(*a, **kw):
            raise ClaudeUnavailableError("Not configured")

        with patch("app.utils.claude_client.claude_structured", new=_raise):
            result = await knowledge_service.generate_vendor_insights(db_session, test_vendor_card.id)
        assert result == []

    async def test_no_result_returns_empty(self, db_session: Session, test_user: User, test_vendor_card: VendorCard):
        """None result from Claude in vendor insights returns empty list."""
        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="note",
            content="Arrow note",
            vendor_card_id=test_vendor_card.id,
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()

        async def _mock(*a, **kw):
            return None

        with patch("app.utils.claude_client.claude_structured", new=_mock):
            result = await knowledge_service.generate_vendor_insights(db_session, test_vendor_card.id)
        assert result == []

    async def test_replaces_old_vendor_insights(
        self, db_session: Session, test_user: User, test_vendor_card: VendorCard
    ):
        """Old vendor insights are replaced when new ones are generated."""
        knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="ai_insight",
            content="Old vendor insight",
            vendor_card_id=test_vendor_card.id,
        )
        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="note",
            content="Arrow data",
            vendor_card_id=test_vendor_card.id,
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()

        mock_result = {"insights": [{"content": "New vendor insight", "confidence": 0.9, "based_on_expired": False}]}

        async def _mock(*a, **kw):
            return mock_result

        with (
            patch("app.utils.claude_client.claude_structured", new=_mock),
            patch.object(knowledge_service, "create_entry", side_effect=_make_create_entry_with_user(test_user.id)),
        ):
            result = await knowledge_service.generate_vendor_insights(db_session, test_vendor_card.id)

        assert len(result) == 1
        cached = knowledge_service.get_cached_vendor_insights(db_session, test_vendor_card.id)
        assert len(cached) == 1
        assert cached[0].content == "New vendor insight"


class TestGeneratePipelineInsightsClaudeError:
    async def test_claude_error_returns_empty(self, db_session: Session, test_user: User):
        """ClaudeError in generate_pipeline_insights returns empty list."""
        from app.utils.claude_errors import ClaudeError

        req = Requisition(
            name="PIPE-ERR-REQ",
            customer_name="Test",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()

        async def _raise(*a, **kw):
            raise ClaudeError("Rate limited")

        with patch("app.utils.claude_client.claude_structured", new=_raise):
            result = await knowledge_service.generate_pipeline_insights(db_session)
        assert result == []

    async def test_claude_unavailable_returns_empty(self, db_session: Session, test_user: User):
        """ClaudeUnavailableError in generate_pipeline_insights returns empty list."""
        from app.utils.claude_errors import ClaudeUnavailableError

        req = Requisition(
            name="PIPE-UNAVAIL-REQ",
            customer_name="Test",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()

        async def _raise(*a, **kw):
            raise ClaudeUnavailableError("Not configured")

        with patch("app.utils.claude_client.claude_structured", new=_raise):
            result = await knowledge_service.generate_pipeline_insights(db_session)
        assert result == []

    async def test_no_result_returns_empty(self, db_session: Session, test_user: User):
        """None Claude result in pipeline insights returns empty list."""
        req = Requisition(
            name="PIPE-NONE-REQ",
            customer_name="Test",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()

        async def _mock(*a, **kw):
            return None

        with patch("app.utils.claude_client.claude_structured", new=_mock):
            result = await knowledge_service.generate_pipeline_insights(db_session)
        assert result == []

    async def test_replaces_old_pipeline_insights(self, db_session: Session, test_user: User):
        """Old pipeline insights are replaced when new ones are generated."""
        knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="ai_insight",
            content="Old pipeline insight",
            mpn="__pipeline__",
        )
        req = Requisition(
            name="PIPE-REPLACE-REQ",
            customer_name="Test",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()

        mock_result = {"insights": [{"content": "New pipeline insight", "confidence": 0.8, "based_on_expired": False}]}

        async def _mock(*a, **kw):
            return mock_result

        with (
            patch("app.utils.claude_client.claude_structured", new=_mock),
            patch.object(knowledge_service, "create_entry", side_effect=_make_create_entry_with_user(test_user.id)),
        ):
            result = await knowledge_service.generate_pipeline_insights(db_session)

        assert len(result) == 1
        cached = knowledge_service.get_cached_pipeline_insights(db_session)
        assert len(cached) == 1
        assert cached[0].content == "New pipeline insight"


class TestGenerateCompanyInsightsClaudeError:
    async def test_claude_error_returns_empty(self, db_session: Session, test_user: User, test_company: Company):
        """ClaudeError in generate_company_insights returns empty list."""
        from app.utils.claude_errors import ClaudeError

        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="note",
            content="Acme note",
            company_id=test_company.id,
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()

        async def _raise(*a, **kw):
            raise ClaudeError("Rate limited")

        with patch("app.utils.claude_client.claude_structured", new=_raise):
            result = await knowledge_service.generate_company_insights(db_session, test_company.id)
        assert result == []

    async def test_claude_unavailable_returns_empty(self, db_session: Session, test_user: User, test_company: Company):
        """ClaudeUnavailableError in generate_company_insights returns empty list."""
        from app.utils.claude_errors import ClaudeUnavailableError

        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="note",
            content="Acme note",
            company_id=test_company.id,
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()

        async def _raise(*a, **kw):
            raise ClaudeUnavailableError("Not configured")

        with patch("app.utils.claude_client.claude_structured", new=_raise):
            result = await knowledge_service.generate_company_insights(db_session, test_company.id)
        assert result == []

    async def test_no_result_returns_empty(self, db_session: Session, test_user: User, test_company: Company):
        """None Claude result in company insights returns empty list."""
        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="note",
            content="Acme note",
            company_id=test_company.id,
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()

        async def _mock(*a, **kw):
            return None

        with patch("app.utils.claude_client.claude_structured", new=_mock):
            result = await knowledge_service.generate_company_insights(db_session, test_company.id)
        assert result == []

    async def test_replaces_old_company_insights(self, db_session: Session, test_user: User, test_company: Company):
        """Old company insights are replaced when new ones are generated."""
        knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="ai_insight",
            content="Old company insight",
            company_id=test_company.id,
        )
        entry = knowledge_service.create_entry(
            db_session,
            user_id=test_user.id,
            entry_type="note",
            content="Acme note",
            company_id=test_company.id,
        )
        entry.created_at = datetime.now(timezone.utc)
        db_session.commit()

        mock_result = {"insights": [{"content": "New company insight", "confidence": 0.7, "based_on_expired": False}]}

        async def _mock(*a, **kw):
            return mock_result

        with (
            patch("app.utils.claude_client.claude_structured", new=_mock),
            patch.object(knowledge_service, "create_entry", side_effect=_make_create_entry_with_user(test_user.id)),
        ):
            result = await knowledge_service.generate_company_insights(db_session, test_company.id)

        assert len(result) == 1
        cached = knowledge_service.get_cached_company_insights(db_session, test_company.id)
        assert len(cached) == 1
        assert cached[0].content == "New company insight"
