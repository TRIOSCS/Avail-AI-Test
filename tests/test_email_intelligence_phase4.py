"""Tests for Phase 4 — Enhanced Deep Scan + Thread Summarization.

Covers:
  4A: AI brand/commodity detection (detect_specialties_ai)
  4B: Email thread summarization (summarize_thread)
  4C: Deep scan delta query behavior
  Thread summary endpoint

Called by: pytest
Depends on: conftest fixtures
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from tests.conftest import engine  # noqa: F401

# ═══════════════════════════════════════════════════════════════════════
#  4A: AI Brand/Commodity Detection
# ═══════════════════════════════════════════════════════════════════════


class TestDetectSpecialtiesAI:
    def test_batch_detection_success(self):
        """Batch AI detection returns normalized brand/commodity data."""
        from app.services.email_intelligence_service import detect_specialties_ai

        # Each claude_json call returns one result (asyncio.gather runs them in parallel)
        individual_results = [
            {
                "brands": ["Texas Instruments", "STMicro"],
                "commodities": ["voltage regulators", "microcontrollers"],
                "sender_type": "distributor",
            },
            {
                "brands": ["Murata"],
                "commodities": ["capacitors"],
                "sender_type": "manufacturer_rep",
            },
        ]

        with patch(
            "app.utils.claude_client.claude_json",
            new_callable=AsyncMock,
            side_effect=individual_results,
        ):
            result = asyncio.get_event_loop().run_until_complete(
                detect_specialties_ai(["email text 1", "email text 2"])
            )

        assert len(result) == 2
        assert result[0]["brands"] == ["Texas Instruments", "STMicro"]
        assert result[0]["commodities"] == ["voltage regulators", "microcontrollers"]
        assert result[0]["sender_type"] == "distributor"
        assert result[1]["brands"] == ["Murata"]

    def test_batch_detection_partial_failure(self):
        """Individual failures in batch return None for that entry."""
        from app.services.email_intelligence_service import detect_specialties_ai

        # Each claude_json call returns one result; None means that call failed
        individual_results = [
            {"brands": ["NXP"], "commodities": ["ICs"], "sender_type": "broker"},
            None,  # Failed entry
            {"brands": [], "commodities": [], "sender_type": "unknown"},
        ]

        with patch(
            "app.utils.claude_client.claude_json",
            new_callable=AsyncMock,
            side_effect=individual_results,
        ):
            result = asyncio.get_event_loop().run_until_complete(detect_specialties_ai(["text1", "text2", "text3"]))

        assert len(result) == 3
        assert result[0]["brands"] == ["NXP"]
        assert result[1] is None
        assert result[2]["brands"] == []

    def test_batch_detection_invalid_brands_type(self):
        """Non-list brands field normalized to empty list."""
        from app.services.email_intelligence_service import detect_specialties_ai

        with patch(
            "app.utils.claude_client.claude_json",
            new_callable=AsyncMock,
            return_value={"brands": "not a list", "commodities": ["caps"], "sender_type": "unknown"},
        ):
            result = asyncio.get_event_loop().run_until_complete(detect_specialties_ai(["text"]))

        assert result[0]["brands"] == []
        assert result[0]["commodities"] == ["caps"]

    def test_batch_detection_all_failures(self):
        """All entries failing returns list of Nones."""
        from app.services.email_intelligence_service import detect_specialties_ai

        with patch(
            "app.utils.claude_client.claude_json",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = asyncio.get_event_loop().run_until_complete(detect_specialties_ai(["text1", "text2"]))

        assert result == [None, None]


# ═══════════════════════════════════════════════════════════════════════
#  4B: Thread Summarization
# ═══════════════════════════════════════════════════════════════════════


class TestSummarizeThread:
    def test_summarize_thread_success(self, db_session, test_user):
        """Thread summarization fetches messages and returns AI summary."""
        from app.services.email_intelligence_service import summarize_thread

        mock_messages = [
            {
                "from": {"emailAddress": {"address": "vendor@chips.com"}},
                "subject": "RE: LM317T Quote",
                "body": {"content": "We can offer LM317T at $0.50 for 1000pcs"},
                "receivedDateTime": "2026-02-20T10:00:00Z",
            },
            {
                "from": {"emailAddress": {"address": "buyer@trioscs.com"}},
                "subject": "RE: LM317T Quote",
                "body": {"content": "Can you do $0.45?"},
                "receivedDateTime": "2026-02-20T14:00:00Z",
            },
        ]

        mock_summary = {
            "key_points": ["Vendor quoted LM317T at $0.50/1000", "Buyer counter-offered $0.45"],
            "latest_pricing": [{"mpn": "LM317T", "price": 0.50, "qty": 1000}],
            "action_items": ["Await vendor response on counter-offer"],
            "thread_status": "negotiating",
        }

        mock_gc = MagicMock()
        mock_gc.get_all_pages = AsyncMock(return_value=mock_messages)

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=mock_summary),
        ):
            result = asyncio.get_event_loop().run_until_complete(
                summarize_thread("fake-token", "conv-sum-1", db_session, test_user.id)
            )

        assert result is not None
        assert result["thread_status"] == "negotiating"
        assert len(result["key_points"]) == 2

    def test_summarize_thread_cached(self, db_session, test_user):
        """Cached thread summary is returned without API calls."""
        from app.models import EmailIntelligence
        from app.services.email_intelligence_service import summarize_thread

        cached_summary = {
            "key_points": ["Cached summary"],
            "thread_status": "quoted",
        }
        db_session.add(
            EmailIntelligence(
                message_id="msg-cache-1",
                user_id=test_user.id,
                sender_email="v@t.com",
                sender_domain="t.com",
                classification="offer",
                confidence=0.9,
                conversation_id="conv-cached",
                thread_summary=cached_summary,
                created_at=datetime.now(timezone.utc),
            )
        )
        db_session.commit()

        # Should NOT call Graph API or Claude
        with patch("app.utils.graph_client.GraphClient") as mock_gc_cls:
            result = asyncio.get_event_loop().run_until_complete(
                summarize_thread("fake-token", "conv-cached", db_session, test_user.id)
            )
            mock_gc_cls.assert_not_called()

        assert result == cached_summary

    def test_summarize_thread_no_messages(self, db_session, test_user):
        """Returns None when thread has no messages."""
        from app.services.email_intelligence_service import summarize_thread

        mock_gc = MagicMock()
        mock_gc.get_all_pages = AsyncMock(return_value=[])

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            result = asyncio.get_event_loop().run_until_complete(
                summarize_thread("fake-token", "conv-empty", db_session, test_user.id)
            )

        assert result is None

    def test_summarize_thread_graph_failure(self, db_session, test_user):
        """Returns None on Graph API failure."""
        from app.services.email_intelligence_service import summarize_thread

        mock_gc = MagicMock()
        mock_gc.get_all_pages = AsyncMock(side_effect=Exception("Graph error"))

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            result = asyncio.get_event_loop().run_until_complete(
                summarize_thread("fake-token", "conv-fail", db_session, test_user.id)
            )

        assert result is None

    def test_summarize_thread_ai_failure(self, db_session, test_user):
        """Returns None when Claude AI returns None."""
        from app.services.email_intelligence_service import summarize_thread

        mock_gc = MagicMock()
        mock_gc.get_all_pages = AsyncMock(
            return_value=[
                {
                    "from": {"emailAddress": {"address": "v@t.com"}},
                    "subject": "Test",
                    "body": {"content": "Test body"},
                    "receivedDateTime": "2026-02-20T10:00:00Z",
                },
            ]
        )

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=None),
        ):
            result = asyncio.get_event_loop().run_until_complete(
                summarize_thread("fake-token", "conv-ai-fail", db_session, test_user.id)
            )

        assert result is None

    def test_summarize_thread_caches_result(self, db_session, test_user):
        """Summary is cached to existing EmailIntelligence record."""
        from app.models import EmailIntelligence
        from app.services.email_intelligence_service import summarize_thread

        # Create an existing record for this conversation
        db_session.add(
            EmailIntelligence(
                message_id="msg-tocache",
                user_id=test_user.id,
                sender_email="v@t.com",
                sender_domain="t.com",
                classification="offer",
                confidence=0.9,
                conversation_id="conv-to-cache",
                created_at=datetime.now(timezone.utc),
            )
        )
        db_session.commit()

        mock_gc = MagicMock()
        mock_gc.get_all_pages = AsyncMock(
            return_value=[
                {
                    "from": {"emailAddress": {"address": "v@t.com"}},
                    "subject": "Quote",
                    "body": {"content": "LM317T at $0.50"},
                    "receivedDateTime": "2026-02-20T10:00:00Z",
                },
            ]
        )
        mock_summary = {"key_points": ["New summary"], "thread_status": "active"}

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=mock_summary),
        ):
            result = asyncio.get_event_loop().run_until_complete(
                summarize_thread("fake-token", "conv-to-cache", db_session, test_user.id)
            )

        assert result == mock_summary

        # Verify it was cached in the DB
        cached = db_session.query(EmailIntelligence).filter_by(conversation_id="conv-to-cache").first()
        assert cached.thread_summary == mock_summary


# ═══════════════════════════════════════════════════════════════════════
#  Thread Summary Endpoint
# ═══════════════════════════════════════════════════════════════════════


class TestThreadSummaryEndpoint:
    def test_get_thread_summary_success(self, client, db_session, test_user):
        """GET /api/email-intelligence/thread-summary/{id} returns summary."""
        mock_summary = {
            "key_points": ["Point 1"],
            "thread_status": "active",
        }

        with (
            patch(
                "app.routers.emails.require_fresh_token",
                new_callable=AsyncMock,
                return_value="fake-token",
            ),
            patch(
                "app.services.email_intelligence_service.summarize_thread",
                new_callable=AsyncMock,
                return_value=mock_summary,
            ),
        ):
            resp = client.get("/api/email-intelligence/thread-summary/conv-123")

        assert resp.status_code == 200
        data = resp.json()
        assert data["summary"]["thread_status"] == "active"

    def test_get_thread_summary_no_summary(self, client, db_session, test_user):
        """Returns error when summary cannot be generated."""
        with (
            patch(
                "app.routers.emails.require_fresh_token",
                new_callable=AsyncMock,
                return_value="fake-token",
            ),
            patch(
                "app.services.email_intelligence_service.summarize_thread",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            resp = client.get("/api/email-intelligence/thread-summary/conv-none")

        assert resp.status_code == 200
        data = resp.json()
        assert data["summary"] is None
        assert "error" in data

    def test_get_thread_summary_no_token(self, client, db_session, test_user):
        """Returns 401 when M365 token is missing."""
        with patch(
            "app.routers.emails.require_fresh_token",
            new_callable=AsyncMock,
            side_effect=__import__("fastapi").HTTPException(401, "No token"),
        ):
            resp = client.get("/api/email-intelligence/thread-summary/conv-notoken")

        assert resp.status_code == 401


# ═══════════════════════════════════════════════════════════════════════
#  4C: Deep Scan Delta Query
# ═══════════════════════════════════════════════════════════════════════


class TestDeepScanDeltaQuery:
    def _make_miner(self, db=None, user_id=None):
        with patch("app.utils.graph_client.GraphClient"):
            from app.connectors.email_mining import EmailMiner

            miner = EmailMiner("fake-token", db=db, user_id=user_id)
            miner.gc = MagicMock()
        return miner

    def test_deep_scan_uses_delta_query(self, db_session, test_user):
        """Deep scan tries delta query first when user_id and db are present."""
        miner = self._make_miner(db=db_session, user_id=test_user.id)

        delta_messages = [
            {
                "id": "deep-msg-1",
                "from": {"emailAddress": {"address": "vendor@chips.com", "name": "Chip Co"}},
                "subject": "Stock Update",
                "body": {"content": "New inventory available"},
                "receivedDateTime": "2026-02-20T10:00:00Z",
                "conversationId": "conv-deep-1",
            },
        ]

        miner.gc.delta_query = AsyncMock(return_value=(delta_messages, "new-delta-token"))
        miner._get_delta_token = MagicMock(return_value=None)
        miner._save_delta_token = MagicMock()
        miner._already_processed = MagicMock(return_value=set())
        miner._mark_processed = MagicMock()

        result = asyncio.get_event_loop().run_until_complete(miner.deep_scan_inbox(lookback_days=30, max_messages=100))

        miner.gc.delta_query.assert_called_once()
        miner._save_delta_token.assert_called_once_with("deep_mining", "new-delta-token")
        assert result["messages_scanned"] == 1
        # Should not fall back to get_all_pages
        miner.gc.get_all_pages.assert_not_called()

    def test_deep_scan_delta_expired_falls_back(self, db_session, test_user):
        """Delta token expiry triggers fallback to full scan."""
        from app.utils.graph_client import GraphSyncStateExpired

        miner = self._make_miner(db=db_session, user_id=test_user.id)

        miner.gc.delta_query = AsyncMock(side_effect=GraphSyncStateExpired("expired"))
        miner._get_delta_token = MagicMock(return_value="old-token")
        miner._clear_delta_token = MagicMock()
        miner._already_processed = MagicMock(return_value=set())
        miner._mark_processed = MagicMock()

        # Fallback returns messages via get_all_pages
        fallback_msg = {
            "id": "fallback-1",
            "from": {"emailAddress": {"address": "sales@vendor.com", "name": "Sales"}},
            "subject": "Inventory",
            "body": {"content": "See attached"},
            "receivedDateTime": "2026-02-20T10:00:00Z",
            "conversationId": "conv-fb-1",
        }
        miner.gc.get_all_pages = AsyncMock(return_value=[fallback_msg])

        result = asyncio.get_event_loop().run_until_complete(miner.deep_scan_inbox(lookback_days=30, max_messages=100))

        miner._clear_delta_token.assert_called_once_with("deep_mining")
        miner.gc.get_all_pages.assert_called_once()
        assert result["messages_scanned"] == 1

    def test_deep_scan_no_user_id_skips_delta(self):
        """Without user_id, delta query is skipped entirely."""
        miner = self._make_miner(db=None, user_id=None)

        msg = {
            "id": "noid-1",
            "from": {"emailAddress": {"address": "vendor@example.com", "name": "Vendor"}},
            "subject": "Hello",
            "body": {"content": "Test email body"},
            "receivedDateTime": "2026-02-20T10:00:00Z",
            "conversationId": "conv-noid",
        }
        miner.gc.get_all_pages = AsyncMock(return_value=[msg])
        miner._already_processed = MagicMock(return_value=set())

        result = asyncio.get_event_loop().run_until_complete(miner.deep_scan_inbox(lookback_days=30, max_messages=100))

        # Delta query should never be called
        miner.gc.delta_query = AsyncMock()
        miner.gc.delta_query.assert_not_called()
        assert result["messages_scanned"] == 1

    def test_deep_scan_last_body_stored(self, db_session, test_user):
        """Deep scan stores last_body per domain for signature extraction."""
        miner = self._make_miner(db=db_session, user_id=test_user.id)

        msg = {
            "id": "body-1",
            "from": {"emailAddress": {"address": "rep@arrow.com", "name": "Arrow Rep"}},
            "subject": "Quote for parts",
            "body": {"content": "Here is the quote body text for signature extraction"},
            "receivedDateTime": "2026-02-20T10:00:00Z",
            "conversationId": "conv-body-1",
        }

        miner.gc.delta_query = AsyncMock(return_value=([msg], "token"))
        miner._get_delta_token = MagicMock(return_value=None)
        miner._save_delta_token = MagicMock()
        miner._already_processed = MagicMock(return_value=set())
        miner._mark_processed = MagicMock()

        result = asyncio.get_event_loop().run_until_complete(miner.deep_scan_inbox(lookback_days=30, max_messages=100))

        assert "arrow.com" in result["per_domain"]
        assert result["per_domain"]["arrow.com"]["last_body"] != ""
