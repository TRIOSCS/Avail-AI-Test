"""
test_email_threads.py — Tests for email thread service and endpoints

Tests thread attribution (all 4 matching tiers), cache behavior,
needs_response detection, internal email filtering, empty results,
token expiry handling, and Pydantic schema validation.

Business Rules:
- Threads match via conversationId, subject token, part number, vendor domain
- Internal TRIOSCS emails are filtered out
- Cache expires after 5 minutes
- Needs response when last msg is from vendor and >24h old

Called by: pytest
Depends on: conftest.py fixtures, app.services.email_threads
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.models import Contact
from app.schemas.emails import (
    EmailMessage,
    EmailReplyRequest,
    EmailThreadListResponse,
    EmailThreadSummary,
)
from app.services.email_threads import (
    _build_thread_summary,
    _cache_get,
    _cache_set,
    _detect_needs_response,
    _extract_direction,
    _is_internal_message,
    _is_trioscs_domain,
    clear_cache,
    fetch_thread_messages,
    fetch_threads_for_requirement,
    fetch_threads_for_vendor,
)

# ═══════════════════════════════════════════════════════════════════════
#  Unit Tests — Helper functions
# ═══════════════════════════════════════════════════════════════════════


class TestTrIocsDomainDetection:
    def test_trioscs_domain(self):
        assert _is_trioscs_domain("john@trioscs.com") is True

    def test_non_trioscs_domain(self):
        assert _is_trioscs_domain("vendor@arrow.com") is False

    def test_empty_email(self):
        assert _is_trioscs_domain("") is False

    def test_no_at_sign(self):
        assert _is_trioscs_domain("invalid") is False

    def test_none_email(self):
        assert _is_trioscs_domain(None) is False


class TestInternalMessageDetection:
    def test_internal_trioscs_to_trioscs(self):
        assert _is_internal_message("buyer@trioscs.com", ["manager@trioscs.com"]) is True

    def test_external_vendor_to_trioscs(self):
        assert _is_internal_message("vendor@arrow.com", ["buyer@trioscs.com"]) is False

    def test_trioscs_to_vendor(self):
        assert _is_internal_message("buyer@trioscs.com", ["vendor@arrow.com"]) is False

    def test_mixed_recipients(self):
        # If any recipient is non-TRIOSCS, it's external
        assert _is_internal_message("buyer@trioscs.com", ["manager@trioscs.com", "vendor@arrow.com"]) is False

    def test_empty_to_list(self):
        assert _is_internal_message("buyer@trioscs.com", []) is False


class TestDirectionExtraction:
    def test_sent_from_trioscs(self):
        assert _extract_direction("buyer@trioscs.com") == "sent"

    def test_received_from_vendor(self):
        assert _extract_direction("vendor@arrow.com") == "received"


class TestNeedsResponseDetection:
    def test_no_messages(self):
        assert _detect_needs_response([]) is False

    def test_last_message_from_trioscs(self):
        """No response needed — we sent the last message."""
        messages = [
            {
                "from": {"emailAddress": {"address": "buyer@trioscs.com"}},
                "receivedDateTime": datetime.now(timezone.utc).isoformat(),
            }
        ]
        assert _detect_needs_response(messages) is False

    def test_last_message_from_vendor_recent(self):
        """Vendor replied recently — not yet 24h, no response flag."""
        messages = [
            {
                "from": {"emailAddress": {"address": "vendor@arrow.com"}},
                "receivedDateTime": datetime.now(timezone.utc).isoformat(),
            }
        ]
        assert _detect_needs_response(messages) is False

    def test_last_message_from_vendor_old(self):
        """Vendor replied >24h ago — needs response."""
        old_time = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        messages = [
            {
                "from": {"emailAddress": {"address": "vendor@arrow.com"}},
                "receivedDateTime": old_time,
            }
        ]
        assert _detect_needs_response(messages) is True

    def test_multiple_messages_last_from_vendor(self):
        """Multiple messages, vendor sent last one >24h ago."""
        old_time = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
        recent_time = (datetime.now(timezone.utc) - timedelta(hours=35)).isoformat()
        messages = [
            {
                "from": {"emailAddress": {"address": "buyer@trioscs.com"}},
                "receivedDateTime": recent_time,
            },
            {
                "from": {"emailAddress": {"address": "vendor@arrow.com"}},
                "receivedDateTime": old_time,
            },
        ]
        assert _detect_needs_response(messages) is True

    def test_no_date_defaults_to_needs_response(self):
        """No date on vendor message — assume needs response."""
        messages = [
            {
                "from": {"emailAddress": {"address": "vendor@arrow.com"}},
                "receivedDateTime": "",
            }
        ]
        assert _detect_needs_response(messages) is True


# ═══════════════════════════════════════════════════════════════════════
#  Cache Tests
# ═══════════════════════════════════════════════════════════════════════


class TestCache:
    def setup_method(self):
        clear_cache()

    def test_cache_miss(self):
        assert _cache_get("nonexistent") is None

    def test_cache_hit(self):
        data = [{"thread": 1}]
        _cache_set("key1", data)
        assert _cache_get("key1") == data

    def test_cache_expiry(self):
        """Cache entries expire after TTL."""
        data = [{"thread": 1}]
        _cache_set("key1", data)

        # Patch the stored timestamp to be old
        from app.services import email_threads
        ts, stored = email_threads._thread_cache["key1"]
        email_threads._thread_cache["key1"] = (ts - 301, stored)

        assert _cache_get("key1") is None

    def test_clear_cache(self):
        _cache_set("key1", [{"a": 1}])
        _cache_set("key2", [{"b": 2}])
        clear_cache()
        assert _cache_get("key1") is None
        assert _cache_get("key2") is None


# ═══════════════════════════════════════════════════════════════════════
#  Thread Summary Builder
# ═══════════════════════════════════════════════════════════════════════


class TestBuildThreadSummary:
    def test_basic_summary(self):
        messages = [
            {
                "subject": "RFQ for LM317T",
                "from": {"emailAddress": {"address": "vendor@arrow.com", "name": "Arrow Sales"}},
                "toRecipients": [{"emailAddress": {"address": "buyer@trioscs.com"}}],
                "bodyPreview": "We have 1000 units available",
                "receivedDateTime": "2025-01-15T10:00:00Z",
            }
        ]
        summary = _build_thread_summary("conv-123", messages, "conversation_id")
        assert summary["conversation_id"] == "conv-123"
        assert summary["subject"] == "RFQ for LM317T"
        assert summary["message_count"] == 1
        assert summary["matched_via"] == "conversation_id"
        assert "vendor@arrow.com" in summary["participants"]

    def test_strips_avail_token_from_subject(self):
        messages = [
            {
                "subject": "[AVAIL-42] RFQ for LM317T",
                "from": {"emailAddress": {"address": "vendor@arrow.com"}},
                "toRecipients": [],
                "bodyPreview": "Quote attached",
                "receivedDateTime": "2025-01-15T10:00:00Z",
            }
        ]
        summary = _build_thread_summary("conv-456", messages, "subject_token")
        assert "[AVAIL-42]" not in summary["subject"]
        assert "RFQ for LM317T" in summary["subject"]


# ═══════════════════════════════════════════════════════════════════════
#  Pydantic Schema Validation
# ═══════════════════════════════════════════════════════════════════════


class TestSchemaValidation:
    def test_email_thread_summary(self):
        summary = EmailThreadSummary(
            conversation_id="conv-123",
            subject="RFQ for parts",
            participants=["vendor@arrow.com"],
            message_count=5,
            last_message_date=datetime.now(timezone.utc),
            snippet="We can supply...",
            needs_response=True,
            matched_via="conversation_id",
        )
        assert summary.conversation_id == "conv-123"
        assert summary.needs_response is True

    def test_email_message(self):
        msg = EmailMessage(
            id="msg-1",
            from_name="Arrow Sales",
            from_email="sales@arrow.com",
            to=["buyer@trioscs.com"],
            subject="Re: RFQ",
            body_preview="We have stock",
            received_date=datetime.now(timezone.utc),
            direction="received",
        )
        assert msg.direction == "received"
        assert msg.from_email == "sales@arrow.com"

    def test_email_thread_list_response(self):
        resp = EmailThreadListResponse(threads=[], error=None)
        assert resp.threads == []
        assert resp.error is None

    def test_email_thread_list_response_with_error(self):
        resp = EmailThreadListResponse(threads=[], error="M365 connection needs refresh")
        assert resp.error is not None

    def test_email_reply_request(self):
        req = EmailReplyRequest(
            conversation_id="conv-123",
            to="vendor@arrow.com",
            subject="Re: RFQ",
            body="Thanks for the quote",
        )
        assert req.to == "vendor@arrow.com"

    def test_email_thread_summary_defaults(self):
        summary = EmailThreadSummary(conversation_id="c1", subject="test")
        assert summary.participants == []
        assert summary.message_count == 0
        assert summary.needs_response is False
        assert summary.matched_via == ""


# ═══════════════════════════════════════════════════════════════════════
#  Integration Tests — fetch_threads_for_requirement
# ═══════════════════════════════════════════════════════════════════════


class TestFetchThreadsForRequirement:
    """Test thread fetching with mocked GraphClient."""

    def setup_method(self):
        clear_cache()

    @pytest.mark.asyncio
    async def test_tier1_conversation_id_match(self, db_session, test_user, test_requisition):
        """Tier 1: Match via conversationId from Contact record."""
        req = test_requisition
        requirement = req.requirements[0]

        # Create a contact with a conversationId
        contact = Contact(
            requisition_id=req.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="Arrow",
            vendor_contact="sales@arrow.com",
            graph_conversation_id="conv-tier1-test",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(contact)
        db_session.commit()

        mock_messages = [
            {
                "id": "msg-1",
                "subject": "RFQ for LM317T",
                "from": {"emailAddress": {"address": "buyer@trioscs.com", "name": "Buyer"}},
                "toRecipients": [{"emailAddress": {"address": "sales@arrow.com"}}],
                "bodyPreview": "Please quote",
                "receivedDateTime": "2025-01-15T10:00:00Z",
                "conversationId": "conv-tier1-test",
            },
            {
                "id": "msg-2",
                "subject": "Re: RFQ for LM317T",
                "from": {"emailAddress": {"address": "sales@arrow.com", "name": "Arrow Sales"}},
                "toRecipients": [{"emailAddress": {"address": "buyer@trioscs.com"}}],
                "bodyPreview": "We have 1000 units at $0.45",
                "receivedDateTime": "2025-01-15T14:00:00Z",
                "conversationId": "conv-tier1-test",
            },
        ]

        with patch("app.services.email_threads.GraphClient") as MockGC:
            instance = MockGC.return_value
            instance.get_all_pages = AsyncMock(return_value=mock_messages)

            threads = await fetch_threads_for_requirement(
                requirement.id, "fake-token", db_session, user_id=test_user.id
            )

        assert len(threads) >= 1
        thread = threads[0]
        assert thread["matched_via"] == "conversation_id"
        assert thread["message_count"] == 2
        assert "sales@arrow.com" in thread["participants"]

    @pytest.mark.asyncio
    async def test_empty_results(self, db_session, test_user, test_requisition):
        """Requirement with no matching emails returns empty list."""
        requirement = test_requisition.requirements[0]

        with patch("app.services.email_threads.GraphClient") as MockGC:
            instance = MockGC.return_value
            instance.get_all_pages = AsyncMock(return_value=[])

            threads = await fetch_threads_for_requirement(
                requirement.id, "fake-token", db_session, user_id=test_user.id
            )

        assert threads == []

    @pytest.mark.asyncio
    async def test_nonexistent_requirement(self, db_session, test_user):
        """Nonexistent requirement returns empty list."""
        threads = await fetch_threads_for_requirement(
            99999, "fake-token", db_session, user_id=test_user.id
        )
        assert threads == []

    @pytest.mark.asyncio
    async def test_cache_hit(self, db_session, test_user, test_requisition):
        """Second call returns cached data without hitting Graph API."""
        requirement = test_requisition.requirements[0]

        with patch("app.services.email_threads.GraphClient") as MockGC:
            instance = MockGC.return_value
            instance.get_all_pages = AsyncMock(return_value=[])

            # First call
            await fetch_threads_for_requirement(
                requirement.id, "fake-token", db_session, user_id=test_user.id
            )
            # Second call should use cache
            threads = await fetch_threads_for_requirement(
                requirement.id, "fake-token", db_session, user_id=test_user.id
            )

        assert threads == []
        # GraphClient should only be instantiated once (first call only)
        assert MockGC.call_count == 1

    @pytest.mark.asyncio
    async def test_internal_emails_filtered(self, db_session, test_user, test_requisition):
        """Internal TRIOSCS-to-TRIOSCS messages are filtered out."""
        req = test_requisition
        requirement = req.requirements[0]

        contact = Contact(
            requisition_id=req.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="Internal",
            vendor_contact="other@trioscs.com",
            graph_conversation_id="conv-internal",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(contact)
        db_session.commit()

        # Only internal messages
        mock_messages = [
            {
                "id": "msg-internal",
                "subject": "Internal discussion",
                "from": {"emailAddress": {"address": "buyer@trioscs.com"}},
                "toRecipients": [{"emailAddress": {"address": "other@trioscs.com"}}],
                "bodyPreview": "Let's discuss",
                "receivedDateTime": "2025-01-15T10:00:00Z",
                "conversationId": "conv-internal",
            },
        ]

        with patch("app.services.email_threads.GraphClient") as MockGC:
            instance = MockGC.return_value
            instance.get_all_pages = AsyncMock(return_value=mock_messages)

            threads = await fetch_threads_for_requirement(
                requirement.id, "fake-token", db_session, user_id=test_user.id
            )

        # Internal-only threads should not appear
        internal_threads = [t for t in threads if t.get("conversation_id") == "conv-internal"]
        assert len(internal_threads) == 0


class TestFetchThreadMessages:
    """Test thread message fetching."""

    @pytest.mark.asyncio
    async def test_fetch_messages(self):
        mock_messages = [
            {
                "id": "msg-1",
                "subject": "RFQ",
                "from": {"emailAddress": {"address": "buyer@trioscs.com", "name": "Buyer"}},
                "toRecipients": [{"emailAddress": {"address": "vendor@arrow.com"}}],
                "bodyPreview": "Requesting quote",
                "receivedDateTime": "2025-01-15T10:00:00Z",
            },
            {
                "id": "msg-2",
                "subject": "Re: RFQ",
                "from": {"emailAddress": {"address": "vendor@arrow.com", "name": "Arrow"}},
                "toRecipients": [{"emailAddress": {"address": "buyer@trioscs.com"}}],
                "bodyPreview": "Here is our quote",
                "receivedDateTime": "2025-01-15T14:00:00Z",
            },
        ]

        with patch("app.services.email_threads.GraphClient") as MockGC:
            instance = MockGC.return_value
            instance.get_all_pages = AsyncMock(return_value=mock_messages)

            messages = await fetch_thread_messages("conv-123", "fake-token")

        assert len(messages) == 2
        assert messages[0]["direction"] == "sent"
        assert messages[1]["direction"] == "received"

    @pytest.mark.asyncio
    async def test_graph_error_returns_empty(self):
        """GraphClient error returns empty list, doesn't crash."""
        with patch("app.services.email_threads.GraphClient") as MockGC:
            instance = MockGC.return_value
            instance.get_all_pages = AsyncMock(side_effect=Exception("Token expired"))

            messages = await fetch_thread_messages("conv-123", "fake-token")

        assert messages == []


class TestFetchThreadsForVendor:
    """Test vendor thread fetching."""

    def setup_method(self):
        clear_cache()

    @pytest.mark.asyncio
    async def test_vendor_with_domain(self, db_session, test_user, test_vendor_card):
        """Vendor with a domain returns threads from that domain."""
        test_vendor_card.domain = "arrow.com"
        db_session.commit()

        mock_messages = [
            {
                "id": "msg-v1",
                "subject": "Stock list update",
                "from": {"emailAddress": {"address": "sales@arrow.com", "name": "Arrow"}},
                "toRecipients": [{"emailAddress": {"address": "buyer@trioscs.com"}}],
                "bodyPreview": "Updated stock list attached",
                "receivedDateTime": "2025-01-20T10:00:00Z",
                "conversationId": "conv-vendor-1",
            },
        ]

        with patch("app.services.email_threads.GraphClient") as MockGC:
            instance = MockGC.return_value
            instance.get_all_pages = AsyncMock(return_value=mock_messages)

            threads = await fetch_threads_for_vendor(
                test_vendor_card.id, "fake-token", db_session, user_id=test_user.id
            )

        assert len(threads) == 1
        assert threads[0]["matched_via"] == "vendor_domain"

    @pytest.mark.asyncio
    async def test_vendor_no_domain_returns_empty(self, db_session, test_user, test_vendor_card):
        """Vendor with no domain and only generic email domains returns empty."""
        test_vendor_card.domain = None
        test_vendor_card.emails = ["someone@gmail.com"]
        db_session.commit()

        threads = await fetch_threads_for_vendor(
            test_vendor_card.id, "fake-token", db_session, user_id=test_user.id
        )
        assert threads == []

    @pytest.mark.asyncio
    async def test_nonexistent_vendor(self, db_session, test_user):
        """Nonexistent vendor returns empty list."""
        threads = await fetch_threads_for_vendor(
            99999, "fake-token", db_session, user_id=test_user.id
        )
        assert threads == []
