"""tests/test_coverage_boost_nightly.py — Targeted tests for coverage gaps.

Covers branches missed by existing tests in:
- app/services/activity_quality_service.py (SIGHTING_ADDED branches)
- app/services/ai_email_parser.py (ClaudeUnavailableError, ClaudeError)
- app/services/company_merge_service.py (exception paths in merge)
- app/services/prospect_discovery_email.py (vendor domains, apollo fallback error)
- app/services/requisition_service.py (substitute dedup)
- app/utils/vendor_helpers.py (commit failure paths)

Called by: pytest
Depends on: conftest fixtures, app service modules
"""

import asyncio
import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import Company, Requirement, Requisition, User, VendorCard
from app.models.intelligence import ActivityLog
from tests.conftest import engine  # noqa: F401

# ── activity_quality_service ─────────────────────────────────────────────────


class TestActivityQualitySightingAdded:
    """Cover SIGHTING_ADDED branches in score_activity (lines 119-129)."""

    async def test_sighting_added_with_notes_reaches_claude(self, db_session: Session, test_user: User):
        """SIGHTING_ADDED with notes (but no count/sources) builds a prompt and calls Claude."""
        from app.services.activity_quality_service import score_activity

        log = ActivityLog(
            user_id=test_user.id,
            activity_type="sighting_added",
            channel="system",
            notes="Found 500 units at spot price",
            details={},
        )
        db_session.add(log)
        db_session.flush()

        mock_result = {
            "is_meaningful": True,
            "quality_score": 70,
            "classification": "sighting",
            "sentiment": "positive",
            "clean_summary": "Batch sighting with notes.",
        }

        with patch(
            "app.utils.claude_client.claude_structured",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            await score_activity(log.id, db_session)

        db_session.refresh(log)
        assert log.quality_assessed_at is not None
        assert log.quality_score == 70.0

    async def test_sighting_added_empty_details_marks_no_data(self, db_session: Session, test_user: User):
        """SIGHTING_ADDED with no count, no sources, no notes → no_data path (lines 123-129)."""
        from app.services.activity_quality_service import score_activity

        log = ActivityLog(
            user_id=test_user.id,
            activity_type="sighting_added",
            channel="system",
            details={},
            notes=None,
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
        assert log.summary == "No interaction details available"
        assert log.quality_assessed_at is not None

    async def test_sighting_added_with_count_reaches_claude(self, db_session: Session, test_user: User):
        """SIGHTING_ADDED with count in details builds prompt and calls Claude."""
        from app.services.activity_quality_service import score_activity

        log = ActivityLog(
            user_id=test_user.id,
            activity_type="sighting_added",
            channel="system",
            details={"count": 12, "sources": ["brokerbin", "nexar"]},
        )
        db_session.add(log)
        db_session.flush()

        mock_result = {
            "is_meaningful": True,
            "quality_score": 55,
            "classification": "sighting",
            "sentiment": "neutral",
            "clean_summary": "12 sightings from 2 sources.",
        }

        with patch(
            "app.utils.claude_client.claude_structured",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            await score_activity(log.id, db_session)

        db_session.refresh(log)
        assert log.quality_score == 55.0


# ── ai_email_parser ──────────────────────────────────────────────────────────


class TestAiEmailParserExceptions:
    """Cover ClaudeUnavailableError and ClaudeError branches (lines 127-132)."""

    async def test_claude_unavailable_returns_none(self):
        """ClaudeUnavailableError → logs info and returns None."""
        from app.services.ai_email_parser import parse_email
        from app.utils.claude_errors import ClaudeUnavailableError

        with patch(
            "app.services.ai_email_parser.claude_json",
            new_callable=AsyncMock,
            side_effect=ClaudeUnavailableError("not configured"),
        ):
            result = await parse_email(
                email_body="We have 100 units of LM317T at $0.50 each.",
                email_subject="RE: RFQ",
                vendor_name="Test Vendor",
            )
        assert result is None

    async def test_claude_error_returns_none(self):
        """ClaudeError → logs warning and returns None."""
        from app.services.ai_email_parser import parse_email
        from app.utils.claude_errors import ClaudeError

        with patch(
            "app.services.ai_email_parser.claude_json",
            new_callable=AsyncMock,
            side_effect=ClaudeError("quota exceeded"),
        ):
            result = await parse_email(
                email_body="We have 100 units of LM317T at $0.50 each.",
                email_subject="RE: RFQ",
                vendor_name="Test Vendor",
            )
        assert result is None


# ── company_merge_service ────────────────────────────────────────────────────


class TestCompanyMergeEdgePaths:
    """Cover exception paths in merge_companies (lines 146-147, 158-159)."""

    def test_merge_with_reassign_exception_still_completes(self, db_session: Session):
        """FK reassignment raising an exception is caught; merge still completes."""
        from app.services.company_merge_service import merge_companies

        keep = Company(name="KeepCo", is_active=True)
        remove = Company(name="RemoveCo", is_active=True)
        db_session.add_all([keep, remove])
        db_session.commit()

        # Capture the real query method
        original_query = db_session.query
        call_count = [0]

        def _sometimes_raise(model, *args, **kwargs):
            call_count[0] += 1
            # On the 2nd FK-reassign query, return a mock that raises on update
            if call_count[0] == 2:
                mock_q = MagicMock()
                mock_q.filter.return_value.update.side_effect = Exception("FK reassign error")
                return mock_q
            return original_query(model, *args, **kwargs)

        with patch.object(db_session, "query", side_effect=_sometimes_raise):
            result = merge_companies(keep.id, remove.id, db_session)

        db_session.commit()
        # Merge still completed despite one FK error
        assert result["kept"] == keep.id

    def test_merge_cache_invalidation_exception_is_swallowed(self, db_session: Session):
        """Cache invalidation failure is caught and doesn't abort merge (lines 158-159)."""
        from app.services.company_merge_service import merge_companies

        keep = Company(name="AlphaCo", is_active=True)
        remove = Company(name="BetaCo", is_active=True)
        db_session.add_all([keep, remove])
        db_session.commit()

        # Patch invalidate_prefix at its definition site so the import-inside-try finds it raising
        with patch("app.cache.decorators.invalidate_prefix", side_effect=Exception("Redis down")):
            result = merge_companies(keep.id, remove.id, db_session)

        db_session.commit()
        assert result["kept"] == keep.id


# ── prospect_discovery_email ─────────────────────────────────────────────────


class TestProspectDiscoveryEmailVendorDomains:
    """Cover vendor_domains loop (lines 88-92) and apollo error (lines 185-186)."""

    def test_mine_unknown_domains_excludes_vendor_domains(self, db_session: Session):
        """VendorCard emails populate vendor_domains; those domains are excluded."""
        from app.services.prospect_discovery_email import mine_unknown_domains

        # Create a vendor card with emails from known domain
        card = VendorCard(
            normalized_name="supply vendor",
            display_name="Supply Vendor",
            emails=["sales@supplyvendor.com", "info@supplyvendor.com"],
        )
        db_session.add(card)
        db_session.commit()

        graph_client = AsyncMock()
        # Two emails from vendor domain — should be excluded
        graph_client.list_messages = AsyncMock(
            return_value=[
                {
                    "from": {"emailAddress": {"address": "a@supplyvendor.com", "name": "A"}},
                    "receivedDateTime": "2026-03-01",
                },
                {
                    "from": {"emailAddress": {"address": "b@supplyvendor.com", "name": "B"}},
                    "receivedDateTime": "2026-03-02",
                },
                {
                    "from": {"emailAddress": {"address": "x@unknownco.com", "name": "X"}},
                    "receivedDateTime": "2026-03-03",
                },
                {
                    "from": {"emailAddress": {"address": "y@unknownco.com", "name": "Y"}},
                    "receivedDateTime": "2026-03-04",
                },
            ]
        )

        result = asyncio.get_event_loop().run_until_complete(
            mine_unknown_domains(graph_client, db_session, days_back=30)
        )
        domains = [r["domain"] for r in result]
        # supplyvendor.com is a vendor domain → excluded
        assert "supplyvendor.com" not in domains
        # unknownco.com has 2 emails and is not a vendor → included
        assert "unknownco.com" in domains

    async def test_enrich_email_domains_apollo_exception_swallowed(self):
        """apollo_enrich_fn raising an exception is caught (lines 185-186)."""
        from app.services.prospect_discovery_email import enrich_email_domains

        domains = [{"domain": "failapollo.com", "email_count": 3, "sample_senders": []}]

        async def fail_primary(domain):
            return None

        async def fail_apollo(domain):
            raise RuntimeError("Apollo API timeout")

        result = await enrich_email_domains(
            domains,
            enrich_fn=fail_primary,
            apollo_enrich_fn=fail_apollo,
        )
        # Both enrichment functions failed — domain skipped, empty result
        assert result == []


# ── requisition_service ──────────────────────────────────────────────────────


class TestCloneRequisitionSubstituteDedup:
    """Cover substitute dedup logic in clone_requisition (lines 112-116)."""

    def test_clone_deduplicates_normalized_substitutes(self, db_session: Session, test_user: User):
        """Duplicate normalized substitutes are deduplicated during clone."""
        from app.services.requisition_service import clone_requisition

        req = Requisition(
            name="REQ-CLONE-DEDUP",
            customer_name="Acme",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        # Include primary MPN in substitutes — it normalizes to the same key and should be deduped
        item = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            target_qty=100,
            substitutes=["lm317t", "LM340T"],  # lm317t normalizes same as primary → deduped
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(req)

        new_req = clone_requisition(db_session, req, test_user.id)
        db_session.commit()

        assert new_req is not None
        assert new_req.id != req.id
        cloned_items = db_session.query(Requirement).filter_by(requisition_id=new_req.id).all()
        assert len(cloned_items) == 1
        subs = cloned_items[0].substitutes or []
        # lm317t (same key as primary LM317T) should be deduped; LM340T kept
        assert len(subs) == 1

    def test_clone_with_valid_distinct_substitutes(self, db_session: Session, test_user: User):
        """Distinct substitutes are all preserved in cloned requirement."""
        from app.services.requisition_service import clone_requisition

        req = Requisition(
            name="REQ-CLONE-SUBS",
            customer_name="Acme",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        item = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            target_qty=500,
            substitutes=["LM340T", "UA7805"],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(req)

        new_req = clone_requisition(db_session, req, test_user.id)
        db_session.commit()

        cloned_items = db_session.query(Requirement).filter_by(requisition_id=new_req.id).all()
        assert len(cloned_items) == 1
        subs = cloned_items[0].substitutes or []
        assert len(subs) == 2


# ── vendor_helpers ───────────────────────────────────────────────────────────


class TestVendorHelpersCommitFailures:
    """Cover commit-failure exception paths in get_or_create_card (lines 67-69, 133-135, 150-153)."""

    def test_domain_match_commit_failure_logs_and_rolls_back(self, db_session: Session):
        """Commit failure when updating alt names for domain-matched vendor is caught (lines 67-69)."""
        from app.utils.vendor_helpers import get_or_create_card

        # Create a vendor with a known domain
        card = VendorCard(
            normalized_name="orig vendor",
            display_name="Orig Vendor",
            domain="origvendor.com",
            emails=[],
            phones=[],
        )
        db_session.add(card)
        db_session.commit()

        with patch.object(db_session, "commit", side_effect=Exception("DB error")):
            # Should not raise — commit failure is caught and rolled back
            result = get_or_create_card("Orig Vendor Alias", db_session, domain="origvendor.com")

        # Returns the domain-matched card despite commit failure
        assert result is not None
        assert result.id == card.id

    def test_new_card_commit_failure_raises(self, db_session: Session):
        """Commit failure when creating new VendorCard re-raises after rollback (lines 150-153)."""
        from app.utils.vendor_helpers import get_or_create_card

        with patch.object(db_session, "commit", side_effect=Exception("write failed")):
            with pytest.raises(Exception, match="write failed"):
                get_or_create_card("BrandNewVendorXYZ999", db_session)
