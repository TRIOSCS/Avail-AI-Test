"""test_coverage_boost_final.py — Covers specific missing lines across low-coverage modules.

Targets:
- sourcing_auto_progress: validate_transition blocked forward jump (lines 51-56)
- ai_email_parser: ClaudeUnavailableError / ClaudeError (lines 127-132)
- requisition_service: clone with substitutes dedup loop (lines 112-116)
- company_merge_service: reassign exception + cache exception (lines 146-147, 158-159)
- routers/documents: ValueError for valid req/quote (lines 37, 68)
- activity_quality_service: SIGHTING_ADDED with notes / no data (lines 120, 123-129)
- vendor_helpers: domain-match commit failure / new-card commit failure (lines 67-69, 150-153)

Called by: pytest
Depends on: tests/conftest.py
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

# ── sourcing_auto_progress: validate_transition blocked ──────────────────────


class TestAutoProgressValidationBlocked:
    """Lines 51-56: validate_transition returns False for disallowed forward jumps."""

    def test_open_to_won_blocked(self, db_session: Session, test_user):
        from app.constants import SourcingStatus
        from app.models import Requirement, Requisition
        from app.services.sourcing_auto_progress import auto_progress_status

        req = Requisition(name="BlockedReq", status="active", created_by=test_user.id)
        db_session.add(req)
        db_session.flush()
        r = Requirement(requisition_id=req.id, primary_mpn="LM317T", target_qty=10, sourcing_status=SourcingStatus.OPEN)
        db_session.add(r)
        db_session.flush()

        # OPEN → WON: forward in _STATUS_ORDER but not an allowed transition
        result = auto_progress_status(r, SourcingStatus.WON, db_session, test_user.id)

        assert result is False
        assert r.sourcing_status == SourcingStatus.OPEN

    def test_open_to_quoted_blocked(self, db_session: Session, test_user):
        from app.constants import SourcingStatus
        from app.models import Requirement, Requisition
        from app.services.sourcing_auto_progress import auto_progress_status

        req = Requisition(name="BlockedReq2", status="active", created_by=test_user.id)
        db_session.add(req)
        db_session.flush()
        r = Requirement(requisition_id=req.id, primary_mpn="ABC123", target_qty=5, sourcing_status=SourcingStatus.OPEN)
        db_session.add(r)
        db_session.flush()

        result = auto_progress_status(r, SourcingStatus.QUOTED, db_session, test_user.id)

        assert result is False


# ── ai_email_parser: Claude exception handlers ───────────────────────────────


class TestEmailParserClaudeErrors:
    """Lines 127-132: ClaudeUnavailableError and ClaudeError must return None."""

    async def test_claude_unavailable_returns_none(self):
        from app.services.ai_email_parser import parse_email
        from app.utils.claude_errors import ClaudeUnavailableError

        with patch(
            "app.services.ai_email_parser.claude_json",
            new_callable=AsyncMock,
            side_effect=ClaudeUnavailableError("not configured"),
        ):
            result = await parse_email("Stock of 1000 units at $0.50.", "RFQ Reply", "Arrow")

        assert result is None

    async def test_claude_error_returns_none(self):
        from app.services.ai_email_parser import parse_email
        from app.utils.claude_errors import ClaudeError

        with patch(
            "app.services.ai_email_parser.claude_json",
            new_callable=AsyncMock,
            side_effect=ClaudeError("Rate limit hit"),
        ):
            result = await parse_email("We can offer 500 pcs at $1.20.", "RE: RFQ", "Mouser")

        assert result is None


# ── requisition_service: clone with substitutes ──────────────────────────────


class TestCloneRequisitionSubstitutes:
    """Lines 112-116: dedup loop for substitutes in clone_requisition."""

    def test_clone_with_substitutes_deduped(self, db_session: Session, test_user):
        from app.models import Requirement, Requisition
        from app.services.requisition_service import clone_requisition

        src = Requisition(name="SRC-SUBS", status="active", created_by=test_user.id)
        db_session.add(src)
        db_session.flush()

        r = Requirement(
            requisition_id=src.id,
            primary_mpn="LM317T",
            target_qty=100,
            substitutes=["LM317T-REF", "LM317AT", "LM317T-REF"],  # dup at index 0 and 2
        )
        db_session.add(r)
        db_session.commit()

        cloned = clone_requisition(db_session, src, test_user.id)
        cloned_reqs = list(cloned.requirements)
        assert len(cloned_reqs) == 1
        assert len(cloned_reqs[0].substitutes) == 2  # duplicate removed

    def test_clone_empty_substitutes_no_error(self, db_session: Session, test_user):
        from app.models import Requirement, Requisition
        from app.services.requisition_service import clone_requisition

        src = Requisition(name="SRC-NOSUBS", status="active", created_by=test_user.id)
        db_session.add(src)
        db_session.flush()

        r = Requirement(requisition_id=src.id, primary_mpn="STM32F407", target_qty=10, substitutes=[])
        db_session.add(r)
        db_session.commit()

        cloned = clone_requisition(db_session, src, test_user.id)
        cloned_reqs = list(cloned.requirements)
        assert cloned_reqs[0].substitutes == []


# ── company_merge_service: exception handlers ────────────────────────────────


class TestCompanyMergeExceptions:
    """Lines 146-147, 158-159: exception handlers must not abort the merge."""

    def test_reassign_exception_logged_but_merge_succeeds(self, db_session: Session):
        from sqlalchemy.orm import Query

        from app.models import Company
        from app.services.company_merge_service import merge_companies

        keep = Company(name="Keep Corp", is_active=True)
        remove = Company(name="Remove Corp", is_active=True)
        db_session.add_all([keep, remove])
        db_session.commit()

        original_update = Query.update
        call_count = [0]

        def patched_update(self, values, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("Simulated FK reassign failure")
            return original_update(self, values, **kwargs)

        with patch.object(Query, "update", patched_update):
            result = merge_companies(keep.id, remove.id, db_session)
            db_session.commit()

        assert result["ok"] is True

    def test_cache_exception_logged_but_merge_succeeds(self, db_session: Session):
        from app.models import Company
        from app.services.company_merge_service import merge_companies

        keep = Company(name="Cache Keep", is_active=True)
        remove = Company(name="Cache Remove", is_active=True)
        db_session.add_all([keep, remove])
        db_session.commit()

        with patch("app.cache.decorators.invalidate_prefix", side_effect=Exception("Cache unavailable")):
            result = merge_companies(keep.id, remove.id, db_session)
            db_session.commit()

        assert result["ok"] is True


# ── routers/documents: ValueError from PDF service ───────────────────────────


class TestDocumentsValueError:
    """Lines 37, 68: ValueError from service for a valid req/quote → 404."""

    @patch(
        "app.services.document_service.generate_rfq_summary_pdf",
        side_effect=ValueError("No requirements found"),
    )
    def test_rfq_pdf_value_error_returns_404(self, _mock, client, test_requisition):
        resp = client.get(f"/api/requisitions/{test_requisition.id}/pdf")
        assert resp.status_code == 404

    @patch(
        "app.services.document_service.generate_quote_report_pdf",
        side_effect=ValueError("Quote has no line items"),
    )
    def test_quote_pdf_value_error_returns_404(self, _mock, client, test_quote):
        resp = client.get(f"/api/quotes/{test_quote.id}/pdf")
        assert resp.status_code == 404


# ── activity_quality_service: SIGHTING_ADDED branches ───────────────────────


class TestActivityQualitySightingAdded:
    """Lines 120, 123-129: SIGHTING_ADDED with/without notes."""

    async def test_sighting_added_with_notes_calls_claude(self, db_session: Session, test_user):
        """Line 120: notes contribute to parts → Claude is called."""
        from app.constants import ActivityType
        from app.models import ActivityLog
        from app.services.activity_quality_service import score_activity

        log = ActivityLog(
            user_id=test_user.id,
            activity_type=ActivityType.SIGHTING_ADDED,
            channel="system",
            notes="Found 500 units at Arrow Electronics",
            details={},  # no count/sources — only notes in parts
        )
        db_session.add(log)
        db_session.flush()

        with patch(
            "app.utils.claude_client.claude_structured",
            new_callable=AsyncMock,
        ) as mock_claude:
            mock_claude.return_value = {
                "quality_score": 0.7,
                "is_meaningful": True,
                "classification": "vendor_sighting",
                "summary": "Found stock at Arrow",
            }
            await score_activity(log.id, db_session)
            mock_claude.assert_called_once()

    async def test_sighting_added_no_data_skips_claude(self, db_session: Session, test_user):
        """Lines 123-129: no count/sources/notes → no_data branch, Claude not called."""
        from app.constants import ActivityType
        from app.models import ActivityLog
        from app.services.activity_quality_service import score_activity

        log = ActivityLog(
            user_id=test_user.id,
            activity_type=ActivityType.SIGHTING_ADDED,
            channel="system",
            notes=None,
            details={},
        )
        db_session.add(log)
        db_session.flush()

        with patch(
            "app.utils.claude_client.claude_structured",
            new_callable=AsyncMock,
        ) as mock_claude:
            await score_activity(log.id, db_session)
            mock_claude.assert_not_called()

        db_session.refresh(log)
        assert log.quality_score == 0.0
        assert log.quality_classification == "no_data"
        assert log.is_meaningful is False
        assert log.quality_assessed_at is not None


# ── vendor_helpers: commit failure handlers ──────────────────────────────────


class TestVendorHelpersCommitFailures:
    """Lines 67-69, 150-153: commit failure paths in get_or_create_card."""

    def test_domain_match_commit_failure_logged_returns_card(self, db_session: Session):
        """Lines 67-69: commit failure when saving alt name on domain match."""
        from app.models import VendorCard
        from app.utils.vendor_helpers import get_or_create_card

        # Create a card with a known domain (flush only, no commit)
        card = VendorCard(
            display_name="Arrow Electronics",
            normalized_name="arrow electronics",
            domain="arrow.com",
            emails=[],
            phones=[],
        )
        db_session.add(card)
        db_session.flush()

        # Patch commit to raise so the alt-name update path hits lines 67-69
        with patch.object(db_session, "commit", side_effect=Exception("DB error")):
            with patch.object(db_session, "rollback"):
                result = get_or_create_card("Arrow USA", db_session, domain="arrow.com")

        # Card is returned even after the commit failure
        assert result is not None
        assert result.normalized_name == "arrow electronics"

    def test_new_card_commit_failure_raises(self, db_session: Session):
        """Lines 150-153: commit failure on new VendorCard creation re-raises."""
        from app.utils.vendor_helpers import get_or_create_card

        with patch.object(db_session, "commit", side_effect=Exception("DB write error")):
            with patch.object(db_session, "rollback"):
                with pytest.raises(Exception, match="DB write error"):
                    get_or_create_card("TotallyNewVendorXYZ999", db_session)
