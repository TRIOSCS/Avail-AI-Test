"""Tests for medium-sized service files at 0% coverage.

Covers:
  - buyer_leaderboard
  - prospect_discovery_email
  - auto_attribution_service
  - ics_worker/ai_gate
  - strategic_vendor_service
  - auto_dedup_service
  - response_analytics
  - connector_status
  - models/notification
  - management/reenrich

Called by: pytest
Depends on: conftest.py fixtures, in-memory SQLite
"""

import os

os.environ["TESTING"] = "1"

import asyncio
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.orm import Session

from app.models import (
    ActivityLog,
    BuyerLeaderboardSnapshot,
    IcsSearchQueue,
    Offer,
    Requirement,
    Requisition,
    User,
    VendorCard,
)
from app.models.notification import Notification
from app.models.strategic import StrategicVendor

# ═══════════════════════════════════════════════════════════════════════
# 1. Buyer Leaderboard
# ═══════════════════════════════════════════════════════════════════════


class TestBuyerLeaderboard:
    """Tests for app.services.buyer_leaderboard.compute_buyer_leaderboard."""

    def test_compute_empty_buyers(self, db_session: Session):
        """No buyers -> empty result."""
        from app.services.buyer_leaderboard import compute_buyer_leaderboard

        result = compute_buyer_leaderboard(db_session, date(2026, 3, 1))
        assert result["entries"] == 0
        assert result["month"] == "2026-03-01"

    def test_compute_buyer_with_offers(self, db_session: Session, test_user: User):
        """Buyer with offers in the target month gets scored."""
        from app.services.buyer_leaderboard import compute_buyer_leaderboard

        # test_user has role='buyer', create a requisition and offers in March 2026
        req = Requisition(
            name="REQ-LB-001",
            customer_name="Test Co",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        offer = Offer(
            requisition_id=req.id,
            vendor_name="Vendor A",
            mpn="LM317T",
            qty_available=100,
            unit_price=1.0,
            entered_by_id=test_user.id,
            status="active",
            created_at=datetime(2026, 3, 15, tzinfo=timezone.utc),
        )
        db_session.add(offer)
        db_session.commit()

        result = compute_buyer_leaderboard(db_session, date(2026, 3, 1))
        assert result["entries"] == 1

        snap = (
            db_session.query(BuyerLeaderboardSnapshot).filter(BuyerLeaderboardSnapshot.user_id == test_user.id).first()
        )
        assert snap is not None
        assert snap.offers_logged == 1
        assert snap.total_points >= 1  # At least PTS_LOGGED
        assert snap.rank == 1

    def test_compute_december_boundary(self, db_session: Session, test_user: User):
        """December month_end wraps to January of next year."""
        from app.services.buyer_leaderboard import compute_buyer_leaderboard

        result = compute_buyer_leaderboard(db_session, date(2025, 12, 15))
        assert result["month"] == "2025-12-01"

    def test_upsert_existing_snapshot(self, db_session: Session, test_user: User):
        """Re-computing for the same month updates existing snapshot."""
        from app.services.buyer_leaderboard import compute_buyer_leaderboard

        # First compute
        compute_buyer_leaderboard(db_session, date(2026, 3, 1))

        snap = (
            db_session.query(BuyerLeaderboardSnapshot).filter(BuyerLeaderboardSnapshot.user_id == test_user.id).first()
        )
        assert snap is not None

        # Second compute should update, not duplicate
        compute_buyer_leaderboard(db_session, date(2026, 3, 1))
        count = (
            db_session.query(BuyerLeaderboardSnapshot).filter(BuyerLeaderboardSnapshot.user_id == test_user.id).count()
        )
        assert count == 1

    def test_grace_period_offers(self, db_session: Session, test_user: User):
        """Offers from the grace period only count if they advanced (quoted/buyplan)."""
        from app.services.buyer_leaderboard import compute_buyer_leaderboard

        req = Requisition(
            name="REQ-GRACE",
            customer_name="Test Co",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        # Offer in grace period (Feb 24 for March month)
        grace_offer = Offer(
            requisition_id=req.id,
            vendor_name="V1",
            mpn="ABC123",
            qty_available=10,
            unit_price=1.0,
            entered_by_id=test_user.id,
            status="active",
            created_at=datetime(2026, 2, 25, tzinfo=timezone.utc),
        )
        db_session.add(grace_offer)
        db_session.commit()

        # Grace offer NOT in any quote or buy plan -> should not count
        result = compute_buyer_leaderboard(db_session, date(2026, 3, 1))
        snap = (
            db_session.query(BuyerLeaderboardSnapshot).filter(BuyerLeaderboardSnapshot.user_id == test_user.id).first()
        )
        assert snap.offers_logged == 0


# ═══════════════════════════════════════════════════════════════════════
# 2. Prospect Discovery Email
# ═══════════════════════════════════════════════════════════════════════


class TestProspectDiscoveryEmail:
    """Tests for app.services.prospect_discovery_email."""

    def test_normalize_domain_valid(self):
        from app.services.prospect_discovery_email import _normalize_domain

        assert _normalize_domain("user@Example.COM") == "example.com"
        assert _normalize_domain("user@www.example.com") == "example.com"

    def test_normalize_domain_invalid(self):
        from app.services.prospect_discovery_email import _normalize_domain

        assert _normalize_domain("") is None
        assert _normalize_domain("no-at-sign") is None
        assert _normalize_domain(None) is None

    def test_mine_unknown_domains_happy(self, db_session: Session):
        """mine_unknown_domains returns domains with 2+ emails."""
        from app.services.prospect_discovery_email import mine_unknown_domains

        graph_client = AsyncMock()
        graph_client.list_messages = AsyncMock(
            return_value=[
                {
                    "from": {"emailAddress": {"address": "a@newcompany.com", "name": "Alice"}},
                    "receivedDateTime": "2026-03-01",
                },
                {
                    "from": {"emailAddress": {"address": "b@newcompany.com", "name": "Bob"}},
                    "receivedDateTime": "2026-03-02",
                },
                {
                    "from": {"emailAddress": {"address": "c@lonely.com", "name": "Carol"}},
                    "receivedDateTime": "2026-03-03",
                },
            ]
        )

        result = asyncio.get_event_loop().run_until_complete(
            mine_unknown_domains(graph_client, db_session, days_back=90)
        )
        # newcompany.com has 2 emails (passes threshold), lonely.com has 1 (filtered out)
        assert len(result) == 1
        assert result[0]["domain"] == "newcompany.com"
        assert result[0]["email_count"] == 2

    def test_mine_unknown_domains_excludes_freemail(self, db_session: Session):
        """Freemail domains (gmail.com etc.) are excluded."""
        from app.services.prospect_discovery_email import mine_unknown_domains

        graph_client = AsyncMock()
        graph_client.list_messages = AsyncMock(
            return_value=[
                {"from": {"emailAddress": {"address": "x@gmail.com", "name": "X"}}, "receivedDateTime": "2026-03-01"},
                {"from": {"emailAddress": {"address": "y@gmail.com", "name": "Y"}}, "receivedDateTime": "2026-03-02"},
            ]
        )

        result = asyncio.get_event_loop().run_until_complete(
            mine_unknown_domains(graph_client, db_session, days_back=90)
        )
        assert len(result) == 0

    def test_mine_unknown_domains_api_error(self, db_session: Session):
        """Graph API error returns empty list."""
        from app.services.prospect_discovery_email import mine_unknown_domains

        graph_client = AsyncMock()
        graph_client.list_messages = AsyncMock(side_effect=Exception("API down"))

        result = asyncio.get_event_loop().run_until_complete(
            mine_unknown_domains(graph_client, db_session, days_back=90)
        )
        assert result == []

    def test_enrich_email_domains_with_data(self):
        """enrich_email_domains returns ProspectAccountCreate when enrichment
        succeeds."""
        from app.services.prospect_discovery_email import enrich_email_domains

        domains = [{"domain": "newco.com", "email_count": 5, "sample_senders": []}]

        async def mock_enrich(domain):
            return {"name": "NewCo Inc", "industry": "Electronics", "website": "https://newco.com"}

        result = asyncio.get_event_loop().run_until_complete(enrich_email_domains(domains, enrich_fn=mock_enrich))
        assert len(result) == 1
        assert result[0].name == "NewCo Inc"
        assert result[0].discovery_source == "email_history"

    def test_enrich_email_domains_fallback_to_apollo(self):
        """Falls back to Apollo when primary enrichment returns None."""
        from app.services.prospect_discovery_email import enrich_email_domains

        domains = [{"domain": "fallback.com", "email_count": 3, "sample_senders": []}]

        async def fail_enrich(domain):
            return None

        async def apollo_enrich(domain):
            return {"name": "Fallback Corp", "industry": "Tech"}

        result = asyncio.get_event_loop().run_until_complete(
            enrich_email_domains(domains, enrich_fn=fail_enrich, apollo_enrich_fn=apollo_enrich)
        )
        assert len(result) == 1
        assert result[0].name == "Fallback Corp"

    def test_enrich_email_domains_no_enrichment(self):
        """No enrichment data available -> empty list."""
        from app.services.prospect_discovery_email import enrich_email_domains

        domains = [{"domain": "nope.com", "email_count": 2, "sample_senders": []}]
        result = asyncio.get_event_loop().run_until_complete(
            enrich_email_domains(domains, enrich_fn=None, apollo_enrich_fn=None)
        )
        assert len(result) == 0

    def test_run_email_mining_batch_no_domains(self, db_session: Session):
        """Batch returns empty when no unknown domains found."""
        from app.services.prospect_discovery_email import run_email_mining_batch

        graph_client = AsyncMock()
        graph_client.list_messages = AsyncMock(return_value=[])

        result = asyncio.get_event_loop().run_until_complete(
            run_email_mining_batch("batch-1", graph_client, db_session)
        )
        assert result == []


# ═══════════════════════════════════════════════════════════════════════
# 3. Auto Attribution Service
# ═══════════════════════════════════════════════════════════════════════


class TestAutoAttribution:
    """Tests for app.services.auto_attribution_service."""

    def test_no_unmatched_activities(self, db_session: Session):
        """Returns zeroed stats when no unmatched activities exist."""
        from app.services.auto_attribution_service import run_auto_attribution

        result = run_auto_attribution(db_session)
        assert result == {"rule_matched": 0, "ai_matched": 0, "auto_dismissed": 0, "skipped": 0}

    def test_rule_based_email_match(self, db_session: Session, test_user: User):
        """Activity with matching email gets rule-matched."""
        from app.services.auto_attribution_service import run_auto_attribution

        # Create unmatched activity
        act = ActivityLog(
            user_id=test_user.id,
            activity_type="email_received",
            channel="email",
            contact_email="match@example.com",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(act)
        db_session.commit()

        match_result = {"type": "company", "id": 999}

        # Patch at the activity_service level since run_auto_attribution imports from there
        with (
            patch(
                "app.services.activity_service.match_email_to_entity",
                return_value=match_result,
            ),
            patch(
                "app.services.activity_service.attribute_activity",
            ),
            patch(
                "app.services.activity_service.match_phone_to_entity",
                return_value=None,
            ),
            patch(
                "app.services.activity_service.dismiss_activity",
            ),
        ):
            result = run_auto_attribution(db_session)

        assert result["rule_matched"] == 1

    def test_auto_dismiss_old_activities(self, db_session: Session, test_user: User):
        """Activities older than 30 days get auto-dismissed."""
        from app.services.auto_attribution_service import run_auto_attribution

        old_activity = ActivityLog(
            user_id=test_user.id,
            activity_type="email_received",
            channel="email",
            contact_email="old@example.com",
            created_at=datetime.now(timezone.utc) - timedelta(days=45),
        )
        db_session.add(old_activity)
        db_session.commit()

        with (
            patch(
                "app.services.activity_service.match_email_to_entity",
                return_value=None,
            ),
            patch(
                "app.services.activity_service.match_phone_to_entity",
                return_value=None,
            ),
            patch(
                "app.services.activity_service.dismiss_activity",
            ) as mock_dismiss,
            patch(
                "app.services.activity_service.attribute_activity",
            ),
        ):
            result = run_auto_attribution(db_session)

        assert result["auto_dismissed"] == 1

    def test_ai_match_batch_empty(self, db_session: Session):
        """_ai_match_batch with empty list returns empty dict."""
        from app.services.auto_attribution_service import _ai_match_batch

        result = _ai_match_batch([], db_session)
        assert result == {}

    @patch("app.services.auto_attribution_service._call_claude_for_matching")
    def test_ai_match_batch_with_activities(self, mock_claude, db_session: Session, test_user: User):
        """_ai_match_batch calls Claude and returns results."""
        from app.services.auto_attribution_service import _ai_match_batch

        # Create activity
        act = ActivityLog(
            user_id=test_user.id,
            activity_type="email_received",
            channel="email",
            contact_email="test@vendor.com",
            contact_name="Test",
            subject="Quote",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(act)
        db_session.commit()

        mock_claude.return_value = {act.id: {"entity_type": "company", "entity_id": 1, "confidence": 0.9}}

        # Need to patch asyncio.get_running_loop to raise RuntimeError
        # so _ai_match_batch uses asyncio.run()
        with patch("asyncio.get_running_loop", side_effect=RuntimeError):
            with patch(
                "asyncio.run", return_value={act.id: {"entity_type": "company", "entity_id": 1, "confidence": 0.9}}
            ):
                result = _ai_match_batch([act], db_session)

        assert act.id in result

    def test_call_claude_for_matching(self, db_session: Session):
        """_call_claude_for_matching returns parsed result dict."""
        from app.services.auto_attribution_service import _call_claude_for_matching

        activities = [{"id": 1, "email": "a@b.com", "phone": "", "name": "Test", "subject": "Quote"}]
        companies = [{"id": 10, "name": "Acme", "domain": "acme.com"}]
        vendors = [{"id": 20, "name": "Arrow", "domain": "arrow.com"}]

        mock_result = {"matches": [{"activity_id": 1, "entity_type": "company", "entity_id": 10, "confidence": 0.95}]}

        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=mock_result):
            result = asyncio.get_event_loop().run_until_complete(
                _call_claude_for_matching(activities, companies, vendors)
            )

        assert 1 in result
        assert result[1]["entity_type"] == "company"

    def test_call_claude_string_result(self, db_session: Session):
        """_call_claude_for_matching handles JSON string response."""
        import json

        from app.services.auto_attribution_service import _call_claude_for_matching

        activities = [{"id": 1, "email": "a@b.com", "phone": "", "name": "Test", "subject": "Quote"}]

        json_str = json.dumps(
            {"matches": [{"activity_id": 1, "entity_type": "vendor", "entity_id": 5, "confidence": 0.85}]}
        )

        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=json_str):
            result = asyncio.get_event_loop().run_until_complete(_call_claude_for_matching(activities, [], []))

        assert 1 in result

    def test_call_claude_unavailable(self, db_session: Session):
        """_call_claude_for_matching returns empty on ClaudeUnavailableError."""
        from app.services.auto_attribution_service import _call_claude_for_matching
        from app.utils.claude_errors import ClaudeUnavailableError

        activities = [{"id": 1, "email": "a@b.com", "phone": "", "name": "Test", "subject": "Quote"}]

        with patch(
            "app.utils.claude_client.claude_structured",
            new_callable=AsyncMock,
            side_effect=ClaudeUnavailableError("no key"),
        ):
            result = asyncio.get_event_loop().run_until_complete(_call_claude_for_matching(activities, [], []))

        assert result == {}


# ═══════════════════════════════════════════════════════════════════════
# 4. ICS Worker AI Gate
# ═══════════════════════════════════════════════════════════════════════


class TestAiGate:
    """Tests for app.services.ics_worker.ai_gate."""

    def test_classify_parts_batch_empty(self):
        """Empty parts list returns empty list."""
        from app.services.ics_worker.ai_gate import classify_parts_batch

        result = asyncio.get_event_loop().run_until_complete(classify_parts_batch([]))
        assert result == []

    def test_classify_parts_batch_success(self):
        """Successful classification returns list of dicts."""
        from app.services.ics_worker.ai_gate import classify_parts_batch

        parts = [{"mpn": "STM32F407VG", "manufacturer": "STMicro", "description": "MCU"}]

        mock_result = {
            "classifications": [
                {"mpn": "STM32F407VG", "search_ics": True, "commodity": "semiconductor", "reason": "MCU chip"}
            ]
        }

        with patch("app.utils.llm_router.routed_structured", new_callable=AsyncMock, return_value=mock_result):
            result = asyncio.get_event_loop().run_until_complete(classify_parts_batch(parts))

        assert len(result) == 1
        assert result[0]["search_ics"] is True

    def test_classify_parts_batch_api_error(self):
        """API failure returns None."""
        from app.services.ics_worker.ai_gate import classify_parts_batch

        parts = [{"mpn": "TEST123", "manufacturer": "Test", "description": "Part"}]

        with patch("app.utils.llm_router.routed_structured", new_callable=AsyncMock, side_effect=Exception("API down")):
            result = asyncio.get_event_loop().run_until_complete(classify_parts_batch(parts))

        assert result is None

    def test_process_ai_gate_no_pending(self, db_session: Session):
        """No pending items -> returns immediately."""
        from app.services.ics_worker.ai_gate import process_ai_gate

        asyncio.get_event_loop().run_until_complete(process_ai_gate(db_session))
        # No error = success

    def test_process_ai_gate_cached_item(self, db_session: Session, test_user: User):
        """Cached classification skips API call."""
        from app.services.ics_worker.ai_gate import (
            _cache_lock,
            _classification_cache,
            clear_classification_cache,
            process_ai_gate,
        )

        clear_classification_cache()

        # Create a requisition first (FK required)
        req = Requisition(
            name="REQ-GATE",
            customer_name="Test",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        rqmt = Requirement(
            requisition_id=req.id,
            primary_mpn="STM32F407VG",
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(rqmt)
        db_session.flush()

        item = IcsSearchQueue(
            requirement_id=rqmt.id,
            requisition_id=req.id,
            mpn="STM32F407VG",
            normalized_mpn="stm32f407vg",
            manufacturer="STMicro",
            status="pending",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.commit()

        # Pre-populate cache
        with _cache_lock:
            _classification_cache[("stm32f407vg", "stmicro")] = ("semiconductor", "search", "MCU chip")

        asyncio.get_event_loop().run_until_complete(process_ai_gate(db_session))

        db_session.refresh(item)
        assert item.status == "queued"
        assert item.commodity_class == "semiconductor"
        assert "[cached]" in item.gate_reason

        clear_classification_cache()

    def test_process_ai_gate_api_failure_failopen(self, db_session: Session, test_user: User):
        """API failure defaults items to 'queued' (fail-open)."""
        import app.services.ics_worker.ai_gate as ai_gate_mod
        from app.services.ics_worker.ai_gate import clear_classification_cache, process_ai_gate

        clear_classification_cache()
        ai_gate_mod._last_api_failure = 0.0  # Reset cooldown

        req = Requisition(
            name="REQ-FAIL",
            customer_name="Test",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        rqmt = Requirement(
            requisition_id=req.id,
            primary_mpn="FAIL123",
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(rqmt)
        db_session.flush()

        item = IcsSearchQueue(
            requirement_id=rqmt.id,
            requisition_id=req.id,
            mpn="FAIL123",
            normalized_mpn="fail123",
            status="pending",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.commit()

        with patch("app.services.ics_worker.ai_gate.classify_parts_batch", new_callable=AsyncMock, return_value=None):
            asyncio.get_event_loop().run_until_complete(process_ai_gate(db_session))

        db_session.refresh(item)
        assert item.status == "queued"
        assert item.gate_decision == "search"

        # Reset for other tests
        ai_gate_mod._last_api_failure = 0.0

    def test_clear_classification_cache(self):
        """clear_classification_cache empties the cache."""
        from app.services.ics_worker.ai_gate import (
            _cache_lock,
            _classification_cache,
            clear_classification_cache,
        )

        with _cache_lock:
            _classification_cache[("test", "test")] = ("x", "y", "z")

        clear_classification_cache()

        with _cache_lock:
            assert len(_classification_cache) == 0


# ═══════════════════════════════════════════════════════════════════════
# 5. Strategic Vendor Service
# ═══════════════════════════════════════════════════════════════════════


class TestStrategicVendorService:
    """Tests for app.services.strategic_vendor_service."""

    def test_ensure_utc_naive(self):
        from app.services.strategic_vendor_service import _ensure_utc

        naive = datetime(2026, 1, 1)
        result = _ensure_utc(naive)
        assert result.tzinfo == timezone.utc

    def test_ensure_utc_aware(self):
        from app.services.strategic_vendor_service import _ensure_utc

        aware = datetime(2026, 1, 1, tzinfo=timezone.utc)
        result = _ensure_utc(aware)
        assert result is aware

    def test_ensure_utc_none(self):
        from app.services.strategic_vendor_service import _ensure_utc

        assert _ensure_utc(None) is None

    def test_get_my_strategic_empty(self, db_session: Session, test_user: User):
        from app.services.strategic_vendor_service import get_my_strategic

        result = get_my_strategic(db_session, test_user.id)
        assert result == []

    def test_active_count_zero(self, db_session: Session, test_user: User):
        from app.services.strategic_vendor_service import active_count

        assert active_count(db_session, test_user.id) == 0

    def test_claim_vendor_success(self, db_session: Session, test_user: User, test_vendor_card: VendorCard):
        from app.services.strategic_vendor_service import claim_vendor

        record, err = claim_vendor(db_session, test_user.id, test_vendor_card.id)
        assert err is None
        assert record is not None
        assert record.user_id == test_user.id

    def test_claim_vendor_cap_reached(self, db_session: Session, test_user: User):
        from app.services.strategic_vendor_service import MAX_STRATEGIC_VENDORS, claim_vendor

        # Create 10 vendor cards and claim them
        for i in range(MAX_STRATEGIC_VENDORS):
            vc = VendorCard(
                normalized_name=f"vendor_{i}",
                display_name=f"Vendor {i}",
                sighting_count=1,
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(vc)
            db_session.flush()
            claim_vendor(db_session, test_user.id, vc.id)

        # 11th should fail
        extra = VendorCard(
            normalized_name="vendor_extra",
            display_name="Vendor Extra",
            sighting_count=1,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(extra)
        db_session.flush()

        record, err = claim_vendor(db_session, test_user.id, extra.id)
        assert record is None
        assert "Already at" in err

    def test_claim_vendor_already_claimed_by_self(
        self, db_session: Session, test_user: User, test_vendor_card: VendorCard
    ):
        from app.services.strategic_vendor_service import claim_vendor

        claim_vendor(db_session, test_user.id, test_vendor_card.id)
        record, err = claim_vendor(db_session, test_user.id, test_vendor_card.id)
        assert record is None
        assert "already have" in err

    def test_claim_vendor_not_found(self, db_session: Session, test_user: User):
        from app.services.strategic_vendor_service import claim_vendor

        record, err = claim_vendor(db_session, test_user.id, 99999)
        assert record is None
        assert "not found" in err.lower()

    def test_drop_vendor_success(self, db_session: Session, test_user: User, test_vendor_card: VendorCard):
        from app.services.strategic_vendor_service import claim_vendor, drop_vendor

        claim_vendor(db_session, test_user.id, test_vendor_card.id)
        success, err = drop_vendor(db_session, test_user.id, test_vendor_card.id)
        assert success is True
        assert err is None

    def test_drop_vendor_not_in_list(self, db_session: Session, test_user: User):
        from app.services.strategic_vendor_service import drop_vendor

        success, err = drop_vendor(db_session, test_user.id, 99999)
        assert success is False
        assert "not in your" in err

    def test_replace_vendor_success(self, db_session: Session, test_user: User):
        from app.services.strategic_vendor_service import claim_vendor, replace_vendor

        vc1 = VendorCard(
            normalized_name="rep_a", display_name="Rep A", sighting_count=1, created_at=datetime.now(timezone.utc)
        )
        vc2 = VendorCard(
            normalized_name="rep_b", display_name="Rep B", sighting_count=1, created_at=datetime.now(timezone.utc)
        )
        db_session.add_all([vc1, vc2])
        db_session.flush()

        claim_vendor(db_session, test_user.id, vc1.id)
        record, err = replace_vendor(db_session, test_user.id, vc1.id, vc2.id)
        assert err is None
        assert record is not None
        assert record.vendor_card_id == vc2.id

    def test_replace_vendor_same_id(self, db_session: Session, test_user: User, test_vendor_card: VendorCard):
        from app.services.strategic_vendor_service import replace_vendor

        record, err = replace_vendor(db_session, test_user.id, test_vendor_card.id, test_vendor_card.id)
        assert record is None
        assert "itself" in err

    def test_record_offer_resets_clock(self, db_session: Session, test_user: User, test_vendor_card: VendorCard):
        from app.services.strategic_vendor_service import claim_vendor, record_offer

        claim_vendor(db_session, test_user.id, test_vendor_card.id)
        result = record_offer(db_session, test_vendor_card.id)
        assert result is True

    def test_record_offer_no_strategic(self, db_session: Session):
        from app.services.strategic_vendor_service import record_offer

        assert record_offer(db_session, 99999) is False

    def test_expire_stale(self, db_session: Session, test_user: User, test_vendor_card: VendorCard):
        from app.services.strategic_vendor_service import expire_stale

        # Create an already-expired record
        sv = StrategicVendor(
            user_id=test_user.id,
            vendor_card_id=test_vendor_card.id,
            claimed_at=datetime.now(timezone.utc) - timedelta(days=50),
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        db_session.add(sv)
        db_session.commit()

        count = expire_stale(db_session)
        assert count == 1

        db_session.refresh(sv)
        assert sv.released_at is not None
        assert sv.release_reason == "expired"

    def test_expire_stale_none(self, db_session: Session):
        from app.services.strategic_vendor_service import expire_stale

        assert expire_stale(db_session) == 0

    def test_get_expiring_soon(self, db_session: Session, test_user: User, test_vendor_card: VendorCard):
        from app.services.strategic_vendor_service import get_expiring_soon

        sv = StrategicVendor(
            user_id=test_user.id,
            vendor_card_id=test_vendor_card.id,
            claimed_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(days=3),
        )
        db_session.add(sv)
        db_session.commit()

        result = get_expiring_soon(db_session, days=7)
        assert len(result) == 1

    def test_get_vendor_status(self, db_session: Session, test_user: User, test_vendor_card: VendorCard):
        from app.services.strategic_vendor_service import claim_vendor, get_vendor_status

        claim_vendor(db_session, test_user.id, test_vendor_card.id)
        status = get_vendor_status(db_session, test_vendor_card.id)
        assert status is not None
        assert status["owner_user_id"] == test_user.id
        assert status["days_remaining"] >= 0

    def test_get_vendor_status_none(self, db_session: Session):
        from app.services.strategic_vendor_service import get_vendor_status

        assert get_vendor_status(db_session, 99999) is None

    def test_get_open_pool(self, db_session: Session, test_vendor_card: VendorCard):
        from app.services.strategic_vendor_service import get_open_pool

        vendors, total = get_open_pool(db_session)
        assert total >= 1

    def test_get_open_pool_with_search(self, db_session: Session, test_vendor_card: VendorCard):
        from app.services.strategic_vendor_service import get_open_pool

        vendors, total = get_open_pool(db_session, search="Arrow")
        assert total >= 1

        vendors2, total2 = get_open_pool(db_session, search="NONEXISTENT_XYZZY")
        assert total2 == 0


# ═══════════════════════════════════════════════════════════════════════
# 6. Auto Dedup Service
# ═══════════════════════════════════════════════════════════════════════


class TestAutoDedup:
    """Tests for app.services.auto_dedup_service."""

    def test_run_auto_dedup_empty(self, db_session: Session):
        """Empty database -> zero merges."""
        from app.services.auto_dedup_service import run_auto_dedup

        result = run_auto_dedup(db_session)
        assert result["vendors_merged"] == 0
        assert result["companies_merged"] == 0

    def test_run_coro_sync_no_loop(self):
        """_run_coro_sync with no running loop uses asyncio.run."""
        from app.services.auto_dedup_service import _run_coro_sync

        async def simple_coro():
            return True

        # Ensure no running loop
        with patch("asyncio.get_running_loop", side_effect=RuntimeError):
            with patch("asyncio.run", return_value=True) as mock_run:
                result = _run_coro_sync(simple_coro())
        assert result is True

    def test_run_coro_sync_with_loop(self):
        """_run_coro_sync with active loop uses ThreadPoolExecutor."""
        from app.services.auto_dedup_service import _run_coro_sync

        async def simple_coro():
            return True

        # Simulate active event loop
        with patch("asyncio.get_running_loop", return_value=MagicMock()):
            result = _run_coro_sync(simple_coro())
        assert result is True

    @patch("app.services.auto_dedup_service._dedup_vendors", side_effect=Exception("boom"))
    @patch("app.services.auto_dedup_service._dedup_companies", return_value=0)
    def test_run_auto_dedup_vendor_error(self, mock_companies, mock_vendors, db_session: Session):
        """Vendor dedup failure doesn't prevent company dedup."""
        from app.services.auto_dedup_service import run_auto_dedup

        result = run_auto_dedup(db_session)
        assert result["vendors_merged"] == 0
        assert result["companies_merged"] == 0

    def test_ai_confirm_vendor_merge_success(self):
        """_ai_confirm_vendor_merge returns True when AI confirms."""
        from app.services.auto_dedup_service import _ai_confirm_vendor_merge

        with patch("app.services.auto_dedup_service._run_coro_sync", return_value=True):
            assert _ai_confirm_vendor_merge("Arrow Electronics", "Arrow Electr.", 95) is True

    def test_ai_confirm_vendor_merge_failure(self):
        """_ai_confirm_vendor_merge returns False on error."""
        from app.services.auto_dedup_service import _ai_confirm_vendor_merge

        with patch("app.services.auto_dedup_service._run_coro_sync", side_effect=Exception("fail")):
            assert _ai_confirm_vendor_merge("A", "B", 93) is False

    def test_ai_confirm_company_merge(self):
        """_ai_confirm_company_merge returns bool."""
        from app.services.auto_dedup_service import _ai_confirm_company_merge

        with patch("app.services.auto_dedup_service._run_coro_sync", return_value=False):
            assert _ai_confirm_company_merge("A", "B", "a.com", "b.com", 94) is False

    def test_ask_claude_merge_true(self):
        """_ask_claude_merge returns True when same_entity=True and confidence>=0.85."""
        from app.services.auto_dedup_service import _ask_claude_merge

        with patch(
            "app.utils.claude_client.claude_structured",
            new_callable=AsyncMock,
            return_value={"same_entity": True, "confidence": 0.9},
        ):
            result = asyncio.get_event_loop().run_until_complete(_ask_claude_merge("test"))
        assert result is True

    def test_ask_claude_merge_low_confidence(self):
        """_ask_claude_merge returns False when confidence < 0.85."""
        from app.services.auto_dedup_service import _ask_claude_merge

        with patch(
            "app.utils.claude_client.claude_structured",
            new_callable=AsyncMock,
            return_value={"same_entity": True, "confidence": 0.5},
        ):
            result = asyncio.get_event_loop().run_until_complete(_ask_claude_merge("test"))
        assert result is False

    def test_ask_claude_merge_unavailable(self):
        """_ask_claude_merge returns False on ClaudeUnavailableError."""
        from app.services.auto_dedup_service import _ask_claude_merge
        from app.utils.claude_errors import ClaudeUnavailableError

        with patch(
            "app.utils.claude_client.claude_structured",
            new_callable=AsyncMock,
            side_effect=ClaudeUnavailableError("no key"),
        ):
            result = asyncio.get_event_loop().run_until_complete(_ask_claude_merge("test"))
        assert result is False

    def test_ask_claude_merge_none_result(self):
        """_ask_claude_merge returns False when result is None."""
        from app.services.auto_dedup_service import _ask_claude_merge

        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=None):
            result = asyncio.get_event_loop().run_until_complete(_ask_claude_merge("test"))
        assert result is False


# ═══════════════════════════════════════════════════════════════════════
# 7. Response Analytics
# ═══════════════════════════════════════════════════════════════════════


class TestResponseAnalytics:
    """Tests for app.services.response_analytics."""

    def test_empty_metrics(self):
        from app.services.response_analytics import _empty_metrics

        m = _empty_metrics()
        assert m["avg_response_hours"] is None
        assert m["response_rate"] == 0.0

    def test_compute_vendor_not_found(self, db_session: Session):
        from app.services.response_analytics import compute_vendor_response_metrics

        result = compute_vendor_response_metrics(db_session, 99999)
        assert result["response_count"] == 0
        assert result["avg_response_hours"] is None

    def test_compute_vendor_no_activity(self, db_session: Session, test_vendor_card: VendorCard):
        from app.services.response_analytics import compute_vendor_response_metrics

        result = compute_vendor_response_metrics(db_session, test_vendor_card.id)
        assert result["response_rate"] == 0.0
        assert result["outreach_count"] == 0

    def test_compute_vendor_with_outreach(self, db_session: Session, test_user: User, test_vendor_card: VendorCard):
        """Vendor with outreach activity but no responses has 0% response rate."""
        from app.services.response_analytics import compute_vendor_response_metrics

        act = ActivityLog(
            user_id=test_user.id,
            activity_type="rfq_sent",
            channel="email",
            vendor_card_id=test_vendor_card.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(act)
        db_session.commit()

        result = compute_vendor_response_metrics(db_session, test_vendor_card.id)
        assert result["outreach_count"] == 1
        assert result["response_rate"] == 0.0

    def test_compute_vendor_fallback_total_outreach(self, db_session: Session, test_vendor_card: VendorCard):
        """Falls back to vendor.total_outreach when no activity log entries."""
        from app.services.response_analytics import compute_vendor_response_metrics

        test_vendor_card.total_outreach = 10
        db_session.commit()

        result = compute_vendor_response_metrics(db_session, test_vendor_card.id)
        assert result["outreach_count"] == 10

    def test_email_health_score_no_vendor(self, db_session: Session):
        from app.services.response_analytics import compute_email_health_score

        result = compute_email_health_score(db_session, 99999)
        # Still returns a dict (using _empty_metrics internally)
        assert "email_health_score" in result

    def test_email_health_score_defaults(self, db_session: Session, test_vendor_card: VendorCard):
        """Vendor with no data gets neutral defaults."""
        from app.services.response_analytics import compute_email_health_score

        result = compute_email_health_score(db_session, test_vendor_card.id)
        assert 0.0 <= result["email_health_score"] <= 100.0
        # Response time defaults to 50 (neutral), OOO defaults to 100
        assert result["response_time_score"] == 50.0
        assert result["ooo_score"] == 100.0

    def test_update_vendor_email_health_not_found(self, db_session: Session):
        from app.services.response_analytics import update_vendor_email_health

        assert update_vendor_email_health(db_session, 99999) is None

    def test_update_vendor_email_health_persists(self, db_session: Session, test_vendor_card: VendorCard):
        from app.services.response_analytics import update_vendor_email_health

        result = update_vendor_email_health(db_session, test_vendor_card.id)
        assert result is not None
        db_session.refresh(test_vendor_card)
        assert test_vendor_card.email_health_score is not None

    def test_batch_update_email_health(self, db_session: Session, test_vendor_card: VendorCard):
        from app.services.response_analytics import batch_update_email_health

        # Set last_contact_at so vendor is selected
        test_vendor_card.last_contact_at = datetime.now(timezone.utc)
        db_session.commit()

        result = batch_update_email_health(db_session)
        assert result["updated"] >= 1
        assert result["errors"] == 0

    def test_batch_update_no_vendors(self, db_session: Session):
        from app.services.response_analytics import batch_update_email_health

        result = batch_update_email_health(db_session)
        assert result["updated"] == 0

    def test_get_email_intelligence_dashboard(self, db_session: Session, test_user: User):
        from app.services.response_analytics import get_email_intelligence_dashboard

        result = get_email_intelligence_dashboard(db_session, test_user.id)
        assert result["emails_scanned_7d"] == 0
        assert result["top_vendors"] == []
        assert result["recent_offers"] == []


# ═══════════════════════════════════════════════════════════════════════
# 8. Connector Status
# ═══════════════════════════════════════════════════════════════════════


class TestConnectorStatus:
    """Tests for app.connector_status."""

    def test_log_connector_status_returns_dict(self):
        from app.connector_status import log_connector_status

        result = log_connector_status()
        assert isinstance(result, dict)
        assert "Nexar (Octopart)" in result
        assert "BrokerBin" in result

    def test_log_connector_status_values_are_bools(self):
        from app.connector_status import log_connector_status

        result = log_connector_status()
        for key, val in result.items():
            assert isinstance(val, bool), f"{key} should be bool, got {type(val)}"

    def test_connector_status_all_keys(self):
        """All expected connectors present."""
        from app.connector_status import log_connector_status

        result = log_connector_status()
        expected = {
            "Nexar (Octopart)",
            "BrokerBin",
            "eBay",
            "DigiKey",
            "Mouser",
            "OEMSecrets",
            "Sourcengine",
            "Element14",
            "Anthropic AI",
            "Azure OAuth",
        }
        assert set(result.keys()) == expected


# ═══════════════════════════════════════════════════════════════════════
# 9. Notification Model
# ═══════════════════════════════════════════════════════════════════════


class TestNotificationModel:
    """Tests for app.models.notification.Notification model definition.

    Note: The Notification table is not registered in the test SQLite DB
    (not exported from app.models.__init__), so we test the model class
    attributes and defaults without persisting.
    """

    def test_notification_tablename(self):
        assert Notification.__tablename__ == "notifications"

    def test_notification_columns_exist(self):
        columns = {c.name for c in Notification.__table__.columns}
        expected = {"id", "user_id", "ticket_id", "event_type", "title", "body", "is_read", "created_at"}
        assert expected == columns

    def test_notification_defaults(self):
        n = Notification(user_id=1, event_type="diagnosed", title="Test")
        # Column default=False only applies at DB level; Python-side may be None
        assert n.is_read in (False, None)
        assert n.ticket_id is None
        assert n.body is None

    def test_notification_all_fields(self):
        n = Notification(
            user_id=1,
            ticket_id=42,
            event_type="escalated",
            title="Issue escalated",
            body="Details here",
            is_read=True,
        )
        assert n.user_id == 1
        assert n.ticket_id == 42
        assert n.event_type == "escalated"
        assert n.title == "Issue escalated"
        assert n.body == "Details here"
        assert n.is_read is True


# ═══════════════════════════════════════════════════════════════════════
# 10. Management Re-enrich
# ═══════════════════════════════════════════════════════════════════════


class TestReenrich:
    """Tests for app.management.reenrich."""

    @patch("app.services.material_enrichment_service.enrich_material_cards", new_callable=AsyncMock)
    @patch("app.services.spec_write_service.record_spec")
    def test_main_no_cards(self, mock_record_spec, mock_enrich, db_session: Session):
        """Re-enrich with no cards does nothing."""
        from app.management.reenrich import main

        mock_enrich.return_value = {"enriched": 0}

        with patch("app.database.SessionLocal", return_value=db_session):
            asyncio.get_event_loop().run_until_complete(main(limit=10, batch_size=5))

        mock_enrich.assert_called_once()

    @patch("app.services.material_enrichment_service.enrich_material_cards", new_callable=AsyncMock)
    @patch("app.services.spec_write_service.record_spec")
    def test_main_with_cards(self, mock_record_spec, mock_enrich, db_session: Session, test_material_card):
        """Re-enrich processes available material cards."""
        from app.management.reenrich import main

        mock_enrich.return_value = {"enriched": 1}

        # Set specs_structured and category on the card
        test_material_card.specs_structured = {"voltage": {"value": "3.3V"}}
        test_material_card.category = "semiconductor"
        db_session.commit()

        with patch("app.database.SessionLocal", return_value=db_session):
            asyncio.get_event_loop().run_until_complete(main(limit=10, batch_size=5))

        mock_enrich.assert_called_once()
        # record_spec should be called for the voltage spec
        assert mock_record_spec.call_count >= 1

    @patch("app.services.material_enrichment_service.enrich_material_cards", new_callable=AsyncMock)
    @patch("app.services.spec_write_service.record_spec")
    def test_main_skips_cards_without_specs(
        self, mock_record_spec, mock_enrich, db_session: Session, test_material_card
    ):
        """Cards without specs_structured or category are skipped for facet backfill."""
        from app.management.reenrich import main

        mock_enrich.return_value = {"enriched": 1}
        # specs_structured is None by default

        with patch("app.database.SessionLocal", return_value=db_session):
            asyncio.get_event_loop().run_until_complete(main(limit=10, batch_size=5))

        mock_record_spec.assert_not_called()

    @patch("app.services.material_enrichment_service.enrich_material_cards", new_callable=AsyncMock)
    @patch("app.services.spec_write_service.record_spec")
    def test_main_handles_plain_spec_values(
        self, mock_record_spec, mock_enrich, db_session: Session, test_material_card
    ):
        """Specs with plain values (not dicts) are handled correctly."""
        from app.management.reenrich import main

        mock_enrich.return_value = {"enriched": 1}

        test_material_card.specs_structured = {"voltage": "3.3V"}
        test_material_card.category = "ic"
        db_session.commit()

        with patch("app.database.SessionLocal", return_value=db_session):
            asyncio.get_event_loop().run_until_complete(main(limit=10, batch_size=5))

        # Plain string "3.3V" is not a dict, so spec_data.get("value") won't work
        # The code handles this: value = spec_data if not isinstance(spec_data, dict)
        assert mock_record_spec.call_count >= 1
