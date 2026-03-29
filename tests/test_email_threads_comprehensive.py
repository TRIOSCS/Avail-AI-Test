"""test_email_threads_comprehensive.py — Additional tests for email_threads service.

Covers group_by_thread (union-find threading), VendorResponse tier 1b,
tier 4 vendor card domain lookup, and error edge cases that bring
coverage above 85%.

Called by: pytest
Depends on: conftest.py fixtures, app.services.email_threads
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.models import Contact, Requirement, Requisition, Sighting, VendorCard
from app.services.email_threads import (
    clear_cache,
    fetch_threads_for_requirement,
    fetch_threads_for_vendor,
    group_by_thread,
)

# ═══════════════════════════════════════════════════════════════════════
#  group_by_thread — Union-Find threading via headers
# ═══════════════════════════════════════════════════════════════════════


class TestGroupByThread:
    """Tests for group_by_thread which uses union-find on email headers."""

    def test_empty_messages(self):
        assert group_by_thread([]) == []

    def test_single_message_no_headers(self):
        """Message with no internetMessageHeaders becomes standalone."""
        msgs = [
            {
                "id": "msg-1",
                "subject": "Hello",
                "from": {"emailAddress": {"address": "vendor@arrow.com"}},
                "receivedDateTime": "2025-01-15T10:00:00Z",
            }
        ]
        threads = group_by_thread(msgs)
        assert len(threads) == 1
        assert threads[0]["message_count"] == 1
        assert threads[0]["messages"][0]["id"] == "msg-1"

    def test_single_message_with_message_id(self):
        """Message with Message-ID header is its own thread."""
        msgs = [
            {
                "id": "msg-1",
                "subject": "Test",
                "from": {"emailAddress": {"address": "vendor@arrow.com"}},
                "receivedDateTime": "2025-01-15T10:00:00Z",
                "internetMessageHeaders": [
                    {"name": "Message-ID", "value": "<abc123@mail.com>"},
                ],
            }
        ]
        threads = group_by_thread(msgs)
        assert len(threads) == 1
        assert threads[0]["message_count"] == 1

    def test_two_messages_linked_by_in_reply_to(self):
        """In-Reply-To header links reply to original message in one thread."""
        msgs = [
            {
                "id": "msg-1",
                "subject": "RFQ",
                "from": {"emailAddress": {"address": "buyer@trioscs.com"}},
                "receivedDateTime": "2025-01-15T10:00:00Z",
                "internetMessageHeaders": [
                    {"name": "Message-ID", "value": "<original@mail.com>"},
                ],
            },
            {
                "id": "msg-2",
                "subject": "Re: RFQ",
                "from": {"emailAddress": {"address": "vendor@arrow.com"}},
                "receivedDateTime": "2025-01-15T14:00:00Z",
                "internetMessageHeaders": [
                    {"name": "Message-ID", "value": "<reply@mail.com>"},
                    {"name": "In-Reply-To", "value": "<original@mail.com>"},
                ],
            },
        ]
        threads = group_by_thread(msgs)
        assert len(threads) == 1
        assert threads[0]["message_count"] == 2

    def test_three_messages_linked_by_references(self):
        """References header links 3 messages into one thread."""
        msgs = [
            {
                "id": "msg-1",
                "subject": "RFQ",
                "from": {"emailAddress": {"address": "buyer@trioscs.com"}},
                "receivedDateTime": "2025-01-15T10:00:00Z",
                "internetMessageHeaders": [
                    {"name": "Message-ID", "value": "<msg1@mail.com>"},
                ],
            },
            {
                "id": "msg-2",
                "subject": "Re: RFQ",
                "from": {"emailAddress": {"address": "vendor@arrow.com"}},
                "receivedDateTime": "2025-01-15T14:00:00Z",
                "internetMessageHeaders": [
                    {"name": "Message-ID", "value": "<msg2@mail.com>"},
                    {"name": "In-Reply-To", "value": "<msg1@mail.com>"},
                    {"name": "References", "value": "<msg1@mail.com>"},
                ],
            },
            {
                "id": "msg-3",
                "subject": "Re: Re: RFQ",
                "from": {"emailAddress": {"address": "buyer@trioscs.com"}},
                "receivedDateTime": "2025-01-15T16:00:00Z",
                "internetMessageHeaders": [
                    {"name": "Message-ID", "value": "<msg3@mail.com>"},
                    {"name": "In-Reply-To", "value": "<msg2@mail.com>"},
                    {"name": "References", "value": "<msg1@mail.com> <msg2@mail.com>"},
                ],
            },
        ]
        threads = group_by_thread(msgs)
        assert len(threads) == 1
        assert threads[0]["message_count"] == 3

    def test_two_separate_threads(self):
        """Unrelated messages with no linking headers form separate threads."""
        msgs = [
            {
                "id": "msg-a",
                "subject": "Thread A",
                "from": {"emailAddress": {"address": "vendor@arrow.com"}},
                "receivedDateTime": "2025-01-15T10:00:00Z",
                "internetMessageHeaders": [
                    {"name": "Message-ID", "value": "<a1@mail.com>"},
                ],
            },
            {
                "id": "msg-b",
                "subject": "Thread B",
                "from": {"emailAddress": {"address": "vendor@mouser.com"}},
                "receivedDateTime": "2025-01-15T11:00:00Z",
                "internetMessageHeaders": [
                    {"name": "Message-ID", "value": "<b1@mail.com>"},
                ],
            },
        ]
        threads = group_by_thread(msgs)
        assert len(threads) == 2

    def test_references_without_in_reply_to(self):
        """Multiple References headers (>=2) link messages together."""
        msgs = [
            {
                "id": "msg-1",
                "subject": "Thread",
                "from": {"emailAddress": {"address": "vendor@arrow.com"}},
                "receivedDateTime": "2025-01-15T10:00:00Z",
                "internetMessageHeaders": [
                    {"name": "Message-ID", "value": "<r1@mail.com>"},
                    {"name": "References", "value": "<root@mail.com> <r0@mail.com>"},
                ],
            },
        ]
        threads = group_by_thread(msgs)
        # r1, root, r0 all grouped into one thread
        assert len(threads) == 1

    def test_message_with_empty_message_id(self):
        """Message with empty message-id but has in-reply-to is grouped by parent."""
        msgs = [
            {
                "id": "msg-parent",
                "subject": "Parent",
                "from": {"emailAddress": {"address": "vendor@arrow.com"}},
                "receivedDateTime": "2025-01-15T10:00:00Z",
                "internetMessageHeaders": [
                    {"name": "Message-ID", "value": "<parent@mail.com>"},
                ],
            },
            {
                "id": "msg-child",
                "subject": "Re: Parent",
                "from": {"emailAddress": {"address": "buyer@trioscs.com"}},
                "receivedDateTime": "2025-01-15T14:00:00Z",
                "internetMessageHeaders": [
                    {"name": "Message-ID", "value": ""},
                    {"name": "In-Reply-To", "value": "<parent@mail.com>"},
                ],
            },
        ]
        threads = group_by_thread(msgs)
        # The child with empty msg-id should be grouped via in-reply-to
        # Parent has its own message_id thread; child joins via in_reply_to
        total_msgs = sum(t["message_count"] for t in threads)
        assert total_msgs == 2

    def test_standalone_no_id_no_reply_to(self):
        """Message with no message-id and no in-reply-to is standalone."""
        msgs = [
            {
                "id": "msg-standalone",
                "subject": "Standalone",
                "from": {"emailAddress": {"address": "vendor@arrow.com"}},
                "receivedDateTime": "2025-01-15T10:00:00Z",
                "internetMessageHeaders": [
                    {"name": "Message-ID", "value": ""},
                ],
            },
        ]
        threads = group_by_thread(msgs)
        assert len(threads) == 1
        assert threads[0]["message_count"] == 1

    def test_direction_in_grouped_messages(self):
        """Thread messages contain correct direction based on sender domain."""
        msgs = [
            {
                "id": "msg-1",
                "subject": "RFQ",
                "from": {"emailAddress": {"address": "buyer@trioscs.com"}},
                "receivedDateTime": "2025-01-15T10:00:00Z",
                "internetMessageHeaders": [
                    {"name": "Message-ID", "value": "<x@mail.com>"},
                ],
            },
            {
                "id": "msg-2",
                "subject": "Re: RFQ",
                "from": {"emailAddress": {"address": "vendor@arrow.com"}},
                "receivedDateTime": "2025-01-15T14:00:00Z",
                "internetMessageHeaders": [
                    {"name": "Message-ID", "value": "<y@mail.com>"},
                    {"name": "In-Reply-To", "value": "<x@mail.com>"},
                ],
            },
        ]
        threads = group_by_thread(msgs)
        assert len(threads) == 1
        directions = {m["id"]: m["direction"] for m in threads[0]["messages"]}
        assert directions["msg-1"] == "sent"
        assert directions["msg-2"] == "received"

    def test_sentDateTime_fallback(self):
        """Uses sentDateTime if receivedDateTime is absent."""
        msgs = [
            {
                "id": "msg-1",
                "subject": "Sent only",
                "from": {"emailAddress": {"address": "vendor@arrow.com"}},
                "sentDateTime": "2025-01-15T10:00:00Z",
                "internetMessageHeaders": [
                    {"name": "Message-ID", "value": "<s1@mail.com>"},
                ],
            },
        ]
        threads = group_by_thread(msgs)
        assert threads[0]["messages"][0]["date"] == "2025-01-15T10:00:00Z"

    def test_complex_union_find_path_compression(self):
        """Chain of 4 messages tests union-find path compression."""
        msgs = [
            {
                "id": f"msg-{i}",
                "subject": f"Re: chain {i}",
                "from": {"emailAddress": {"address": "vendor@arrow.com"}},
                "receivedDateTime": f"2025-01-15T{10 + i}:00:00Z",
                "internetMessageHeaders": [
                    {"name": "Message-ID", "value": f"<chain{i}@mail.com>"},
                ]
                + ([{"name": "In-Reply-To", "value": f"<chain{i - 1}@mail.com>"}] if i > 0 else []),
            }
            for i in range(4)
        ]
        threads = group_by_thread(msgs)
        assert len(threads) == 1
        assert threads[0]["message_count"] == 4


# ═══════════════════════════════════════════════════════════════════════
#  fetch_threads_for_requirement — Tier 1b VendorResponse
# ═══════════════════════════════════════════════════════════════════════


class TestFetchThreadsVendorResponseTier:
    """Test Tier 1b: VendorResponse conversationId matching."""

    def setup_method(self):
        clear_cache()

    @pytest.mark.asyncio
    async def test_tier1b_vendor_response_conversation_id(self, db_session, test_user, test_requisition):
        """Tier 1b: Match via VendorResponse graph_conversation_id."""
        from app.models import VendorResponse

        req = test_requisition
        requirement = req.requirements[0]

        # Create a VendorResponse with conversationId
        vr = VendorResponse(
            requisition_id=req.id,
            vendor_name="Arrow",
            graph_conversation_id="conv-vr-test",
        )
        db_session.add(vr)
        db_session.commit()

        mock_messages = [
            {
                "id": "msg-vr1",
                "subject": "Re: RFQ for LM317T",
                "from": {"emailAddress": {"address": "sales@arrow.com", "name": "Arrow"}},
                "toRecipients": [{"emailAddress": {"address": "buyer@trioscs.com"}}],
                "bodyPreview": "Here is our quote",
                "receivedDateTime": "2025-01-15T14:00:00Z",
                "conversationId": "conv-vr-test",
            },
        ]

        with patch("app.services.email_threads.GraphClient") as MockGC:
            instance = MockGC.return_value
            instance.get_all_pages = AsyncMock(return_value=mock_messages)

            threads = await fetch_threads_for_requirement(
                requirement.id, "fake-token", db_session, user_id=test_user.id
            )

        conv_threads = [t for t in threads if t.get("conversation_id") == "conv-vr-test"]
        assert len(conv_threads) == 1
        assert conv_threads[0]["matched_via"] == "conversation_id"

    @pytest.mark.asyncio
    async def test_tier1b_vendor_response_graph_error(self, db_session, test_user, test_requisition):
        """Tier 1b: Graph API error for VendorResponse is handled gracefully."""
        from app.models import VendorResponse

        req = test_requisition
        requirement = req.requirements[0]

        vr = VendorResponse(
            requisition_id=req.id,
            vendor_name="Arrow",
            graph_conversation_id="conv-vr-error",
        )
        db_session.add(vr)
        db_session.commit()

        call_count = 0

        async def mock_failing(path, params=None, max_items=50):
            nonlocal call_count
            call_count += 1
            # Fail on VR conversation lookup
            filter_str = (params or {}).get("$filter", "")
            if "conv-vr-error" in filter_str:
                raise Exception("Graph API timeout")
            return []

        with patch("app.services.email_threads.GraphClient") as MockGC:
            instance = MockGC.return_value
            instance.get_all_pages = AsyncMock(side_effect=mock_failing)

            # Should not raise
            threads = await fetch_threads_for_requirement(
                requirement.id, "fake-token", db_session, user_id=test_user.id
            )

        assert isinstance(threads, list)

    @pytest.mark.asyncio
    async def test_tier1b_vr_internal_only_filtered(self, db_session, test_user, test_requisition):
        """Tier 1b: VendorResponse thread with only internal messages is excluded."""
        from app.models import VendorResponse

        req = test_requisition
        requirement = req.requirements[0]

        vr = VendorResponse(
            requisition_id=req.id,
            vendor_name="Internal",
            graph_conversation_id="conv-vr-internal",
        )
        db_session.add(vr)
        db_session.commit()

        internal_msgs = [
            {
                "id": "msg-vr-internal",
                "subject": "Internal discussion",
                "from": {"emailAddress": {"address": "buyer@trioscs.com"}},
                "toRecipients": [{"emailAddress": {"address": "other@trioscs.com"}}],
                "bodyPreview": "Internal note",
                "receivedDateTime": "2025-01-15T10:00:00Z",
                "conversationId": "conv-vr-internal",
            },
        ]

        with patch("app.services.email_threads.GraphClient") as MockGC:
            instance = MockGC.return_value
            instance.get_all_pages = AsyncMock(return_value=internal_msgs)

            threads = await fetch_threads_for_requirement(
                requirement.id, "fake-token", db_session, user_id=test_user.id
            )

        vr_threads = [t for t in threads if t.get("conversation_id") == "conv-vr-internal"]
        assert len(vr_threads) == 0


# ═══════════════════════════════════════════════════════════════════════
#  fetch_threads_for_requirement — Tier 4 vendor card domain
# ═══════════════════════════════════════════════════════════════════════


class TestFetchThreadsTier4VendorCard:
    """Test Tier 4 with vendor card domain lookups."""

    def setup_method(self):
        clear_cache()

    @pytest.mark.asyncio
    async def test_tier4_vendor_card_domain_from_sighting(self, db_session, test_user, test_requisition):
        """Tier 4: Vendor domain is found via VendorCard linked through sighting vendor name."""
        requirement = test_requisition.requirements[0]

        # Create vendor card with domain
        card = VendorCard(
            normalized_name="arrow electronics",
            display_name="Arrow Electronics",
            domain="arrow.com",
        )
        db_session.add(card)
        db_session.flush()

        # Create sighting that links to vendor card by name
        sighting = Sighting(
            requirement_id=requirement.id,
            vendor_name="Arrow Electronics",
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
                        "id": "msg-t4-card",
                        "subject": "Arrow quote",
                        "from": {"emailAddress": {"address": "rep@arrow.com"}},
                        "toRecipients": [{"emailAddress": {"address": "buyer@trioscs.com"}}],
                        "bodyPreview": "Quote for LM317T",
                        "receivedDateTime": "2025-01-18T10:00:00Z",
                        "conversationId": "conv-tier4-card",
                    },
                ]
            return []

        with patch("app.services.email_threads.GraphClient") as MockGC:
            instance = MockGC.return_value
            instance.get_all_pages = AsyncMock(side_effect=mock_get_all_pages)

            with patch("app.vendor_utils.normalize_vendor_name", return_value="arrow electronics"):
                threads = await fetch_threads_for_requirement(
                    requirement.id, "fake-token", db_session, user_id=test_user.id
                )

        domain_threads = [t for t in threads if t.get("matched_via") == "vendor_domain"]
        assert len(domain_threads) >= 1

    @pytest.mark.asyncio
    async def test_tier4_domain_search_error_handled(self, db_session, test_user, test_requisition):
        """Tier 4: Error in domain search is handled gracefully."""
        requirement = test_requisition.requirements[0]

        sighting = Sighting(
            requirement_id=requirement.id,
            vendor_name="BadVendor",
            vendor_email="sales@badvendor.com",
            mpn_matched="LM317T",
            source_type="broker",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(sighting)
        db_session.commit()

        async def mock_error(path, params=None, max_items=50):
            search_str = (params or {}).get("$search", "")
            if "badvendor.com" in search_str:
                raise Exception("Network error")
            return []

        with patch("app.services.email_threads.GraphClient") as MockGC:
            instance = MockGC.return_value
            instance.get_all_pages = AsyncMock(side_effect=mock_error)

            threads = await fetch_threads_for_requirement(
                requirement.id, "fake-token", db_session, user_id=test_user.id
            )

        assert isinstance(threads, list)

    @pytest.mark.asyncio
    async def test_tier3_short_part_number_skipped(self, db_session, test_user):
        """Tier 3: Part numbers shorter than 3 chars are skipped."""
        clear_cache()
        req = Requisition(
            name="REQ-SHORT",
            customer_name="Test",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        item = Requirement(
            requisition_id=req.id,
            primary_mpn="AB",  # Too short for tier 3
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.commit()

        with patch("app.services.email_threads.GraphClient") as MockGC:
            instance = MockGC.return_value
            instance.get_all_pages = AsyncMock(return_value=[])

            threads = await fetch_threads_for_requirement(item.id, "fake-token", db_session, user_id=test_user.id)

        assert threads == []

    @pytest.mark.asyncio
    async def test_tier2_subject_search_error_handled(self, db_session, test_user, test_requisition):
        """Tier 2: Graph search error is handled gracefully."""
        requirement = test_requisition.requirements[0]
        call_count = 0

        async def mock_err(path, params=None, max_items=50):
            nonlocal call_count
            call_count += 1
            search_str = (params or {}).get("$search", "")
            if "subject:" in search_str:
                raise Exception("Search API error")
            return []

        with patch("app.services.email_threads.GraphClient") as MockGC:
            instance = MockGC.return_value
            instance.get_all_pages = AsyncMock(side_effect=mock_err)

            threads = await fetch_threads_for_requirement(
                requirement.id, "fake-token", db_session, user_id=test_user.id
            )

        assert isinstance(threads, list)

    @pytest.mark.asyncio
    async def test_tier3_part_number_search_error_handled(self, db_session, test_user, test_requisition):
        """Tier 3: Graph part number search error handled gracefully."""
        requirement = test_requisition.requirements[0]

        async def mock_err(path, params=None, max_items=50):
            search_str = (params or {}).get("$search", "")
            if "LM317T" in search_str and "subject:" not in search_str:
                raise Exception("Part number search error")
            return []

        with patch("app.services.email_threads.GraphClient") as MockGC:
            instance = MockGC.return_value
            instance.get_all_pages = AsyncMock(side_effect=mock_err)

            threads = await fetch_threads_for_requirement(
                requirement.id, "fake-token", db_session, user_id=test_user.id
            )

        assert isinstance(threads, list)


# ═══════════════════════════════════════════════════════════════════════
#  Tier 1 Contact error handling
# ═══════════════════════════════════════════════════════════════════════


class TestTier1ContactError:
    def setup_method(self):
        clear_cache()

    @pytest.mark.asyncio
    async def test_tier1_contact_graph_error(self, db_session, test_user, test_requisition):
        """Tier 1: Graph error for Contact conversationId is handled."""
        req = test_requisition
        requirement = req.requirements[0]

        contact = Contact(
            requisition_id=req.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="Arrow",
            vendor_contact="sales@arrow.com",
            graph_conversation_id="conv-err-contact",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(contact)
        db_session.commit()

        async def mock_err(path, params=None, max_items=50):
            filter_str = (params or {}).get("$filter", "")
            if "conv-err-contact" in filter_str:
                raise Exception("Auth expired")
            return []

        with patch("app.services.email_threads.GraphClient") as MockGC:
            instance = MockGC.return_value
            instance.get_all_pages = AsyncMock(side_effect=mock_err)

            threads = await fetch_threads_for_requirement(
                requirement.id, "fake-token", db_session, user_id=test_user.id
            )

        assert isinstance(threads, list)


# ═══════════════════════════════════════════════════════════════════════
#  fetch_threads_for_vendor — domain search error handling
# ═══════════════════════════════════════════════════════════════════════


class TestFetchThreadsVendorDomainError:
    def setup_method(self):
        clear_cache()

    @pytest.mark.asyncio
    async def test_vendor_domain_search_error(self, db_session, test_user):
        """Error searching a vendor domain returns empty, doesn't crash."""
        card = VendorCard(
            normalized_name="error vendor",
            display_name="Error Vendor",
            domain="errorvendor.com",
        )
        db_session.add(card)
        db_session.commit()

        with patch("app.services.email_threads.GraphClient") as MockGC:
            instance = MockGC.return_value
            instance.get_all_pages = AsyncMock(side_effect=Exception("API down"))

            threads = await fetch_threads_for_vendor(card.id, "fake-token", db_session, user_id=test_user.id)

        assert threads == []

    @pytest.mark.asyncio
    async def test_vendor_internal_only_filtered(self, db_session, test_user):
        """Vendor thread with only internal messages is excluded."""
        card = VendorCard(
            normalized_name="internal vendor",
            display_name="Internal Vendor",
            domain="internalvendor.com",
        )
        db_session.add(card)
        db_session.commit()

        internal_msgs = [
            {
                "id": "msg-int",
                "subject": "Internal",
                "from": {"emailAddress": {"address": "buyer@trioscs.com"}},
                "toRecipients": [{"emailAddress": {"address": "other@trioscs.com"}}],
                "bodyPreview": "Internal note",
                "receivedDateTime": "2025-01-15T10:00:00Z",
                "conversationId": "conv-int-vendor",
            },
        ]

        with patch("app.services.email_threads.GraphClient") as MockGC:
            instance = MockGC.return_value
            instance.get_all_pages = AsyncMock(return_value=internal_msgs)

            threads = await fetch_threads_for_vendor(card.id, "fake-token", db_session, user_id=test_user.id)

        assert threads == []
