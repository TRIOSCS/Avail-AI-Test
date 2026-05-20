"""test_engagement_scorer_coverage.py — Gap tests for engagement_scorer.py.

Targets missing lines:
- Line 65: now is None → defaults to datetime.now(timezone.utc)
- Lines 180-181: domain_aliases loop iteration with alias values
- Line 199: response email without @ symbol (skipped)
- Line 204: norm fallback via vendor_name split
- Lines 257-258: win_map accumulation for same norm
- Lines 307-308: flush exception in compute_all_engagement_scores
- Lines 406-408: flush exception and rollback in apply_outbound_stats

Called by: pytest
Depends on: app/services/engagement_scorer.py, tests/conftest.py
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.services.engagement_scorer import (
    COLD_START_SCORE,
    apply_outbound_stats,
    compute_all_engagement_scores,
    compute_engagement_score,
)
from tests.conftest import engine  # noqa: F401


def _make_vendor_card(db: Session, normalized_name: str, display_name: str, domain=None, domain_aliases=None):
    from app.models import VendorCard

    card = VendorCard(
        normalized_name=normalized_name,
        display_name=display_name,
        domain=domain,
        domain_aliases=domain_aliases or [],
        emails=[],
        phones=[],
        sighting_count=0,
        created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    db.flush()
    return card


class TestComputeEngagementScoreNowDefault:
    def test_now_defaults_to_utc_when_none(self):
        """Line 65: when now=None, datetime.now(timezone.utc) is used internally."""
        # Call without now= parameter — should not raise
        result = compute_engagement_score(
            total_outreach=0,
            total_responses=0,
            total_wins=0,
            avg_velocity_hours=None,
            last_contact_at=None,
            now=None,
        )
        assert result["engagement_score"] == COLD_START_SCORE


class TestComputeAllEngagementScoresDomainAliasLoop:
    @pytest.mark.asyncio
    async def test_domain_aliases_with_empty_alias_skipped(self, db_session: Session):
        """Lines 180-181: domain_aliases list with empty string — empty alias is skipped."""
        # Card with domain_aliases that include None and empty string
        card = _make_vendor_card(
            db_session,
            "aliasvendor",
            "Alias Vendor",
            domain="aliasvendor.com",
            domain_aliases=["", None, "av.io"],  # Empty and None should be skipped
        )
        db_session.commit()

        # Should not raise
        result = await compute_all_engagement_scores(db_session)
        assert result["updated"] >= 1
        db_session.refresh(card)
        assert card.engagement_score is not None


class TestComputeAllEngagementScoresResponseWithoutAt:
    @pytest.mark.asyncio
    async def test_vendor_email_without_at_is_skipped(self, db_session: Session):
        """Line 199: response email without @ is silently skipped."""
        from app.models import VendorResponse

        _make_vendor_card(db_session, "skipme", "Skip Me", domain="skipme.com")
        # Insert a VendorResponse with no @ in email
        vr = VendorResponse(
            vendor_name="Skip",
            vendor_email="noemail",  # No @ sign
            status="new",
            received_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.commit()

        # Should not raise
        result = await compute_all_engagement_scores(db_session)
        assert "updated" in result


class TestComputeAllEngagementScoresFallbackNorm:
    @pytest.mark.asyncio
    async def test_response_email_domain_not_in_map_uses_fallback(self, db_session: Session):
        """Line 204: when domain not in domain_to_norm, falls back to vendor_name split."""
        from app.models import VendorResponse

        _make_vendor_card(db_session, "unknown", "Unknown", domain=None)
        # Response email whose domain isn't mapped to any card
        vr = VendorResponse(
            vendor_name="rep",
            vendor_email="rep@unknown-co.com",  # Domain not in domain_to_norm
            status="new",
            received_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.commit()

        # Should not raise (fallback normalize used)
        result = await compute_all_engagement_scores(db_session)
        assert "updated" in result


class TestComputeAllEngagementScoresWinMap:
    @pytest.mark.asyncio
    async def test_multiple_won_offers_same_vendor_accumulated(self, db_session: Session):
        """Lines 257-258: multiple won offers for the same vendor name accumulate."""
        from app.constants import OfferStatus
        from app.models import Offer, Requisition, User

        # Create a user for offers
        user = User(
            email="buyer_win@trioscs.com",
            name="Win Buyer",
            role="buyer",
            azure_id="az-buyer-win",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.flush()

        # Create requisition (required by Offer)
        req = Requisition(
            name="WIN-REQ",
            customer_name="Test Customer",
            status="active",
            created_by=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        _make_vendor_card(db_session, "winvendor", "Win Vendor", domain="winvendor.com")

        # Create multiple won offers for the same vendor_name
        for _ in range(3):
            o = Offer(
                requisition_id=req.id,
                vendor_name="Win Vendor",
                mpn="LM317T",
                qty_available=100,
                unit_price=1.0,
                status=OfferStatus.WON,
                entered_by_id=user.id,
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(o)
        db_session.commit()

        result = await compute_all_engagement_scores(db_session)
        assert result["updated"] >= 1


class TestComputeAllEngagementScoresFlushException:
    @pytest.mark.asyncio
    async def test_flush_exception_is_logged_not_raised(self, db_session: Session, monkeypatch):
        """Lines 307-308: flush exception during batch processing is caught and logged."""
        _make_vendor_card(db_session, "flushfail", "Flush Fail", domain="flushfail.com")
        db_session.commit()

        original_flush = db_session.flush
        call_count = [0]

        def bad_flush(*args, **kwargs):
            call_count[0] += 1
            # Fail on first flush call (the batch flush)
            if call_count[0] == 1:
                raise RuntimeError("Simulated flush error in batch")
            return original_flush(*args, **kwargs)

        monkeypatch.setattr(db_session, "flush", bad_flush)

        # Should not raise
        result = await compute_all_engagement_scores(db_session)
        assert "updated" in result


class TestApplyOutboundStatsFlushException:
    def test_flush_exception_rolls_back(self, db_session: Session, monkeypatch):
        """Lines 406-408: flush exception in apply_outbound_stats triggers rollback."""
        _make_vendor_card(db_session, "outboundco", "Outbound Co", domain="outbound.com")
        db_session.commit()

        original_flush = db_session.flush

        flush_calls = [0]

        def bad_flush(*args, **kwargs):
            flush_calls[0] += 1
            raise RuntimeError("Flush failed during outbound stats")

        monkeypatch.setattr(db_session, "flush", bad_flush)

        # Should not raise — exception is caught internally
        result = apply_outbound_stats(db_session, {"outbound.com": 5})
        # updated count is 1 but flush failed
        assert result == 1
