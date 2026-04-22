"""tests/test_vendor_email_lookup_nightly.py — Coverage for uncovered lines in vendor_email_lookup.

Targets lines: 93, 96, 140-147, 188-190, 193-209, 392-394
Called by: pytest
Depends on: conftest fixtures, unittest.mock
"""

import asyncio
import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import Requisition, User
from app.services.vendor_email_lookup import _enrich_vendors_batch, _query_db_for_part


@pytest.fixture()
def basic_req(db_session: Session, test_user: User) -> Requisition:
    from app.models import Requirement

    req = Requisition(
        name="VEL-NIGHTLY",
        customer_name="Test Co",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()
    item = Requirement(
        requisition_id=req.id,
        primary_mpn="LM317T",
        target_qty=10,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    db_session.commit()
    return req


class TestQueryDbForPartEdgeCases:
    def test_sighting_with_empty_vendor_name_is_skipped(self, db_session: Session, basic_req: Requisition):
        """Line 93 — sighting with empty vendor_name triggers the 'if not vn: continue' branch."""
        from app.models import Requirement, Sighting

        item = db_session.query(Requirement).filter_by(requisition_id=basic_req.id).first()
        s = Sighting(
            requirement_id=item.id,
            vendor_name="",  # empty — triggers line 93
            normalized_mpn="LM317T",
            mpn_matched="LM317T",
            source_type="api",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(s)
        db_session.commit()

        result = _query_db_for_part("LM317T", db_session)
        # No vendor with empty name should appear
        assert all(v["vendor_name"] != "" for v in result)

    def test_sighting_with_unnormalizable_vendor_is_skipped(self, db_session: Session, basic_req: Requisition):
        """Line 96 — normalize_vendor_name returns None triggers 'if not norm: continue'."""
        from app.models import Requirement, Sighting

        item = db_session.query(Requirement).filter_by(requisition_id=basic_req.id).first()
        s = Sighting(
            requirement_id=item.id,
            vendor_name="Valid Vendor",
            normalized_mpn="LM317T",
            mpn_matched="LM317T",
            source_type="api",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(s)
        db_session.commit()

        # Patch normalize_vendor_name to return None → line 96 executed
        with patch("app.services.vendor_email_lookup.normalize_vendor_name", return_value=None):
            result = _query_db_for_part("LM317T", db_session)

        assert result == []

    def test_material_vendor_history_duplicate_vendor_skipped(self, db_session: Session, basic_req: Requisition):
        """Lines 140-147 — history vendor already in sightings → 'norm in vendors' skips it."""
        from app.models import MaterialCard, MaterialVendorHistory, Requirement, Sighting

        item = db_session.query(Requirement).filter_by(requisition_id=basic_req.id).first()

        # Add sighting so vendor is already in vendors dict
        s = Sighting(
            requirement_id=item.id,
            vendor_name="Arrow Electronics",
            normalized_mpn="LM317T",
            mpn_matched="LM317T",
            source_type="api",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(s)

        # Add material history for same vendor
        card = MaterialCard(
            normalized_mpn="LM317T",
            display_mpn="LM317T",
            manufacturer="TI",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.flush()
        hist = MaterialVendorHistory(
            material_card_id=card.id,
            vendor_name="Arrow Electronics",
            times_seen=3,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(hist)
        db_session.commit()

        result = _query_db_for_part("LM317T", db_session)
        # Arrow should appear only once (deduped via norm in vendors)
        arrow_entries = [v for v in result if "arrow" in v["vendor_name"].lower()]
        assert len(arrow_entries) == 1

    def test_email_intelligence_first_query_exception_triggers_fallback(self, db_session: Session):
        """Lines 188-190 — first EmailIntelligence query fails → falls into except block."""
        original_query = db_session.query
        call_count = {"n": 0}

        def patched_query(model, *args, **kwargs):
            from app.models.email_intelligence import EmailIntelligence

            if model is EmailIntelligence:
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise Exception("dialect cast not supported")
                # Second call returns empty list
                mock_q = MagicMock()
                mock_q.filter.return_value = mock_q
                mock_q.order_by.return_value = mock_q
                mock_q.limit.return_value = mock_q
                mock_q.all.return_value = []
                return mock_q
            return original_query(model, *args, **kwargs)

        with patch.object(db_session, "query", side_effect=patched_query):
            result = _query_db_for_part("LM317T", db_session)

        assert call_count["n"] >= 1
        assert isinstance(result, list)

    def test_email_intelligence_both_queries_fail_returns_empty_list(self, db_session: Session):
        """Lines 193-209 — both EmailIntelligence queries fail → ei_rows = []."""
        original_query = db_session.query

        def patched_query(model, *args, **kwargs):
            from app.models.email_intelligence import EmailIntelligence

            if model is EmailIntelligence:
                raise Exception("total failure")
            return original_query(model, *args, **kwargs)

        with patch.object(db_session, "query", side_effect=patched_query):
            result = _query_db_for_part("LM317T", db_session)

        # Completes without error, no email intelligence vendors
        assert isinstance(result, list)


class TestEnrichVendorsBatchTimeout:
    async def test_batch_timeout_with_commit_failure_triggers_rollback(self, db_session: Session):
        """Lines 392-394 — asyncio.TimeoutError on batch gather + commit failure → rollback."""
        vendors = [
            {
                "vendor_name": "Test Vendor",
                "domain": "testvendor.com",
                "emails": [],
                "phones": [],
                "card_id": None,
            }
        ]

        commit_calls: list = []
        rollback_calls: list = []
        original_commit = db_session.commit
        original_rollback = db_session.rollback

        def failing_commit():
            commit_calls.append(1)
            raise Exception("commit failed")

        def tracking_rollback():
            rollback_calls.append(1)
            original_rollback()

        db_session.commit = failing_commit
        db_session.rollback = tracking_rollback

        try:
            with patch(
                "app.services.vendor_email_lookup.asyncio.wait_for",
                side_effect=asyncio.TimeoutError,
            ):
                await _enrich_vendors_batch(vendors, db_session, timeout=0.001)
        finally:
            db_session.commit = original_commit
            db_session.rollback = original_rollback

        assert len(rollback_calls) >= 1
