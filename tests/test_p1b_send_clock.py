"""TDD tests for P1b: outbound clock at send time + Contact.sent_at.

Tests written FIRST (RED phase) — they fail until the implementation exists.

Covers:
1. send_batch_rfq success → Contact.sent_at set AND outbound ActivityLog written
   immediately with no graph_message_id yet.
2. scan_sent_folder reconcile → SAME ActivityLog row updated (no duplicate),
   graph_message_id/graph_conversation_id populated.
3. Reply-matching (Tier-1): inbound reply using graph_conversation_id still
   matches after reconciliation.
"""

import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.email_service import send_batch_rfq
from app.models import (
    ActivityLog,
    Contact,
)
from app.services.activity_service import log_email_activity

# ─────────────────────────────────────────────────────────────────────────────
# Helper: build a mock GraphClient that sends OK and returns no sent-items
# (simulates the _find_sent_message lookup finding nothing immediately — so
# the outbound ActivityLog is written with no graph_message_id).
# ─────────────────────────────────────────────────────────────────────────────


def _mock_gc_send_ok_no_lookup():
    """GraphClient mock: sendMail succeeds, sent-folder lookup returns no match."""
    gc = AsyncMock()
    gc.post_json.return_value = {}  # success: no 'error' key
    gc.get_json.return_value = {"value": []}  # empty sent-items
    return gc


def _mock_gc_send_ok_with_lookup(req_id, tagged_subject):
    """GraphClient mock: sendMail succeeds, sent-folder lookup finds the message."""
    gc = AsyncMock()
    gc.post_json.return_value = {}
    gc.get_json.return_value = {
        "value": [
            {
                "id": "graph-msg-001",
                "conversationId": "graph-conv-001",
                "subject": tagged_subject,
                "toRecipients": [{"emailAddress": {"address": "vendor@acme.com"}}],
            }
        ]
    }
    return gc


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: Contact.sent_at is set and outbound ActivityLog exists IMMEDIATELY
# ─────────────────────────────────────────────────────────────────────────────


class TestSendBatchRfqSentAt:
    @pytest.mark.asyncio
    async def test_sent_at_set_on_success(self, db_session, test_user, test_requisition):
        """Contact.sent_at is populated the moment sendMail succeeds."""
        gc = _mock_gc_send_ok_no_lookup()
        vendor_groups = [
            {
                "vendor_name": "Acme Parts",
                "vendor_email": "vendor@acme.com",
                "parts": ["P100"],
                "subject": "RFQ",
                "body": "Please quote.",
            }
        ]

        before = datetime.now(UTC)
        with (
            patch("app.utils.graph_client.GraphClient", return_value=gc),
            patch("app.email_service.get_credential_cached", return_value=None),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            results = await send_batch_rfq(
                token="tok",
                db=db_session,
                user_id=test_user.id,
                requisition_id=test_requisition.id,
                vendor_groups=vendor_groups,
            )
        after = datetime.now(UTC)

        assert results[0]["status"] == "sent"

        contact = db_session.query(Contact).filter_by(requisition_id=test_requisition.id).first()
        assert contact is not None
        # sent_at must be set and fall within the test window
        assert contact.sent_at is not None, "Contact.sent_at must be set at send time"
        # Normalise tz-naive values from SQLite
        ca = contact.sent_at.replace(tzinfo=UTC) if contact.sent_at.tzinfo is None else contact.sent_at
        assert before <= ca <= after, f"sent_at {ca} not in [{before}, {after}]"

    @pytest.mark.asyncio
    async def test_outbound_activity_log_exists_immediately(self, db_session, test_user, test_requisition):
        """After sendMail succeeds, an outbound ActivityLog row exists before the 30-min
        scan — with direction=outbound and no external_id (no graph id yet)."""
        gc = _mock_gc_send_ok_no_lookup()
        vendor_groups = [
            {
                "vendor_name": "Acme Parts",
                "vendor_email": "vendor@acme.com",
                "parts": ["P100"],
                "subject": "RFQ",
                "body": "Please quote.",
            }
        ]

        with (
            patch("app.utils.graph_client.GraphClient", return_value=gc),
            patch("app.email_service.get_credential_cached", return_value=None),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            results = await send_batch_rfq(
                token="tok",
                db=db_session,
                user_id=test_user.id,
                requisition_id=test_requisition.id,
                vendor_groups=vendor_groups,
            )

        assert results[0]["status"] == "sent"

        outbound_logs = (
            db_session.query(ActivityLog)
            .filter(
                ActivityLog.direction == "outbound",
                ActivityLog.contact_email == "vendor@acme.com",
            )
            .all()
        )
        assert len(outbound_logs) == 1, f"Expected exactly 1 outbound ActivityLog, got {len(outbound_logs)}"
        log = outbound_logs[0]
        # No graph id yet — scan will fill it later
        assert log.external_id is None, "ActivityLog.external_id must be NULL at send time (graph id not set yet)"

    @pytest.mark.asyncio
    async def test_sent_at_not_set_on_failure(self, db_session, test_user, test_requisition):
        """Contact.sent_at is NOT set when sendMail fails."""
        gc = AsyncMock()
        gc.post_json.side_effect = Exception("Network error")

        vendor_groups = [
            {
                "vendor_name": "Acme Parts",
                "vendor_email": "vendor@acme.com",
                "parts": ["P100"],
                "subject": "RFQ",
                "body": "Please quote.",
            }
        ]

        with (
            patch("app.utils.graph_client.GraphClient", return_value=gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await send_batch_rfq(
                token="tok",
                db=db_session,
                user_id=test_user.id,
                requisition_id=test_requisition.id,
                vendor_groups=vendor_groups,
            )

        assert results[0]["status"] == "failed"

        contact = db_session.query(Contact).filter_by(requisition_id=test_requisition.id).first()
        assert contact is not None
        assert contact.sent_at is None, "Contact.sent_at must be NULL when send failed"


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: scan_sent_folder reconcile — same ActivityLog row updated, no dup
# ─────────────────────────────────────────────────────────────────────────────


class TestScanSentFolderReconcile:
    @pytest.mark.asyncio
    async def test_scan_updates_existing_log_no_duplicate(self, db_session, test_user, test_requisition):
        """scan_sent_folder, when it finds the graph message for a send that already has
        an outbound ActivityLog, must UPDATE that row (setting external_id + graph ids)
        and NOT create a second outbound ActivityLog for the same send."""
        from app.jobs.email_jobs import scan_sent_folder

        tagged_subject = f"RFQ [ref:{test_requisition.id}]"
        vendor_email = "vendor@acme.com"
        graph_msg_id = "graph-msg-001"
        graph_conv_id = "graph-conv-001"

        # --- Step 1: simulate send_batch_rfq side-effects ---
        # Create the Contact (status=sent, sent_at set)
        now = datetime.now(UTC)
        contact = Contact(
            requisition_id=test_requisition.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="Acme Parts",
            vendor_name_normalized="acme parts",
            vendor_contact=vendor_email,
            parts_included=["P100"],
            subject=tagged_subject,
            details="Please quote.",
            status="sent",
            sent_at=now,
            status_updated_at=now,
            created_at=now,
        )
        db_session.add(contact)
        db_session.flush()

        # Create the outbound ActivityLog via the REAL log_email_activity path
        # (exactly as send_batch_rfq does) so this test genuinely exercises the
        # reconcile match.  occurred_at=now is passed here — without it the row
        # gets occurred_at=NULL and the reconcile query would miss it (the bug).
        activity = log_email_activity(
            user_id=test_user.id,
            direction="sent",
            email_addr=vendor_email,
            subject=tagged_subject,
            external_id=None,  # not set at send time
            contact_name="Acme Parts",
            db=db_session,
            requisition_id=test_requisition.id,
            occurred_at=now,
        )
        db_session.commit()

        assert activity is not None, "log_email_activity must return an ActivityLog"
        activity_id_before = activity.id
        log_count_before = (
            db_session.query(ActivityLog)
            .filter(
                ActivityLog.direction == "outbound",
                ActivityLog.contact_email == vendor_email,
            )
            .count()
        )
        assert log_count_before == 1

        # --- Step 2: run scan_sent_folder ---
        gc_mock = MagicMock()
        gc_mock.delta_query = AsyncMock(
            return_value=(
                [
                    {
                        "id": graph_msg_id,
                        "subject": tagged_subject,
                        "sentDateTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "toRecipients": [{"emailAddress": {"address": vendor_email}}],
                        "hasAttachments": False,
                        "conversationId": graph_conv_id,
                    }
                ],
                "new-delta-token",
            )
        )

        with (
            patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch("app.utils.graph_client.GraphClient", return_value=gc_mock),
        ):
            await scan_sent_folder(test_user, db_session)

        # --- Assert: still exactly ONE outbound ActivityLog ---
        log_count_after = (
            db_session.query(ActivityLog)
            .filter(
                ActivityLog.direction == "outbound",
                ActivityLog.contact_email == vendor_email,
            )
            .count()
        )
        assert log_count_after == 1, (
            f"Expected 1 outbound ActivityLog after scan, got {log_count_after} — duplicate created"
        )

        # --- Assert: the EXISTING row was updated with graph id ---
        updated_log = db_session.get(ActivityLog, activity_id_before)
        assert updated_log is not None
        assert updated_log.external_id == graph_msg_id, (
            f"ActivityLog.external_id must be updated to graph_msg_id, got {updated_log.external_id!r}"
        )

    @pytest.mark.asyncio
    async def test_scan_falls_back_to_create_when_no_send_time_log(self, db_session, test_user, test_requisition):
        """scan_sent_folder falls back to CREATE a new ActivityLog when no existing
        send-time row is found — preserves backward compatibility with old sends that
        predate this change."""
        from app.jobs.email_jobs import scan_sent_folder

        tagged_subject = f"RFQ [ref:{test_requisition.id}]"
        vendor_email = "legacy@vendor.com"
        graph_msg_id = "graph-legacy-001"

        # No ActivityLog or Contact row exists (old send, pre-P1b)

        gc_mock = MagicMock()
        now = datetime.now(UTC)
        gc_mock.delta_query = AsyncMock(
            return_value=(
                [
                    {
                        "id": graph_msg_id,
                        "subject": tagged_subject,
                        "sentDateTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "toRecipients": [{"emailAddress": {"address": vendor_email}}],
                        "hasAttachments": False,
                    }
                ],
                "new-delta-token",
            )
        )

        with (
            patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch("app.utils.graph_client.GraphClient", return_value=gc_mock),
        ):
            result = await scan_sent_folder(test_user, db_session)

        # A new ActivityLog is created (fallback path)
        assert len(result) >= 1
        log = db_session.query(ActivityLog).filter(ActivityLog.external_id == graph_msg_id).first()
        assert log is not None, "Fallback path must create an ActivityLog with external_id set"

    @pytest.mark.asyncio
    async def test_scan_fallback_create_resolves_entity_attribution(self, db_session, test_user, test_requisition):
        """ISS-030: the fallback CREATE path resolves the recipient to a Company via
        match_email_to_entity and stamps company_id — a NULL company_id here is exactly
        the leak-scenario gap the get_company_activities() scope fix depends on being
        backfilled."""
        from app.jobs.email_jobs import scan_sent_folder
        from app.models import Company

        company = Company(name="Acme Customer", domain="acme-customer.com", is_active=True)
        db_session.add(company)
        db_session.commit()

        tagged_subject = f"RFQ [ref:{test_requisition.id}]"
        vendor_email = "buyer@acme-customer.com"
        graph_msg_id = "graph-attrib-001"

        gc_mock = MagicMock()
        now = datetime.now(UTC)
        gc_mock.delta_query = AsyncMock(
            return_value=(
                [
                    {
                        "id": graph_msg_id,
                        "subject": tagged_subject,
                        "sentDateTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "toRecipients": [{"emailAddress": {"address": vendor_email}}],
                        "hasAttachments": False,
                    }
                ],
                "new-delta-token",
            )
        )

        with (
            patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch("app.utils.graph_client.GraphClient", return_value=gc_mock),
        ):
            await scan_sent_folder(test_user, db_session)

        log = db_session.query(ActivityLog).filter(ActivityLog.external_id == graph_msg_id).first()
        assert log is not None
        assert log.company_id == company.id, "Recipient domain match must attribute company_id on the sent row"
        assert log.vendor_card_id is None


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: Reply-matching (Tier-1) still works after reconciliation
# ─────────────────────────────────────────────────────────────────────────────


class TestReplyMatchingAfterReconcile:
    @pytest.mark.asyncio
    async def test_tier1_match_works_after_scan_reconcile(self, db_session, test_user, test_requisition):
        """After scan_sent_folder reconciles the ActivityLog row (sets external_id), a
        Contact with graph_conversation_id set is still matched by poll_inbox Tier-1
        (conversation-id lookup)."""
        from app.email_service import poll_inbox

        graph_conv_id = "graph-conv-999"
        vendor_email = "vendor@acme.com"
        tagged_subject = f"RFQ for parts [ref:{test_requisition.id}]"

        # Create a Contact with graph_conversation_id (set by _find_sent_message
        # right after send, OR by scan_sent_folder reconcile path)
        now = datetime.now(UTC)
        contact = Contact(
            requisition_id=test_requisition.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="Acme Parts",
            vendor_name_normalized="acme parts",
            vendor_contact=vendor_email,
            parts_included=["P100"],
            subject=tagged_subject,
            details="Please quote.",
            status="sent",
            sent_at=now,
            status_updated_at=now,
            created_at=now,
            graph_message_id="graph-msg-999",
            graph_conversation_id=graph_conv_id,
        )
        db_session.add(contact)
        db_session.commit()

        # Simulate an inbound reply on the same conversation.
        # SQLite UTCDateTime requires a datetime object (not an ISO string) for
        # received_at — use a real datetime to avoid the SQLite type-check error.
        inbound_msg = {
            "id": "reply-msg-001",
            "subject": f"Re: {tagged_subject}",
            "conversationId": graph_conv_id,
            "from": {"emailAddress": {"address": vendor_email, "name": "Vendor"}},
            "body": {"content": "We can supply at $1.20 each."},
            "receivedDateTime": now,  # datetime, not ISO string, so SQLite UTCDateTime accepts it
            "hasAttachments": False,
            "toRecipients": [],
        }

        mock_gc = AsyncMock()
        # Expired delta token → explicit fall back to the get_json full scan
        from app.utils.graph_client import GraphSyncStateExpired

        mock_gc.delta_query.side_effect = GraphSyncStateExpired("expired")
        mock_gc.get_json.return_value = {"value": [inbound_msg]}

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch("app.email_service._submit_parse_batch", new_callable=AsyncMock),
        ):
            results = await poll_inbox(
                token="tok",
                db=db_session,
                requisition_id=test_requisition.id,
                scanned_by_user_id=test_user.id,
            )

        # Tier-1 match: conversation_id match → VendorResponse with contact_id set
        from app.models import VendorResponse

        vr = db_session.query(VendorResponse).filter_by(graph_conversation_id=graph_conv_id).first()
        assert vr is not None, "Tier-1 match: VendorResponse must be created for the reply"
        assert vr.contact_id == contact.id, f"VendorResponse.contact_id must link to the Contact, got {vr.contact_id}"
