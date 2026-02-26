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

from app.models import Contact, Sighting, VendorCard, VendorContact
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

    def test_strips_ref_token_from_subject(self):
        messages = [
            {
                "subject": "RFQ for LM317T [ref:42]",
                "from": {"emailAddress": {"address": "vendor@arrow.com"}},
                "toRecipients": [],
                "bodyPreview": "Quote attached",
                "receivedDateTime": "2025-01-15T10:00:00Z",
            }
        ]
        summary = _build_thread_summary("conv-456", messages, "subject_token")
        assert "[ref:42]" not in summary["subject"]
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

    @pytest.mark.asyncio
    async def test_vendor_contacts_collect_domains(self, db_session, test_user, test_vendor_card):
        """VendorContacts contribute their email domains to the search."""
        test_vendor_card.domain = None
        test_vendor_card.emails = []
        db_session.flush()

        # Add a vendor contact with a non-generic domain
        vc = VendorContact(
            vendor_card_id=test_vendor_card.id,
            full_name="Arrow Rep",
            email="rep@arrowelectronics.com",
            source="manual",
        )
        db_session.add(vc)
        db_session.commit()

        mock_messages = [
            {
                "id": "msg-vc1",
                "subject": "Stock update",
                "from": {"emailAddress": {"address": "rep@arrowelectronics.com"}},
                "toRecipients": [{"emailAddress": {"address": "buyer@trioscs.com"}}],
                "bodyPreview": "Updated list",
                "receivedDateTime": "2025-01-20T10:00:00Z",
                "conversationId": "conv-vc-domain",
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
    async def test_vendor_domain_aliases_included(self, db_session, test_user, test_vendor_card):
        """domain_aliases on VendorCard are included in the search."""
        test_vendor_card.domain = "arrow.com"
        test_vendor_card.domain_aliases = ["arrowglobal.com"]
        test_vendor_card.emails = []
        db_session.commit()

        mock_messages = [
            {
                "id": "msg-alias",
                "subject": "Quote",
                "from": {"emailAddress": {"address": "sales@arrowglobal.com"}},
                "toRecipients": [{"emailAddress": {"address": "buyer@trioscs.com"}}],
                "bodyPreview": "Quote attached",
                "receivedDateTime": "2025-02-01T10:00:00Z",
                "conversationId": "conv-alias-1",
            },
        ]

        with patch("app.services.email_threads.GraphClient") as MockGC:
            instance = MockGC.return_value
            instance.get_all_pages = AsyncMock(return_value=mock_messages)

            threads = await fetch_threads_for_vendor(
                test_vendor_card.id, "fake-token", db_session, user_id=test_user.id
            )

        # Should search both arrow.com and arrowglobal.com
        assert MockGC.return_value.get_all_pages.call_count >= 2

    @pytest.mark.asyncio
    async def test_cache_hit_skips_graph(self, db_session, test_user, test_vendor_card):
        """Second call returns cached data without hitting Graph."""
        test_vendor_card.domain = "arrow.com"
        db_session.commit()

        mock_messages = [
            {
                "id": "msg-cache",
                "subject": "Cache test",
                "from": {"emailAddress": {"address": "sales@arrow.com"}},
                "toRecipients": [{"emailAddress": {"address": "buyer@trioscs.com"}}],
                "bodyPreview": "data",
                "receivedDateTime": "2025-01-20T10:00:00Z",
                "conversationId": "conv-cache-1",
            },
        ]

        with patch("app.services.email_threads.GraphClient") as MockGC:
            instance = MockGC.return_value
            instance.get_all_pages = AsyncMock(return_value=mock_messages)

            # First call populates cache
            await fetch_threads_for_vendor(
                test_vendor_card.id, "fake-token", db_session, user_id=test_user.id
            )
            first_call_count = MockGC.call_count

            # Second call should use cache
            await fetch_threads_for_vendor(
                test_vendor_card.id, "fake-token", db_session, user_id=test_user.id
            )

        assert MockGC.call_count == first_call_count  # No additional GraphClient creation

    @pytest.mark.asyncio
    async def test_generic_domains_filtered(self, db_session, test_user, test_vendor_card):
        """Generic domains (gmail.com etc) are filtered from vendor search."""
        test_vendor_card.domain = None
        test_vendor_card.emails = ["vendor@gmail.com", "vendor@yahoo.com"]
        db_session.commit()

        threads = await fetch_threads_for_vendor(
            test_vendor_card.id, "fake-token", db_session, user_id=test_user.id
        )
        # Should return empty since only generic domains
        assert threads == []


# ═══════════════════════════════════════════════════════════════════════
#  Tier 2-4 matching in fetch_threads_for_requirement
# ═══════════════════════════════════════════════════════════════════════


class TestFetchThreadsForRequirementTiers:
    """Extended tier matching tests."""

    def setup_method(self):
        clear_cache()

    @pytest.mark.asyncio
    async def test_tier2_subject_token_match(self, db_session, test_user, test_requisition):
        """Tier 2: Match via [AVAIL-{req_id}] in email subject."""
        requirement = test_requisition.requirements[0]
        req_id = test_requisition.id

        mock_messages = [
            {
                "id": "msg-t2",
                "subject": f"[AVAIL-{req_id}] RFQ for LM317T",
                "from": {"emailAddress": {"address": "vendor@arrow.com"}},
                "toRecipients": [{"emailAddress": {"address": "buyer@trioscs.com"}}],
                "bodyPreview": "Quote attached",
                "receivedDateTime": "2025-01-16T10:00:00Z",
                "conversationId": "conv-tier2",
            },
        ]

        with patch("app.services.email_threads.GraphClient") as MockGC:
            instance = MockGC.return_value
            instance.get_all_pages = AsyncMock(return_value=mock_messages)

            threads = await fetch_threads_for_requirement(
                requirement.id, "fake-token", db_session, user_id=test_user.id
            )

        subject_threads = [t for t in threads if t.get("matched_via") == "subject_token"]
        assert len(subject_threads) >= 1

    @pytest.mark.asyncio
    async def test_tier3_part_number_match(self, db_session, test_user, test_requisition):
        """Tier 3: Match via part number in email subject/body."""
        requirement = test_requisition.requirements[0]

        # Tier 1+2 return empty; Tier 3 finds by part number
        call_count = 0

        async def mock_get_all_pages(path, params=None, max_items=50):
            nonlocal call_count
            call_count += 1
            # First calls are tier 1/2 (conversation_id, subject token) → empty
            if "$search" in (params or {}) and "LM317T" in params.get("$search", ""):
                return [
                    {
                        "id": "msg-t3",
                        "subject": "Stock: LM317T available",
                        "from": {"emailAddress": {"address": "sales@mouser.com"}},
                        "toRecipients": [{"emailAddress": {"address": "buyer@trioscs.com"}}],
                        "bodyPreview": "We have LM317T in stock",
                        "receivedDateTime": "2025-01-17T10:00:00Z",
                        "conversationId": "conv-tier3",
                    },
                ]
            return []

        with patch("app.services.email_threads.GraphClient") as MockGC:
            instance = MockGC.return_value
            instance.get_all_pages = AsyncMock(side_effect=mock_get_all_pages)

            threads = await fetch_threads_for_requirement(
                requirement.id, "fake-token", db_session, user_id=test_user.id
            )

        pn_threads = [t for t in threads if t.get("matched_via") == "part_number"]
        assert len(pn_threads) >= 1

    @pytest.mark.asyncio
    async def test_tier4_vendor_domain_match(self, db_session, test_user, test_requisition):
        """Tier 4: Match via vendor domain from sightings."""
        requirement = test_requisition.requirements[0]

        # Create a sighting with vendor email
        sighting = Sighting(
            requirement_id=requirement.id,
            vendor_name="Arrow",
            vendor_email="sales@arrow.com",
            mpn_matched="LM317T",
            source_type="broker",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(sighting)
        db_session.commit()

        async def mock_get_all_pages(path, params=None, max_items=50):
            search_str = (params or {}).get("$search", "")
            if "arrow.com" in search_str:
                return [
                    {
                        "id": "msg-t4",
                        "subject": "Arrow quote",
                        "from": {"emailAddress": {"address": "rep@arrow.com"}},
                        "toRecipients": [{"emailAddress": {"address": "buyer@trioscs.com"}}],
                        "bodyPreview": "Quote for LM317T",
                        "receivedDateTime": "2025-01-18T10:00:00Z",
                        "conversationId": "conv-tier4",
                    },
                ]
            return []

        with patch("app.services.email_threads.GraphClient") as MockGC:
            instance = MockGC.return_value
            instance.get_all_pages = AsyncMock(side_effect=mock_get_all_pages)

            threads = await fetch_threads_for_requirement(
                requirement.id, "fake-token", db_session, user_id=test_user.id
            )

        domain_threads = [t for t in threads if t.get("matched_via") == "vendor_domain"]
        assert len(domain_threads) >= 1

    @pytest.mark.asyncio
    async def test_combined_tiers_dedup_by_conversation_id(
        self, db_session, test_user, test_requisition
    ):
        """Same conversation_id found by multiple tiers only appears once."""
        requirement = test_requisition.requirements[0]
        req_id = test_requisition.id

        shared_conv = "conv-shared-dedup"

        async def mock_get_all_pages(path, params=None, max_items=50):
            # Both subject token and part number search return same conversation
            return [
                {
                    "id": "msg-dedup",
                    "subject": f"[AVAIL-{req_id}] LM317T quote",
                    "from": {"emailAddress": {"address": "vendor@arrow.com"}},
                    "toRecipients": [{"emailAddress": {"address": "buyer@trioscs.com"}}],
                    "bodyPreview": "dedup test",
                    "receivedDateTime": "2025-01-19T10:00:00Z",
                    "conversationId": shared_conv,
                },
            ]

        with patch("app.services.email_threads.GraphClient") as MockGC:
            instance = MockGC.return_value
            instance.get_all_pages = AsyncMock(side_effect=mock_get_all_pages)

            threads = await fetch_threads_for_requirement(
                requirement.id, "fake-token", db_session, user_id=test_user.id
            )

        # Should only have one thread for this conversation_id
        matching = [t for t in threads if t["conversation_id"] == shared_conv]
        assert len(matching) == 1

    @pytest.mark.asyncio
    async def test_graph_error_graceful_fallthrough(
        self, db_session, test_user, test_requisition
    ):
        """Graph API error at one tier doesn't break other tiers."""
        requirement = test_requisition.requirements[0]

        call_count = 0

        async def mock_get_all_pages(path, params=None, max_items=50):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("Graph API rate limited")
            return []

        with patch("app.services.email_threads.GraphClient") as MockGC:
            instance = MockGC.return_value
            instance.get_all_pages = AsyncMock(side_effect=mock_get_all_pages)

            # Should not raise
            threads = await fetch_threads_for_requirement(
                requirement.id, "fake-token", db_session, user_id=test_user.id
            )

        assert isinstance(threads, list)


# ═══════════════════════════════════════════════════════════════════════
#  _detect_needs_response edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestDetectNeedsResponseEdgeCases:
    def test_invalid_date_format_returns_true(self):
        """Invalid date string in receivedDateTime triggers needs_response=True."""
        messages = [
            {
                "from": {"emailAddress": {"address": "vendor@arrow.com"}},
                "receivedDateTime": "not-a-valid-date",
            }
        ]
        assert _detect_needs_response(messages) is True

    def test_date_with_z_suffix(self):
        """Date with Z suffix is correctly parsed (recent = no response needed)."""
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        messages = [
            {
                "from": {"emailAddress": {"address": "vendor@arrow.com"}},
                "receivedDateTime": now_iso,
            }
        ]
        assert _detect_needs_response(messages) is False

    def test_vendor_message_23h_ago_no_response_needed(self):
        """Message received 23h ago — within 24h window, no response needed."""
        dt = (datetime.now(timezone.utc) - timedelta(hours=23)).isoformat()
        messages = [
            {
                "from": {"emailAddress": {"address": "vendor@arrow.com"}},
                "receivedDateTime": dt,
            }
        ]
        assert _detect_needs_response(messages) is False


# ═══════════════════════════════════════════════════════════════════════
#  _extract_participants edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestExtractParticipantsEdgeCases:
    def test_deduplicates_addresses(self):
        from app.services.email_threads import _extract_participants

        messages = [
            {
                "from": {"emailAddress": {"address": "vendor@arrow.com"}},
                "toRecipients": [{"emailAddress": {"address": "vendor@arrow.com"}}],
            },
            {
                "from": {"emailAddress": {"address": "Vendor@Arrow.COM"}},
                "toRecipients": [],
            },
        ]
        result = _extract_participants(messages)
        # Same email in different cases should be deduped
        assert result.count("vendor@arrow.com") == 1

    def test_trioscs_excluded(self):
        from app.services.email_threads import _extract_participants

        messages = [
            {
                "from": {"emailAddress": {"address": "buyer@trioscs.com"}},
                "toRecipients": [
                    {"emailAddress": {"address": "vendor@arrow.com"}},
                    {"emailAddress": {"address": "manager@trioscs.com"}},
                ],
            },
        ]
        result = _extract_participants(messages)
        assert "vendor@arrow.com" in result
        assert "buyer@trioscs.com" not in result
        assert "manager@trioscs.com" not in result

    def test_empty_address_fields(self):
        from app.services.email_threads import _extract_participants

        messages = [
            {
                "from": {"emailAddress": {"address": ""}},
                "toRecipients": [{"emailAddress": {"address": ""}}],
            },
            {
                "from": {},
                "toRecipients": [{"emailAddress": {}}],
            },
        ]
        result = _extract_participants(messages)
        assert result == []


# ═══════════════════════════════════════════════════════════════════════
#  _message_to_dict edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestMessageToDict:
    def test_full_message(self):
        from app.services.email_threads import _message_to_dict

        msg = {
            "id": "msg-123",
            "from": {"emailAddress": {"address": "vendor@arrow.com", "name": "Arrow Sales"}},
            "toRecipients": [
                {"emailAddress": {"address": "buyer@trioscs.com"}},
                {"emailAddress": {"address": ""}},
            ],
            "subject": "RFQ Reply",
            "bodyPreview": "We have stock",
            "receivedDateTime": "2025-01-15T10:00:00Z",
        }
        result = _message_to_dict(msg)
        assert result["id"] == "msg-123"
        assert result["from_name"] == "Arrow Sales"
        assert result["from_email"] == "vendor@arrow.com"
        assert result["subject"] == "RFQ Reply"
        assert result["direction"] == "received"
        # Empty addresses are filtered out
        assert len(result["to"]) == 1
        assert result["to"][0] == "buyer@trioscs.com"

    def test_minimal_message(self):
        from app.services.email_threads import _message_to_dict

        msg = {}
        result = _message_to_dict(msg)
        assert result["id"] == ""
        assert result["from_name"] == ""
        assert result["from_email"] == ""
        assert result["to"] == []
        assert result["subject"] == ""


# ═══════════════════════════════════════════════════════════════════════
#  fetch_threads_for_vendor — additional edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestFetchThreadsForVendorEdgeCases:
    @pytest.mark.asyncio
    async def test_vendor_not_found(self, db_session, test_user):
        """Non-existent vendor returns empty list."""
        clear_cache()
        result = await fetch_threads_for_vendor(
            99999, "fake-token", db_session, user_id=test_user.id
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_vendor_no_domains(self, db_session, test_user):
        """Vendor with no domains returns empty list."""
        clear_cache()
        card = VendorCard(
            normalized_name="no domains vendor",
            display_name="No Domains Vendor",
            domain=None,
            domain_aliases=None,
            emails=None,
        )
        db_session.add(card)
        db_session.commit()

        result = await fetch_threads_for_vendor(
            card.id, "fake-token", db_session, user_id=test_user.id
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_vendor_with_domains_fetches_threads(self, db_session, test_user):
        """Vendor with domain fetches email threads from Graph."""
        clear_cache()
        card = VendorCard(
            normalized_name="arrow electronics",
            display_name="Arrow Electronics",
            domain="arrow.com",
        )
        db_session.add(card)
        db_session.commit()

        mock_msgs = [
            {
                "id": "msg-v1",
                "subject": "RFQ for LM317T",
                "from": {"emailAddress": {"address": "vendor@arrow.com"}},
                "toRecipients": [{"emailAddress": {"address": "buyer@trioscs.com"}}],
                "bodyPreview": "Here is our quote",
                "receivedDateTime": "2025-01-15T10:00:00Z",
                "conversationId": "conv-vendor-1",
            }
        ]

        with patch("app.services.email_threads.GraphClient") as MockGC:
            instance = MockGC.return_value
            instance.get_all_pages = AsyncMock(return_value=mock_msgs)

            result = await fetch_threads_for_vendor(
                card.id, "fake-token", db_session, user_id=test_user.id
            )

        assert len(result) == 1
        assert result[0]["conversation_id"] == "conv-vendor-1"
        assert result[0]["matched_via"] == "vendor_domain"

    @pytest.mark.asyncio
    async def test_vendor_cache_hit(self, db_session, test_user):
        """Second call for same vendor returns cached result."""
        clear_cache()
        card = VendorCard(
            normalized_name="cached vendor",
            display_name="Cached Vendor",
            domain="cached.com",
        )
        db_session.add(card)
        db_session.commit()

        mock_msgs = [
            {
                "id": "msg-c1",
                "subject": "Quote",
                "from": {"emailAddress": {"address": "rep@cached.com"}},
                "toRecipients": [{"emailAddress": {"address": "buyer@trioscs.com"}}],
                "bodyPreview": "Cached test",
                "receivedDateTime": "2025-01-15T10:00:00Z",
                "conversationId": "conv-cached-1",
            }
        ]

        with patch("app.services.email_threads.GraphClient") as MockGC:
            instance = MockGC.return_value
            instance.get_all_pages = AsyncMock(return_value=mock_msgs)

            result1 = await fetch_threads_for_vendor(
                card.id, "fake-token", db_session, user_id=test_user.id
            )
            # Second call should use cache
            result2 = await fetch_threads_for_vendor(
                card.id, "fake-token", db_session, user_id=test_user.id
            )

        assert result1 == result2
        # Graph API should only be called once (second call is cached)
        assert instance.get_all_pages.call_count == 1

    @pytest.mark.asyncio
    async def test_vendor_domains_from_contacts_and_emails(self, db_session, test_user):
        """Domains collected from vendor contacts and vendor.emails list."""
        clear_cache()
        card = VendorCard(
            normalized_name="multi domain vendor",
            display_name="Multi Domain",
            domain=None,
            emails=["sales@domainA.com", "support@domainB.com"],
        )
        db_session.add(card)
        db_session.flush()

        vc = VendorContact(
            vendor_card_id=card.id,
            email="rep@domainC.com",
            full_name="Rep",
            source="manual",
        )
        db_session.add(vc)
        db_session.commit()

        with patch("app.services.email_threads.GraphClient") as MockGC:
            instance = MockGC.return_value
            instance.get_all_pages = AsyncMock(return_value=[])

            result = await fetch_threads_for_vendor(
                card.id, "fake-token", db_session, user_id=test_user.id
            )

        assert result == []
        # At least 3 domain searches should have been made (domainA, domainB, domainC)
        assert instance.get_all_pages.call_count >= 3


# ═══════════════════════════════════════════════════════════════════════
#  fetch_thread_messages — additional edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestFetchThreadMessagesEdgeCases:
    @pytest.mark.asyncio
    async def test_fetch_messages_success(self):
        """Fetches and converts messages, filtering internal ones."""
        mock_msgs = [
            {
                "id": "msg-1",
                "subject": "RFQ",
                "from": {"emailAddress": {"address": "vendor@arrow.com", "name": "Vendor"}},
                "toRecipients": [{"emailAddress": {"address": "buyer@trioscs.com"}}],
                "bodyPreview": "Quote attached",
                "receivedDateTime": "2025-01-15T10:00:00Z",
            },
            {
                "id": "msg-2",
                "subject": "Internal FYI",
                "from": {"emailAddress": {"address": "manager@trioscs.com"}},
                "toRecipients": [{"emailAddress": {"address": "buyer@trioscs.com"}}],
                "bodyPreview": "Internal note",
                "receivedDateTime": "2025-01-15T11:00:00Z",
            },
        ]

        with patch("app.services.email_threads.GraphClient") as MockGC:
            instance = MockGC.return_value
            instance.get_all_pages = AsyncMock(return_value=mock_msgs)

            result = await fetch_thread_messages("conv-123", "fake-token")

        # Internal message (msg-2) should be filtered out
        assert len(result) == 1
        assert result[0]["from_email"] == "vendor@arrow.com"
        assert result[0]["direction"] == "received"

    @pytest.mark.asyncio
    async def test_fetch_messages_graph_error(self):
        """Graph error returns empty list."""
        with patch("app.services.email_threads.GraphClient") as MockGC:
            instance = MockGC.return_value
            instance.get_all_pages = AsyncMock(side_effect=Exception("Graph down"))

            result = await fetch_thread_messages("conv-err", "fake-token")

        assert result == []
