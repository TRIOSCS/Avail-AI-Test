"""Comprehensive tests for app/email_service.py.

Covers:
- _build_html_body
- send_batch_rfq (success, error, AI rephrase, missing email, lookup)
- _find_sent_message (match, no match, exception)
- log_phone_contact
- poll_inbox (delta path, fallback path, multi-tier matching, noise filter, dedup, error handling)
- _classify_response (all classification branches)
- _progress_contact_status (all status transitions)
- _is_noise_email (domains, prefixes, edge cases)
- parse_response_ai (success, None result)
- _submit_parse_batch
- _parse_sequential_fallback
- _apply_parsed_result (with and without notification)
- process_batch_results (full lifecycle: pending, completed, timeout, error)
"""

import json
import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.email_service import (
    NOISE_DOMAINS,
    NOISE_PREFIXES,
    DeliveryCheckUnavailable,
    _apply_parsed_result,
    _build_html_body,
    _classify_response,
    _email_mining_calls_today,
    _enforce_email_mining_cap,
    _find_sent_message,
    _is_noise_email,
    _parse_sequential_fallback,
    _progress_contact_status,
    _record_email_mining_calls,
    _scope_thread_contacts_to_sender,
    _submit_parse_batch,
    log_phone_contact,
    parse_response_ai,
    poll_inbox,
    process_batch_results,
    send_batch_rfq,
)
from app.models import (
    ActivityLog,
    Contact,
    PendingBatch,
    ProcessedMessage,
    Requirement,
    Requisition,
    SyncState,
    User,
    VendorResponse,
)
from app.utils.graph_client import GraphSyncStateExpired

# ── _build_html_body ─────────────────────────────────────────────────


class TestBuildHtmlBody:
    def test_plain_text_converted_to_html(self):
        result = _build_html_body("Hello\nWorld")
        assert "<br>" in result
        assert "Hello<br>" in result
        assert "World" in result
        assert "<html>" in result
        assert "</html>" in result
        assert "font-family" in result

    def test_single_line_no_br(self):
        result = _build_html_body("No newlines here")
        assert "No newlines here" in result
        assert "<br>" not in result

    def test_empty_string(self):
        result = _build_html_body("")
        assert "<html>" in result
        assert "</html>" in result

    def test_multiline(self):
        result = _build_html_body("Line1\nLine2\nLine3")
        assert result.count("<br>") == 2


# ── _is_noise_email ──────────────────────────────────────────────────


class TestIsNoiseEmail:
    def test_empty_email(self):
        assert _is_noise_email("") is True

    def test_no_at_sign(self):
        assert _is_noise_email("notanemail") is True

    def test_noise_domain(self):
        for domain in ["microsoft.com", "google.com", "linkedin.com"]:
            assert _is_noise_email(f"user@{domain}") is True

    def test_noise_prefix(self):
        for prefix in ["noreply", "no-reply", "mailer-daemon", "newsletter"]:
            assert _is_noise_email(f"{prefix}@vendor.com") is True

    def test_valid_vendor_email(self):
        assert _is_noise_email("sales.team@vendor-parts.com") is False

    def test_case_insensitive_domain(self):
        # The function lower-cases both local and domain
        assert _is_noise_email("user@Microsoft.Com") is True

    def test_case_insensitive_prefix(self):
        assert _is_noise_email("NoReply@vendor.com") is True

    def test_none_email(self):
        assert _is_noise_email(None) is True


# ── _classify_response ───────────────────────────────────────────────


class TestClassifyResponse:
    def test_ooo_bounce_in_body(self):
        result = _classify_response({}, "I am currently out of the office.", "")
        assert result["type"] == "ooo_bounce"
        assert result["needs_action"] is False

    def test_ooo_bounce_in_subject(self):
        result = _classify_response({}, "", "Automatic Reply: Out of office")
        assert result["type"] == "ooo_bounce"

    def test_ooo_undeliverable(self):
        result = _classify_response({}, "Undeliverable: message could not be delivered", "")
        assert result["type"] == "ooo_bounce"

    def test_quote_provided(self):
        parsed = {"parts": [{"unit_price": 1.50, "mpn": "LM317T"}]}
        result = _classify_response(parsed, "Here is our quote", "RE: RFQ")
        assert result["type"] == "quote_provided"
        assert result["needs_action"] is True
        assert "1 part(s)" in result["action_hint"]

    def test_quote_provided_multiple_parts(self):
        parsed = {
            "parts": [
                {"unit_price": 1.50, "mpn": "LM317T"},
                {"unit_price": 2.30, "mpn": "LM7805"},
            ]
        }
        result = _classify_response(parsed, "Quote attached", "")
        assert result["type"] == "quote_provided"
        assert "2 part(s)" in result["action_hint"]

    def test_partial_availability(self):
        parsed = {
            "parts": [{"qty_available": 500}],
            "sentiment": "positive",
        }
        result = _classify_response(parsed, "We have some in stock", "")
        assert result["type"] == "partial_availability"
        assert result["needs_action"] is True

    def test_no_stock_signal(self):
        result = _classify_response({}, "We are currently out of stock for this item.", "")
        assert result["type"] == "no_stock"
        assert result["needs_action"] is False

    def test_no_stock_negative_sentiment(self):
        parsed = {"sentiment": "negative"}
        result = _classify_response(parsed, "Sorry about that", "")
        assert result["type"] == "no_stock"

    def test_counter_offer(self):
        result = _classify_response({}, "We can offer an alternative part instead.", "")
        assert result["type"] == "counter_offer"
        assert result["needs_action"] is True

    def test_counter_offer_substitute(self):
        result = _classify_response({}, "We have a substitute available.", "")
        assert result["type"] == "counter_offer"

    def test_clarification_needed(self):
        result = _classify_response({}, "Could you please confirm the quantity? What version do you need?", "")
        assert result["type"] == "clarification_needed"
        assert result["needs_action"] is True

    def test_clarification_needs_question_mark(self):
        # "can you" present but no question mark => falls through
        result = _classify_response({}, "can you do this for us please", "")
        assert result["type"] != "clarification_needed"

    def test_follow_up_pending_default(self):
        result = _classify_response({}, "Thanks for reaching out, we will review.", "")
        assert result["type"] == "follow_up_pending"
        assert result["needs_action"] is True

    def test_empty_body_and_subject(self):
        result = _classify_response({}, "", "")
        assert result["type"] == "follow_up_pending"

    def test_none_body(self):
        result = _classify_response({}, None, None)
        assert result["type"] == "follow_up_pending"

    def test_body_truncated_to_2000_chars(self):
        """Signals after 2000 chars should not be detected."""
        body = "x" * 2001 + "out of stock"
        result = _classify_response({}, body, "")
        # "out of stock" is past the 2000 char cutoff
        assert result["type"] == "follow_up_pending"

    def test_parts_positive_no_qty(self):
        """Parts with positive sentiment but no qty_available -> follow_up_pending."""
        parsed = {"parts": [{"mpn": "ABC"}], "sentiment": "positive"}
        result = _classify_response(parsed, "Looks good", "")
        assert result["type"] == "follow_up_pending"

    def test_delivery_failure_bounce(self):
        result = _classify_response({}, "delivery failure notification", "")
        assert result["type"] == "ooo_bounce"

    def test_no_stock_regret(self):
        result = _classify_response({}, "We regret to inform you we cannot fulfil.", "")
        assert result["type"] == "no_stock"


# ── _progress_contact_status ─────────────────────────────────────────


class TestProgressContactStatus:
    def _make_contact(self, status="sent"):
        c = MagicMock(spec=Contact)
        c.status = status
        c.status_updated_at = None
        return c

    def _make_vr(self, classification=None):
        vr = MagicMock(spec=VendorResponse)
        vr.classification = classification
        return vr

    @pytest.mark.parametrize(
        ("initial_status", "classification", "expected_status"),
        [
            # Terminal statuses are never changed by a later response.
            pytest.param("quoted", "no_stock", "quoted", id="quoted_terminal_no_change"),
            pytest.param("declined", "quote_provided", "declined", id="declined_terminal_no_change"),
            # Classification-driven transitions from a non-terminal "sent".
            pytest.param("sent", "quote_provided", "quoted", id="quote_provided_sets_quoted"),
            pytest.param("sent", "no_stock", "declined", id="no_stock_sets_declined"),
            pytest.param("sent", "ooo_bounce", "pending", id="ooo_bounce_sets_pending"),
            pytest.param("sent", "clarification_needed", "responded", id="clarification_needed_sets_responded"),
            pytest.param("sent", "counter_offer", "responded", id="counter_offer_sets_responded"),
            pytest.param("sent", "partial_availability", "responded", id="partial_availability_sets_responded"),
            # Unknown / None classification falls to the else branch.
            pytest.param("sent", "unknown_type", "responded", id="unknown_classification_sent_to_responded"),
            pytest.param("opened", "anything", "responded", id="unknown_classification_opened_to_responded"),
            # Not in ("sent", "opened") so status doesn't change from the else branch.
            pytest.param("responded", "something_else", "responded", id="unknown_classification_responded_stays"),
            # None -> empty string -> falls to else branch -> sent->responded.
            pytest.param("sent", None, "responded", id="none_classification"),
        ],
    )
    def test_status_transition(self, initial_status, classification, expected_status):
        c = self._make_contact(initial_status)
        _progress_contact_status(c, self._make_vr(classification), MagicMock())
        assert c.status == expected_status

    def test_status_updated_at_is_set(self):
        c = self._make_contact("sent")
        _progress_contact_status(c, self._make_vr("quote_provided"), MagicMock())
        assert c.status_updated_at is not None

    def test_transitions_assign_strenum_members(self):
        """Phase-3 cleanup: statuses are set from ContactStatus StrEnum members,
        not raw string literals (value-identical, type-stronger)."""
        from app.constants import ContactStatus

        c = self._make_contact("sent")
        _progress_contact_status(c, self._make_vr("quote_provided"), MagicMock())
        assert c.status is ContactStatus.QUOTED
        assert c.status == "quoted"


# ── Phase-3 signature / StrEnum cleanup regression ───────────────────


class TestEmailServiceCleanup:
    def test_poll_inbox_optional_annotations(self):
        import inspect

        from app.email_service import poll_inbox

        hints = inspect.get_annotations(poll_inbox)
        assert hints["requisition_id"] == (int | None)
        assert hints["scanned_by_user_id"] == (int | None)

    def test_apply_parsed_result_optional_db_annotation(self):
        import inspect

        from sqlalchemy.orm import Session

        from app.email_service import _apply_parsed_result

        hints = inspect.get_annotations(_apply_parsed_result)
        assert hints["db"] == (Session | None)


# ── log_phone_contact ────────────────────────────────────────────────


class TestLogPhoneContact:
    def test_creates_contact(self, db_session, test_user, test_requisition):
        result = log_phone_contact(
            db=db_session,
            user_id=test_user.id,
            requisition_id=test_requisition.id,
            vendor_name="Acme Parts",
            vendor_phone="+1-555-1234",
            parts=["LM317T", "LM7805"],
        )
        assert result["vendor_name"] == "Acme Parts"
        assert result["vendor_phone"] == "+1-555-1234"
        assert result["contact_type"] == "phone"
        assert "id" in result
        assert "created_at" in result

        # Verify DB record
        contact = db_session.get(Contact, result["id"])
        assert contact is not None
        assert contact.contact_type == "phone"
        assert contact.vendor_name == "Acme Parts"
        assert contact.vendor_contact == "+1-555-1234"
        assert contact.subject == "Call to Acme Parts"


# ── _find_sent_message ───────────────────────────────────────────────


def _sent_item(msg_id, conv_id, subject, recipient="sales@vendora.com"):
    """A Graph sentItems message shaped like the real API response (toRecipients always
    present under the $select the service requests)."""
    return {
        "id": msg_id,
        "conversationId": conv_id,
        "subject": subject,
        "toRecipients": [{"emailAddress": {"address": recipient}}],
    }


class TestFindSentMessage:
    @pytest.mark.asyncio
    async def test_found_matching_subject(self):
        gc = AsyncMock()
        gc.get_json.return_value = {
            "value": [
                _sent_item("msg-1", "conv-1", "RFQ Parts [ref:10]"),
                _sent_item("msg-2", "conv-2", "Something Else"),
            ]
        }
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await _find_sent_message(gc, "RFQ Parts [ref:10]", "sales@vendora.com")
        assert result["id"] == "msg-1"
        assert result["conversationId"] == "conv-1"

    @pytest.mark.asyncio
    async def test_same_subject_discriminated_by_recipient(self):
        """The batch-collision case (F1): every vendor in a batch shares ONE tagged
        subject, so the lookup MUST discriminate on toRecipients — each vendor's lookup
        returns the message addressed to THAT vendor, never the newest."""
        gc = AsyncMock()
        shared = "RFQ — 2 parts [ref:1] [ref:2]"
        gc.get_json.return_value = {
            "value": [
                _sent_item("sent-b", "conv-b", shared, recipient="b@vendorb.com"),
                _sent_item("sent-a", "conv-a", shared, recipient="a@vendora.com"),
            ]
        }
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result_a = await _find_sent_message(gc, shared, "a@vendora.com")
            result_b = await _find_sent_message(gc, shared, "B@VendorB.com")  # case-insensitive
        assert result_a["id"] == "sent-a"
        assert result_b["id"] == "sent-b"

    @pytest.mark.asyncio
    async def test_subject_match_wrong_recipient_returns_none(self):
        """A same-subject message addressed to a DIFFERENT vendor must not match."""
        gc = AsyncMock()
        gc.get_json.return_value = {"value": [_sent_item("sent-b", "conv-b", "RFQ [ref:1]", recipient="b@vendorb.com")]}
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await _find_sent_message(gc, "RFQ [ref:1]", "a@vendora.com")
        assert result is None

    @pytest.mark.asyncio
    async def test_no_matching_subject(self):
        gc = AsyncMock()
        gc.get_json.return_value = {"value": [_sent_item("msg-1", "conv-1", "Not a match")]}
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await _find_sent_message(gc, "RFQ Parts [ref:10]", "sales@vendora.com")
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_response(self):
        gc = AsyncMock()
        gc.get_json.return_value = None
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await _find_sent_message(gc, "Subject", "sales@vendora.com")
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_value(self):
        gc = AsyncMock()
        gc.get_json.return_value = {"value": []}
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await _find_sent_message(gc, "Subject", "sales@vendora.com")
        assert result is None

    @pytest.mark.asyncio
    async def test_exception_on_every_attempt_raises_delivery_check_unavailable(self):
        """P1 finding #2 (2026-07-22 deep review): when EVERY retry attempt hits a Graph
        error, delivery is INDETERMINATE — the function must raise
        DeliveryCheckUnavailable, never return a bare None (which would be
        indistinguishable from a confirmed no-match and would let a caller wrongly
        justify a resend)."""
        gc = AsyncMock()
        gc.get_json.side_effect = Exception("Network error")
        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(DeliveryCheckUnavailable):
            await _find_sent_message(gc, "Subject", "sales@vendora.com")

    @pytest.mark.asyncio
    async def test_clean_no_match_still_returns_none_not_raise(self):
        """The raise-vs-None contract (d): a clean run — every attempt succeeds, none
        errors — that simply finds no matching message returns ``None`` (safe to treat
        as "not delivered"), distinct from the error case above which raises."""
        gc = AsyncMock()
        gc.get_json.return_value = {"value": [_sent_item("msg-1", "conv-1", "Something else")]}
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await _find_sent_message(gc, "Subject", "sales@vendora.com")
        assert result is None

    @pytest.mark.asyncio
    async def test_partial_error_then_clean_miss_still_raises(self):
        """One failed attempt followed by clean (no-error) misses on the rest STILL
        raises — ``api_error`` is sticky across the whole retry loop, per the documented
        "at least one lookup attempt failed" contract."""
        gc = AsyncMock()
        gc.get_json.side_effect = [
            Exception("transient 503"),
            {"value": []},
            {"value": []},
        ]
        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(DeliveryCheckUnavailable):
            await _find_sent_message(gc, "Subject", "sales@vendora.com")

    @pytest.mark.asyncio
    async def test_subject_whitespace_matching(self):
        gc = AsyncMock()
        gc.get_json.return_value = {"value": [_sent_item("msg-1", "conv-1", " RFQ [ref:10] ")]}
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await _find_sent_message(gc, " RFQ [ref:10] ", "sales@vendora.com")
        assert result["id"] == "msg-1"

    @pytest.mark.asyncio
    async def test_retries_on_miss_then_finds(self):
        """Verify retry loop: returns None twice, then finds the message on 3rd attempt."""
        gc = AsyncMock()
        gc.get_json.side_effect = [
            {"value": []},
            {"value": []},
            {"value": [_sent_item("msg-1", "conv-1", "Test Subject")]},
        ]
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await _find_sent_message(gc, "Test Subject", "sales@vendora.com")
        assert result["id"] == "msg-1"
        assert gc.get_json.call_count == 3

    @pytest.mark.asyncio
    async def test_early_return_on_first_match(self):
        """Verify function returns on first successful match without exhausting
        retries."""
        gc = AsyncMock()
        gc.get_json.return_value = {"value": [_sent_item("msg-1", "conv-1", "Quick Find")]}
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await _find_sent_message(gc, "Quick Find", "sales@vendora.com")
        assert result["id"] == "msg-1"
        assert gc.get_json.call_count == 1

    @pytest.mark.asyncio
    async def test_window_is_at_least_50(self):
        """P1d #1: $top window must be >=50 so that vendor messages in large batches
        (e.g. 30-40 recipients sent in one RFQ batch fan-out) are not pushed below the
        lookup window before scan_sent_folder can reconcile them.

        The previous value of 25 was provably insufficient for batches larger than ~25
        vendors. Raising to >=50 halves the miss rate; scan_sent_folder covers the tail.
        """
        gc = AsyncMock()
        # Simulate a large batch: vendor of interest is the 30th message in Sent Items
        # (positions 0-29), only visible with $top >= 30.
        target_recipient = "vendor30@example.com"
        target_subject = "RFQ Batch [ref:42]"
        # Fill slots 0-29 with other vendors sharing the same subject
        filler = [
            _sent_item(f"msg-{i}", f"conv-{i}", target_subject, recipient=f"other{i}@example.com") for i in range(29)
        ]
        target_msg = _sent_item("msg-target", "conv-target", target_subject, recipient=target_recipient)
        all_msgs = filler + [target_msg]  # target is at index 29 (30th message)

        gc.get_json.return_value = {"value": all_msgs}
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await _find_sent_message(gc, target_subject, target_recipient)

        # Message at slot 30 must be found — only possible if $top >= 30
        assert result is not None, "Vendor at slot 30 fell below $top window — window too small"
        assert result["id"] == "msg-target"
        # Also verify the Graph API was called with $top >= 50
        call_kwargs = gc.get_json.call_args_list[0]
        params = call_kwargs[1].get("params") or call_kwargs[0][1]
        assert int(params["$top"]) >= 50, f"$top must be >=50, got {params['$top']}"


# ── send_batch_rfq ───────────────────────────────────────────────────


class TestSendBatchRfq:
    @pytest.mark.asyncio
    async def test_successful_send(self, db_session, test_user, test_requisition):
        mock_gc = AsyncMock()
        mock_gc.post_json.return_value = {}  # Success: no error key
        mock_gc.get_json.return_value = {
            "value": [
                {
                    "id": "sent-msg-1",
                    "conversationId": "conv-1",
                    "subject": f"RFQ for parts [ref:{test_requisition.id}]",
                }
            ]
        }

        vendor_groups = [
            {
                "vendor_name": "Vendor A",
                "vendor_email": "sales@vendora.com",
                "parts": ["LM317T"],
                "subject": "RFQ for parts",
                "body": "Please quote on the following parts.",
            }
        ]

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            results = await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                requisition_id=test_requisition.id,
                vendor_groups=vendor_groups,
            )

        assert len(results) == 1
        assert results[0]["status"] == "sent"
        assert results[0]["vendor_name"] == "Vendor A"
        assert results[0]["vendor_email"] == "sales@vendora.com"
        assert "id" in results[0]

    @pytest.mark.asyncio
    async def test_send_exception(self, db_session, test_user, test_requisition):
        mock_gc = AsyncMock()
        mock_gc.post_json.side_effect = Exception("SMTP Error")

        vendor_groups = [
            {
                "vendor_name": "Vendor A",
                "vendor_email": "sales@vendora.com",
                "parts": ["LM317T"],
                "subject": "RFQ",
                "body": "Please quote.",
            }
        ]

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                requisition_id=test_requisition.id,
                vendor_groups=vendor_groups,
            )

        assert len(results) == 1
        assert results[0]["status"] == "failed"  # Now persists as Contact with status=failed
        assert "SMTP Error" in results[0]["error"]

    @pytest.mark.asyncio
    async def test_send_api_error(self, db_session, test_user, test_requisition):
        mock_gc = AsyncMock()
        mock_gc.post_json.return_value = {"error": "Unauthorized", "detail": "Token expired"}

        vendor_groups = [
            {
                "vendor_name": "Vendor A",
                "vendor_email": "sales@vendora.com",
                "parts": ["LM317T"],
                "subject": "RFQ",
                "body": "Please quote.",
            }
        ]

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                requisition_id=test_requisition.id,
                vendor_groups=vendor_groups,
            )

        assert len(results) == 1
        assert results[0]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_skip_empty_email(self, db_session, test_user, test_requisition):
        mock_gc = AsyncMock()

        vendor_groups = [
            {
                "vendor_name": "No Email Vendor",
                "vendor_email": "",
                "parts": ["LM317T"],
                "subject": "RFQ",
                "body": "Please quote.",
            },
            {
                "vendor_name": "No Email Key",
                "parts": ["LM317T"],
                "subject": "RFQ",
                "body": "Please quote.",
            },
        ]

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                requisition_id=test_requisition.id,
                vendor_groups=vendor_groups,
            )

        # Email-less vendors are recorded as "skipped" (not silently dropped), and no send
        # is attempted for them.
        assert len(results) == 2
        assert all(r["status"] == "skipped" for r in results)
        assert all("no contact email" in r["error"] for r in results)
        mock_gc.post_json.assert_not_called()

    @pytest.mark.asyncio
    async def test_subject_tagging(self, db_session, test_user, test_requisition):
        """Subject gets [ref:{id}] suffix if not already present."""
        mock_gc = AsyncMock()
        mock_gc.post_json.return_value = {}
        mock_gc.get_json.return_value = {"value": []}

        vendor_groups = [
            {
                "vendor_name": "Vendor A",
                "vendor_email": "sales@vendora.com",
                "parts": ["LM317T"],
                "subject": "Plain Subject",
                "body": "Please quote.",
            }
        ]

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                requisition_id=test_requisition.id,
                vendor_groups=vendor_groups,
            )

        # The posted payload should have the tagged subject
        call_args = mock_gc.post_json.call_args
        payload = call_args[0][1]
        assert f"[ref:{test_requisition.id}]" in payload["message"]["subject"]

    @pytest.mark.asyncio
    async def test_subject_already_tagged(self, db_session, test_user, test_requisition):
        """Subject already has [ref:{id}] — don't double-tag."""
        mock_gc = AsyncMock()
        mock_gc.post_json.return_value = {}
        mock_gc.get_json.return_value = {"value": []}

        avail_token = f"[ref:{test_requisition.id}]"
        vendor_groups = [
            {
                "vendor_name": "Vendor A",
                "vendor_email": "sales@vendora.com",
                "parts": ["LM317T"],
                "subject": f"Already Tagged {avail_token}",
                "body": "Please quote.",
            }
        ]

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                requisition_id=test_requisition.id,
                vendor_groups=vendor_groups,
            )

        payload = mock_gc.post_json.call_args[0][1]
        subject = payload["message"]["subject"]
        assert subject.count(avail_token) == 1

    @pytest.mark.asyncio
    async def test_ai_rephrase_success(self, db_session, test_user, test_requisition):
        """When AI rephrase succeeds, body is replaced."""
        mock_gc = AsyncMock()
        mock_gc.post_json.return_value = {}
        mock_gc.get_json.return_value = {"value": []}

        vendor_groups = [
            {
                "vendor_name": "Vendor A",
                "vendor_email": "sales@vendora.com",
                "parts": ["LM317T"],
                "subject": "RFQ",
                "body": "Original body text",
            }
        ]

        mock_rephrase = AsyncMock(return_value="Rephrased body text")

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value="fake-key"),
            patch("app.services.ai_service.rephrase_rfq", mock_rephrase),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            results = await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                requisition_id=test_requisition.id,
                vendor_groups=vendor_groups,
            )

        assert len(results) == 1
        assert results[0]["status"] == "sent"

    @pytest.mark.asyncio
    async def test_ai_rephrase_exception_fallback(self, db_session, test_user, test_requisition):
        """When AI rephrase import/call throws synchronously, original body is used
        (lines 52-53)."""
        mock_gc = AsyncMock()
        mock_gc.post_json.return_value = {}
        mock_gc.get_json.return_value = {"value": []}

        vendor_groups = [
            {
                "vendor_name": "Vendor A",
                "vendor_email": "sales@vendora.com",
                "parts": ["LM317T"],
                "subject": "RFQ",
                "body": "Original body text",
            }
        ]

        # Use MagicMock (not AsyncMock) so calling it raises synchronously
        # during the list comprehension, triggering the outer except block
        rephrase_mock = MagicMock(side_effect=Exception("AI down"))

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value="fake-key"),
            patch("app.services.ai_service.rephrase_rfq", rephrase_mock),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            results = await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                requisition_id=test_requisition.id,
                vendor_groups=vendor_groups,
            )

        assert len(results) == 1
        assert results[0]["status"] == "sent"

    @pytest.mark.asyncio
    async def test_ai_rephrase_returns_exception_in_gather(self, db_session, test_user, test_requisition):
        """When gather returns an exception for a rephrase task, original body is
        kept."""
        mock_gc = AsyncMock()
        mock_gc.post_json.return_value = {}
        mock_gc.get_json.return_value = {"value": []}

        vendor_groups = [
            {
                "vendor_name": "Vendor A",
                "vendor_email": "sales@vendora.com",
                "parts": ["LM317T"],
                "subject": "RFQ",
                "body": "Original body",
            }
        ]

        # rephrase_rfq returns an Exception object (return_exceptions=True)
        mock_rephrase = AsyncMock(side_effect=ValueError("oops"))

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value="fake-key"),
            patch("app.services.ai_service.rephrase_rfq", mock_rephrase),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            results = await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                requisition_id=test_requisition.id,
                vendor_groups=vendor_groups,
            )

        # Should still succeed with original body
        assert len(results) == 1
        assert results[0]["status"] == "sent"

    @pytest.mark.asyncio
    async def test_ai_rephrase_returns_empty_string(self, db_session, test_user, test_requisition):
        """When rephrase returns empty string, original body is kept."""
        mock_gc = AsyncMock()
        mock_gc.post_json.return_value = {}
        mock_gc.get_json.return_value = {"value": []}

        vendor_groups = [
            {
                "vendor_name": "Vendor A",
                "vendor_email": "sales@vendora.com",
                "parts": ["LM317T"],
                "subject": "RFQ",
                "body": "Original body",
            }
        ]

        mock_rephrase = AsyncMock(return_value="")

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value="fake-key"),
            patch("app.services.ai_service.rephrase_rfq", mock_rephrase),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            results = await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                requisition_id=test_requisition.id,
                vendor_groups=vendor_groups,
            )

        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_multiple_vendors(self, db_session, test_user, test_requisition):
        mock_gc = AsyncMock()
        mock_gc.post_json.return_value = {}
        mock_gc.get_json.return_value = {"value": []}

        vendor_groups = [
            {
                "vendor_name": f"Vendor {i}",
                "vendor_email": f"sales@vendor{i}.com",
                "parts": ["LM317T"],
                "subject": "RFQ",
                "body": f"Body {i}",
            }
            for i in range(3)
        ]

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            results = await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                requisition_id=test_requisition.id,
                vendor_groups=vendor_groups,
            )

        assert len(results) == 3
        assert mock_gc.post_json.call_count == 3

    @pytest.mark.asyncio
    async def test_lookup_sets_graph_ids(self, db_session, test_user, test_requisition):
        """After sending, look up sent message and set graph IDs."""
        mock_gc = AsyncMock()
        mock_gc.post_json.return_value = {}
        tagged_subject = f"RFQ [ref:{test_requisition.id}]"
        mock_gc.get_json.return_value = {
            "value": [_sent_item("sent-msg-100", "conv-100", tagged_subject, recipient="sales@vendora.com")]
        }

        vendor_groups = [
            {
                "vendor_name": "Vendor A",
                "vendor_email": "sales@vendora.com",
                "parts": ["LM317T"],
                "subject": "RFQ",
                "body": "Quote please",
            }
        ]

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            results = await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                requisition_id=test_requisition.id,
                vendor_groups=vendor_groups,
            )

        contact_id = results[0]["id"]
        contact = db_session.get(Contact, contact_id)
        assert contact.graph_message_id == "sent-msg-100"
        assert contact.graph_conversation_id == "conv-100"

    @pytest.mark.asyncio
    async def test_lookup_exception_ignored(self, db_session, test_user, test_requisition):
        """If lookup fails, contact is still created without graph IDs."""
        mock_gc = AsyncMock()
        mock_gc.post_json.return_value = {}
        mock_gc.get_json.side_effect = Exception("Lookup failed")

        vendor_groups = [
            {
                "vendor_name": "Vendor A",
                "vendor_email": "sales@vendora.com",
                "parts": ["LM317T"],
                "subject": "RFQ",
                "body": "Quote please",
            }
        ]

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            results = await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                requisition_id=test_requisition.id,
                vendor_groups=vendor_groups,
            )

        assert len(results) == 1
        assert results[0]["status"] == "sent"
        contact_id = results[0]["id"]
        contact = db_session.get(Contact, contact_id)
        assert contact.graph_message_id is None

    @pytest.mark.asyncio
    async def test_lookup_no_match_logs_and_leaves_graph_ids_null(self, db_session, test_user, test_requisition):
        """F5: when the sent message is not found in the $top Sent window (e.g. pushed
        below it by a large batch), the Contact keeps NULL graph ids AND the dropped
        association is OBSERVABLE (a warning at the call site naming the vendor email),
        not silent."""
        mock_gc = AsyncMock()
        mock_gc.post_json.return_value = {}
        # Sent folder returns messages, but none match the recipient → no-match (not an error)
        mock_gc.get_json.return_value = {
            "value": [_sent_item("other-msg", "other-conv", "RFQ [ref:999]", recipient="someone-else@x.com")]
        }

        vendor_groups = [
            {
                "vendor_name": "Vendor A",
                "vendor_email": "sales@vendora.com",
                "parts": ["LM317T"],
                "subject": "RFQ",
                "body": "Quote please",
            }
        ]

        # Loguru bypasses stdlib logging propagation, so spy on the module logger directly.
        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch("app.email_service.logger.warning") as mock_warn,
        ):
            results = await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                requisition_id=test_requisition.id,
                vendor_groups=vendor_groups,
            )

        assert results[0]["status"] == "sent"
        contact = db_session.get(Contact, results[0]["id"])
        assert contact.graph_message_id is None
        assert contact.graph_conversation_id is None
        # The dropped graph-id association is logged with the vendor email — observable.
        # logger.warning(template, *args) → flatten the call args and look for the email.
        logged = " ".join(
            str(a) for call in mock_warn.call_args_list for a in (call.args + tuple(call.kwargs.values()))
        )
        assert "sales@vendora.com" in logged

    @pytest.mark.asyncio
    async def test_both_requisition_inputs_raise(self, db_session, test_user, test_requisition):
        """F11: requisition_id and requisition_parts_map are mutually exclusive modes —
        passing both must fail loudly instead of silently ignoring the scalar."""
        with patch("app.utils.graph_client.GraphClient", return_value=AsyncMock()):
            with pytest.raises(ValueError, match="mutually exclusive"):
                await send_batch_rfq(
                    token="fake-token",
                    db=db_session,
                    user_id=test_user.id,
                    requisition_id=test_requisition.id,
                    requisition_parts_map={test_requisition.id: [{"mpn": "X", "qty": 1}]},
                    vendor_groups=[],
                )

    @pytest.mark.asyncio
    async def test_group_without_body_still_sends(self, db_session, test_user, test_requisition):
        """Groups with empty body are still sent (rephrase removed in cost
        reduction)."""
        mock_gc = AsyncMock()
        mock_gc.post_json.return_value = {}
        mock_gc.get_json.return_value = {"value": []}

        vendor_groups = [
            {
                "vendor_name": "Vendor A",
                "vendor_email": "sales@vendora.com",
                "parts": ["LM317T"],
                "subject": "RFQ",
                "body": "",
            },
            {
                "vendor_name": "Vendor B",
                "vendor_email": "sales@vendorb.com",
                "parts": ["LM317T"],
                "subject": "RFQ",
                "body": "Actual body",
            },
        ]

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value="fake-key"),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            results = await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                requisition_id=test_requisition.id,
                vendor_groups=vendor_groups,
            )

        # Both groups sent (no rephrase gating)
        assert mock_gc.post_json.call_count == 2


# ── send_batch_rfq: cross-requisition tracking ───────────────────────


def _two_requisitions(db_session, test_user):
    """Two requisitions, one requirement each, for cross-requisition sends."""
    reqs = []
    for i, mpn in enumerate(["CROSS-MPN-A", "CROSS-MPN-B"]):
        req = Requisition(
            name=f"REQ-CROSS-{i}",
            customer_name="Acme Electronics",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(req)
        db_session.flush()
        db_session.add(
            Requirement(
                requisition_id=req.id,
                primary_mpn=mpn,
                target_qty=100 * (i + 1),
                created_at=datetime.now(UTC),
            )
        )
        reqs.append(req)
    db_session.commit()
    return reqs


class TestSendBatchRfqCrossRequisition:
    """Per-requisition Contact fan-out (requisition_parts_map) in send_batch_rfq.

    One email per vendor; one Contact per (requisition, vendor) sharing that email's
    graph ids; subject carries one [ref:{id}] token per requisition in ascending
    requisition-id order. The legacy scalar requisition_id shape (htmx_views.rfq_send)
    stays byte-identical.
    """

    @pytest.mark.asyncio
    async def test_multi_requisition_contact_fanout(self, db_session, test_user):
        """The REAL Graph collision scenario: every vendor in the batch shares one
        identical tagged subject, and EVERY sent-items lookup sees the SAME list
        (both messages). Only the toRecipients discrimination can hand each
        vendor its own graph ids — a subject-only match would give every contact
        the newest message's conversation id (the F1 misattribution bug)."""
        req_a, req_b = _two_requisitions(db_session, test_user)
        parts_map = {
            req_a.id: [{"mpn": "CROSS-MPN-A", "qty": 100}],
            req_b.id: [{"mpn": "CROSS-MPN-B", "qty": 200}],
        }
        expected_tokens = f"[ref:{req_a.id}] [ref:{req_b.id}]"
        tagged = f"RFQ {expected_tokens}"

        mock_gc = AsyncMock()
        mock_gc.post_json.return_value = {}
        # ONE shared sent-items list for every lookup — identical subjects, newest
        # (Vendor B's) first, exactly as Graph returns after a batch send.
        mock_gc.get_json.return_value = {
            "value": [
                _sent_item("sent-b", "conv-vendor-b", tagged, recipient="b@vendorb.com"),
                _sent_item("sent-a", "conv-vendor-a", tagged, recipient="a@vendora.com"),
            ]
        }

        vendor_groups = [
            {"vendor_name": "Vendor A", "vendor_email": "a@vendora.com", "subject": "RFQ", "body": "Quote please"},
            {"vendor_name": "Vendor B", "vendor_email": "b@vendorb.com", "subject": "RFQ", "body": "Quote please"},
        ]

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            results = await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                vendor_groups=vendor_groups,
                requisition_parts_map=parts_map,
            )

        assert [r["status"] for r in results] == ["sent", "sent"]

        contacts = db_session.query(Contact).order_by(Contact.id).all()
        assert len(contacts) == 4  # 2 requisitions x 2 vendors
        pairs = {(c.requisition_id, c.vendor_name) for c in contacts}
        assert pairs == {
            (req_a.id, "Vendor A"),
            (req_b.id, "Vendor A"),
            (req_a.id, "Vendor B"),
            (req_b.id, "Vendor B"),
        }
        for c in contacts:
            # parts_included scoped to that contact's own requisition
            assert c.parts_included == parts_map[c.requisition_id]
            # every row carries the full multi-token subject
            assert expected_tokens in c.subject
        # Graph ids shared per vendor (same email), distinct across vendors —
        # despite every lookup seeing the same same-subject list.
        by_vendor: dict = {}
        for c in contacts:
            by_vendor.setdefault(c.vendor_name, set()).add((c.graph_message_id, c.graph_conversation_id))
        assert by_vendor["Vendor A"] == {("sent-a", "conv-vendor-a")}
        assert by_vendor["Vendor B"] == {("sent-b", "conv-vendor-b")}

    @pytest.mark.asyncio
    async def test_same_subject_batch_reply_progresses_only_replying_vendor(self, db_session, test_user):
        """End-to-end F1 pin: two vendors, same subject, shared sent-lookup → a reply
        from one vendor progresses ONLY that vendor's contacts (on both
        requisitions), never the other's. The replier is Vendor B — the NEWEST
        sent item — whose conversation id the old subject-only lookup stamped on
        EVERY vendor's contacts (so pre-fix, B's reply progressed A too). The
        reply subject carries no tokens (vendors often trim them), isolating
        Tier-1."""
        req_a, req_b = _two_requisitions(db_session, test_user)
        parts_map = {
            req_a.id: [{"mpn": "CROSS-MPN-A", "qty": 100}],
            req_b.id: [{"mpn": "CROSS-MPN-B", "qty": 200}],
        }
        tagged = f"RFQ [ref:{req_a.id}] [ref:{req_b.id}]"

        send_gc = AsyncMock()
        send_gc.post_json.return_value = {}
        send_gc.get_json.return_value = {
            "value": [
                _sent_item("sent-b", "conv-vendor-b", tagged, recipient="b@vendorb.com"),
                _sent_item("sent-a", "conv-vendor-a", tagged, recipient="a@vendora.com"),
            ]
        }
        vendor_groups = [
            {"vendor_name": "Vendor A", "vendor_email": "a@vendora.com", "subject": "RFQ", "body": "Quote"},
            {"vendor_name": "Vendor B", "vendor_email": "b@vendorb.com", "subject": "RFQ", "body": "Quote"},
        ]

        with (
            patch("app.utils.graph_client.GraphClient", return_value=send_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                vendor_groups=vendor_groups,
                requisition_parts_map=parts_map,
            )

        poll_gc = AsyncMock()
        poll_gc.get_json.return_value = {
            "value": [
                {
                    "id": "reply-b",
                    "subject": "RE: RFQ",  # token-stripped reply: Tier-1 only
                    "from": {"emailAddress": {"address": "b@vendorb.com", "name": "Vendor B"}},
                    "bodyPreview": "Quote attached",
                    "body": {"content": "<p>Quote attached</p>"},
                    "conversationId": "conv-vendor-b",
                    "receivedDateTime": None,
                }
            ]
        }
        with (
            patch("app.utils.graph_client.GraphClient", return_value=poll_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(token="fake-token", db=db_session)

        assert len(results) == 1
        contacts = db_session.query(Contact).order_by(Contact.id).all()
        a_contacts = [c for c in contacts if c.vendor_name == "Vendor A"]
        b_contacts = [c for c in contacts if c.vendor_name == "Vendor B"]
        # Vendor B progressed on BOTH requisitions; Vendor A untouched on both.
        assert {c.requisition_id for c in b_contacts} == {req_a.id, req_b.id}
        assert all(c.status == "responded" for c in b_contacts)
        assert all(c.status == "sent" for c in a_contacts)
        # The VendorResponse anchors to one of B's contacts, never A's.
        vr = db_session.query(VendorResponse).one()
        assert vr.contact_id in {c.id for c in b_contacts}

    @pytest.mark.asyncio
    async def test_subject_tokens_sorted_by_requisition_id(self, db_session, test_user):
        """Token order is deterministic (ascending requisition id), regardless of map
        insertion order."""
        req_a, req_b = _two_requisitions(db_session, test_user)
        # Deliberately reversed insertion order.
        parts_map = {
            req_b.id: [{"mpn": "CROSS-MPN-B", "qty": 200}],
            req_a.id: [{"mpn": "CROSS-MPN-A", "qty": 100}],
        }

        mock_gc = AsyncMock()
        mock_gc.post_json.return_value = {}
        mock_gc.get_json.return_value = {"value": []}

        vendor_groups = [
            {"vendor_name": "Vendor A", "vendor_email": "a@vendora.com", "subject": "RFQ", "body": "Quote"},
        ]

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                vendor_groups=vendor_groups,
                requisition_parts_map=parts_map,
            )

        payload = mock_gc.post_json.call_args[0][1]
        assert payload["message"]["subject"] == f"RFQ [ref:{req_a.id}] [ref:{req_b.id}]"

    @pytest.mark.asyncio
    async def test_legacy_scalar_requisition_id_shape_unchanged(self, db_session, test_user, test_requisition):
        """Regression: the htmx_views.rfq_send call shape (scalar requisition_id,
        parts as TEXT) is byte-identical — one Contact, single token, parts passed
        through untouched, parts_count keeps its historical len() semantics."""
        mock_gc = AsyncMock()
        mock_gc.post_json.return_value = {}
        mock_gc.get_json.return_value = {"value": []}

        vendor_groups = [
            {
                "vendor_name": "Vendor A",
                "vendor_email": "a@vendora.com",
                "parts": "LM317T x100",  # rfq_send passes a parts_summary STRING
                "subject": "RFQ - Req",
                "body": "Quote please",
            }
        ]

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            results = await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                requisition_id=test_requisition.id,
                vendor_groups=vendor_groups,
            )

        contacts = db_session.query(Contact).all()
        assert len(contacts) == 1
        c = contacts[0]
        assert c.requisition_id == test_requisition.id
        assert c.parts_included == "LM317T x100"
        assert c.subject == f"RFQ - Req [ref:{test_requisition.id}]"
        assert results[0]["status"] == "sent"
        assert results[0]["parts_count"] == len("LM317T x100")

    @pytest.mark.asyncio
    async def test_failed_send_creates_contact_per_requisition(self, db_session, test_user):
        """A failed vendor send still records a Contact on EVERY involved requisition
        (the failure must be visible on each one's history)."""
        req_a, req_b = _two_requisitions(db_session, test_user)
        parts_map = {
            req_a.id: [{"mpn": "CROSS-MPN-A", "qty": 100}],
            req_b.id: [{"mpn": "CROSS-MPN-B", "qty": 200}],
        }

        mock_gc = AsyncMock()
        mock_gc.post_json.side_effect = Exception("SMTP Error")

        vendor_groups = [
            {"vendor_name": "Vendor A", "vendor_email": "a@vendora.com", "subject": "RFQ", "body": "Quote"},
        ]

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                vendor_groups=vendor_groups,
                requisition_parts_map=parts_map,
            )

        assert len(results) == 1
        assert results[0]["status"] == "failed"
        contacts = db_session.query(Contact).order_by(Contact.id).all()
        assert len(contacts) == 2
        assert {c.requisition_id for c in contacts} == {req_a.id, req_b.id}
        for c in contacts:
            assert c.status == "failed"
            assert "SMTP Error" in c.error_message
            assert c.parts_included == parts_map[c.requisition_id]

    @pytest.mark.asyncio
    async def test_tag_propagation_covers_all_requisitions(self, db_session, test_user):
        """The tag-propagation block reads requirements from ALL involved requisitions,
        not just one."""
        from app.models import MaterialCard, VendorCard

        req_a, req_b = _two_requisitions(db_session, test_user)
        cards = []
        for req in (req_a, req_b):
            card = MaterialCard(normalized_mpn=f"CARD-{req.id}", display_mpn=f"CARD-{req.id}")
            db_session.add(card)
            db_session.flush()
            requirement = db_session.query(Requirement).filter(Requirement.requisition_id == req.id).one()
            requirement.material_card_id = card.id
            cards.append(card)
        db_session.add(
            VendorCard(
                normalized_name="vendor a",
                display_name="Vendor A",
                created_at=datetime.now(UTC),
            )
        )
        db_session.commit()

        parts_map = {
            req_a.id: [{"mpn": "CROSS-MPN-A", "qty": 100}],
            req_b.id: [{"mpn": "CROSS-MPN-B", "qty": 200}],
        }

        mock_gc = AsyncMock()
        mock_gc.post_json.return_value = {}
        mock_gc.get_json.return_value = {"value": []}
        mock_propagate = MagicMock()

        vendor_groups = [
            {"vendor_name": "Vendor A", "vendor_email": "a@vendora.com", "subject": "RFQ", "body": "Quote"},
        ]

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
            patch("app.services.tagging.propagate_tags_to_entity", mock_propagate),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                vendor_groups=vendor_groups,
                requisition_parts_map=parts_map,
            )

        propagated_card_ids = {call.args[2] for call in mock_propagate.call_args_list}
        assert propagated_card_ids == {cards[0].id, cards[1].id}

    @pytest.mark.asyncio
    async def test_contact_tracking_failure_isolated_per_vendor(self, db_session, test_user):
        """F3: a Contact-creation failure for one vendor must not poison the batch —
        the other vendor's rows persist, and the broken vendor is reported as a
        visible tracking error (its email DID go out), never an exception."""
        import app.email_service as es

        req_a, req_b = _two_requisitions(db_session, test_user)
        parts_map = {
            req_a.id: [{"mpn": "CROSS-MPN-A", "qty": 100}],
            req_b.id: [{"mpn": "CROSS-MPN-B", "qty": 200}],
        }

        mock_gc = AsyncMock()
        mock_gc.post_json.return_value = {}
        mock_gc.get_json.return_value = {"value": []}

        vendor_groups = [
            {"vendor_name": "Vendor A", "vendor_email": "a@vendora.com", "subject": "RFQ", "body": "Quote"},
            {"vendor_name": "Vendor B", "vendor_email": "b@vendorb.com", "subject": "RFQ", "body": "Quote"},
        ]

        real_create = es._create_contact

        def exploding_create(db, rid, user_id, vendor_name, *args, **kwargs):
            if vendor_name == "Vendor B":
                raise RuntimeError("simulated flush failure")
            return real_create(db, rid, user_id, vendor_name, *args, **kwargs)

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
            patch("app.email_service._create_contact", side_effect=exploding_create),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            results = await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                vendor_groups=vendor_groups,
                requisition_parts_map=parts_map,
            )

        by_vendor = {r["vendor_name"]: r for r in results}
        assert by_vendor["Vendor A"]["status"] == "sent"
        assert by_vendor["Vendor B"]["status"] == "failed"
        assert "tracking_error" in by_vendor["Vendor B"]["error"]
        # Vendor A's per-requisition rows persisted despite Vendor B's failure.
        contacts = db_session.query(Contact).order_by(Contact.id).all()
        assert {(c.requisition_id, c.vendor_name) for c in contacts} == {
            (req_a.id, "Vendor A"),
            (req_b.id, "Vendor A"),
        }

    @pytest.mark.asyncio
    async def test_legacy_none_requisition_creates_no_contacts(self, db_session, test_user):
        """F12: Contact.requisition_id is NOT NULL — a degenerate legacy call with
        neither a scalar requisition_id nor a parts map sends the email but writes
        no Contact rows (instead of crashing on the NULL flush)."""
        mock_gc = AsyncMock()
        mock_gc.post_json.return_value = {}
        mock_gc.get_json.return_value = {"value": []}

        vendor_groups = [
            {
                "vendor_name": "Vendor A",
                "vendor_email": "a@vendora.com",
                "parts": ["LM317T"],
                "subject": "RFQ",
                "body": "Quote",
            },
        ]

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            results = await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                vendor_groups=vendor_groups,
            )

        assert results[0]["status"] == "sent"
        assert results[0]["id"] is None
        assert db_session.query(Contact).count() == 0


# ── parse_response_ai ────────────────────────────────────────────────


class TestParseResponseAi:
    @pytest.mark.asyncio
    async def test_successful_parse(self):
        mock_result = {
            "overall_sentiment": "positive",
            "parts": [{"mpn": "LM317T", "unit_price": 1.50}],
            "confidence": 0.9,
        }

        with patch(
            "app.services.response_parser.parse_vendor_response",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            result = await parse_response_ai("Quote body", "RE: RFQ")

        assert result is not None
        assert result["sentiment"] == "positive"
        assert len(result["parts"]) == 1
        assert result["confidence"] == 0.9

    @pytest.mark.asyncio
    async def test_returns_none(self):
        with patch(
            "app.services.response_parser.parse_vendor_response",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await parse_response_ai("Unrecognizable", "No Subject")

        assert result is None


# ── _apply_parsed_result ─────────────────────────────────────────────


class TestApplyParsedResult:
    def test_basic_apply(self):
        vr = MagicMock(spec=VendorResponse)
        vr.body = "Some quote text"
        vr.subject = "RE: RFQ"
        vr.needs_action = None
        vr.action_hint = None
        vr.classification = None
        vr.parsed_data = None
        vr.confidence = None
        vr.status = "new"

        parsed = {
            "sentiment": "positive",
            "parts": [{"unit_price": 1.50, "mpn": "LM317T"}],
            "confidence": 0.9,
        }

        _apply_parsed_result(vr, parsed)

        assert vr.parsed_data == parsed
        assert vr.confidence == 0.9
        assert vr.status == "parsed"
        assert vr.classification == "quote_provided"
        assert vr.needs_action is True

    def test_with_notification_created(self, db_session, test_user, test_requisition):
        """When draft offers are extracted, offer_pending_review ActivityLog is
        created."""
        from app.models import Requirement

        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()

        vr = VendorResponse(
            requisition_id=test_requisition.id,
            vendor_name="Vendor X",
            vendor_email="vendor@x.com",
            subject="RE: RFQ",
            body="We can offer LM317T at $0.50 each, 1000 pcs available.",
            scanned_by_user_id=test_user.id,
            status="new",
            created_at=datetime.now(UTC),
        )
        db_session.add(vr)
        db_session.flush()

        parsed = {
            "sentiment": "positive",
            "parts": [{"mpn": "LM317T", "unit_price": 0.50, "qty": 1000, "status": "quoted"}],
            "confidence": 0.65,
        }

        _apply_parsed_result(vr, parsed, db_session)
        db_session.flush()

        # Check offer_pending_review notification was created
        activities = db_session.query(ActivityLog).filter(ActivityLog.activity_type == "offer_pending_review").all()
        assert len(activities) >= 1

    def test_no_notification_high_confidence(self):
        vr = MagicMock(spec=VendorResponse)
        vr.body = "Quote: $1.50 each"
        vr.subject = "RE: RFQ"
        vr.needs_action = None
        vr.requisition_id = 1

        parsed = {"parts": [{"unit_price": 1.50}], "confidence": 0.95}

        db = MagicMock()
        _apply_parsed_result(vr, parsed, db)

        # confidence > 0.8, so no ActivityLog
        db.add.assert_not_called()

    def test_no_notification_low_confidence(self):
        vr = MagicMock(spec=VendorResponse)
        vr.body = "we got something"
        vr.subject = "RE: RFQ"
        vr.needs_action = None

        parsed = {"parts": [], "confidence": 0.3}

        db = MagicMock()
        _apply_parsed_result(vr, parsed, db)

        # confidence < 0.5, so no ActivityLog
        db.add.assert_not_called()

    def test_notification_with_requisition_owner(self, db_session, test_user, test_requisition):
        """Notification goes to requisition owner when draft offers extracted."""
        scanner = User(
            email="scanner@trioscs.com",
            name="Scanner",
            role="buyer",
            azure_id="scanner-azure-id",
            created_at=datetime.now(UTC),
        )
        db_session.add(scanner)
        db_session.flush()

        vr = VendorResponse(
            requisition_id=test_requisition.id,
            vendor_name="Vendor Y",
            vendor_email="vendor@y.com",
            subject="RE: RFQ",
            body="We can offer LM317T at $0.75 each",
            scanned_by_user_id=scanner.id,
            status="new",
            created_at=datetime.now(UTC),
        )
        db_session.add(vr)
        db_session.flush()

        parsed = {"parts": [{"mpn": "LM317T", "unit_price": 0.75, "qty": 500, "status": "quoted"}], "confidence": 0.6}

        _apply_parsed_result(vr, parsed, db_session)
        db_session.flush()

        activity = db_session.query(ActivityLog).filter(ActivityLog.activity_type == "offer_pending_review").first()
        assert activity is not None
        # Owner should be the requisition creator, not scanner
        assert activity.user_id == test_user.id

    def test_no_notification_without_offers(self, db_session, test_user, test_requisition):
        """No offer_pending_review notification when no draft offers extracted."""
        vr = VendorResponse(
            requisition_id=test_requisition.id,
            vendor_name="Vendor Z",
            vendor_email="vendor@z.com",
            subject="RE: RFQ",
            body="We have something for you. Is this what you need?",
            scanned_by_user_id=test_user.id,
            status="new",
            created_at=datetime.now(UTC),
        )
        db_session.add(vr)
        db_session.flush()

        parsed = {"parts": [], "confidence": 0.7}

        _apply_parsed_result(vr, parsed, db_session)
        db_session.flush()

        activity = db_session.query(ActivityLog).filter(ActivityLog.activity_type == "offer_pending_review").first()
        # No offers extracted, so no notification
        assert activity is None

    def test_notification_exception_logged_and_rolled_back(self):
        """If ActivityLog creation fails, exception is logged and rolled back."""
        vr = MagicMock(spec=VendorResponse)
        vr.body = "Something? Anything?"
        vr.subject = "RE: RFQ"
        vr.needs_action = True
        vr.confidence = 0.65
        vr.requisition_id = 1
        vr.scanned_by_user_id = 1
        vr.vendor_name = "Test"
        vr.action_hint = "Review"

        db = MagicMock()
        db.get.side_effect = Exception("DB error")

        parsed = {"parts": [], "confidence": 0.65}

        # _apply_parsed_result itself does field assignment; the exception
        # from _auto_create_offers is caught by the caller (poll_inbox)
        _apply_parsed_result(vr, parsed, db)
        assert vr.status == "parsed"

    def test_no_db_no_notification(self):
        """When db is None, no notification attempt."""
        vr = MagicMock(spec=VendorResponse)
        vr.body = "Question?"
        vr.subject = "RE: RFQ"

        parsed = {"parts": [], "confidence": 0.65}

        _apply_parsed_result(vr, parsed, None)
        assert vr.status == "parsed"

    def test_no_needs_action_no_notification(self):
        """When needs_action is False, no notification."""
        vr = MagicMock(spec=VendorResponse)
        vr.body = "Out of office automatic reply"
        vr.subject = "Automatic Reply"

        parsed = {"parts": [], "confidence": 0.65}

        db = MagicMock()
        _apply_parsed_result(vr, parsed, db)

        # ooo_bounce -> needs_action=False
        db.add.assert_not_called()


# ── _submit_parse_batch ──────────────────────────────────────────────


class TestSubmitParseBatch:
    @pytest.mark.asyncio
    async def test_successful_batch_submit(self, db_session, test_user, test_requisition):
        vr = VendorResponse(
            requisition_id=test_requisition.id,
            vendor_name="Vendor X",
            vendor_email="vendor@x.com",
            subject="RE: RFQ",
            body="Here is the quote",
            status="new",
            created_at=datetime.now(UTC),
        )
        db_session.add(vr)
        db_session.flush()

        with (
            patch(
                "app.utils.claude_client.claude_batch_submit",
                new_callable=AsyncMock,
                return_value="batch-123",
            ),
            patch("app.services.response_parser.clean_email_body", return_value="cleaned"),
            patch("app.services.response_parser.RESPONSE_PARSE_SCHEMA", {"type": "object"}),
            patch("app.services.response_parser.SYSTEM_PROMPT", "System prompt"),
        ):
            await _submit_parse_batch([vr], db_session)

        db_session.flush()
        pb = db_session.query(PendingBatch).first()
        assert pb is not None
        assert pb.batch_id == "batch-123"
        assert pb.batch_type == "inbox_parse"
        assert pb.status == "processing"

    @pytest.mark.asyncio
    async def test_no_batch_id_raises(self, db_session, test_user, test_requisition):
        vr = VendorResponse(
            requisition_id=test_requisition.id,
            vendor_name="Vendor X",
            vendor_email="vendor@x.com",
            subject="RE: RFQ",
            body="Here is the quote",
            status="new",
            created_at=datetime.now(UTC),
        )
        db_session.add(vr)
        db_session.flush()

        with (
            patch(
                "app.utils.claude_client.claude_batch_submit",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("app.services.response_parser.clean_email_body", return_value="cleaned"),
            patch("app.services.response_parser.RESPONSE_PARSE_SCHEMA", {"type": "object"}),
            patch("app.services.response_parser.SYSTEM_PROMPT", "System prompt"),
            pytest.raises(RuntimeError, match="no batch_id"),
        ):
            await _submit_parse_batch([vr], db_session)


# ── Email-mining daily budget cap (Wave 6) ───────────────────────────


class TestEmailMiningBudgetCap:
    """The daily Claude-spend cap on the email-mining inbox-parse batch path."""

    def test_cap_disabled_returns_all(self, monkeypatch):
        """Cap <= 0 disables the cap (graceful default = pre-cap unbounded behavior)."""
        from app.config import settings

        pending = [MagicMock() for _ in range(5)]
        for disabled in (0, -1):
            monkeypatch.setattr(settings, "email_mining_batch_daily_cap", disabled)
            # _email_mining_calls_today must NOT even be consulted when the cap is off.
            with patch("app.email_service._email_mining_calls_today", side_effect=AssertionError):
                assert _enforce_email_mining_cap(pending) == pending

    def test_under_cap_returns_all(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "email_mining_batch_daily_cap", 10)
        pending = [MagicMock() for _ in range(3)]
        with patch("app.email_service._email_mining_calls_today", return_value=0):
            assert _enforce_email_mining_cap(pending) == pending

    def test_at_cap_returns_empty_and_logs(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "email_mining_batch_daily_cap", 5)
        pending = [MagicMock() for _ in range(4)]
        with (
            patch("app.email_service._email_mining_calls_today", return_value=5),
            patch("app.email_service.logger.warning") as mock_warn,
        ):
            assert _enforce_email_mining_cap(pending) == []
        mock_warn.assert_called_once()
        assert "cap reached" in mock_warn.call_args[0][0]

    def test_over_cap_returns_empty(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "email_mining_batch_daily_cap", 5)
        pending = [MagicMock() for _ in range(4)]
        with patch("app.email_service._email_mining_calls_today", return_value=99):
            assert _enforce_email_mining_cap(pending) == []

    def test_trims_to_remaining_budget(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "email_mining_batch_daily_cap", 10)
        pending = [MagicMock() for _ in range(5)]
        with (
            patch("app.email_service._email_mining_calls_today", return_value=8),
            patch("app.email_service.logger.warning") as mock_warn,
        ):
            result = _enforce_email_mining_cap(pending)
        # remaining = 10 - 8 = 2; highest-volume order preserved
        assert result == pending[:2]
        mock_warn.assert_called_once()

    def test_calls_today_is_max_of_metered_and_submitted(self):
        """Today's count reconciles the metered claude_usage calls with the submit
        counter."""

        def fake_count(key):
            return {
                "claude_usage:email_mining:fast:calls:": 3,  # metered (fast tier)
                "email_mining:batch:submitted:": 7,  # submit-time counter
            }.get(
                next(
                    (
                        p
                        for p in (
                            "claude_usage:email_mining:fast:calls:",
                            "email_mining:batch:submitted:",
                        )
                        if key.startswith(p)
                    ),
                    "",
                ),
                0,
            )

        with patch("app.cache.intel_cache.get_count", side_effect=fake_count):
            # metered total = 3 (fast) + 0 (smart) + 0 (opus) = 3; submitted = 7 -> max 7
            assert _email_mining_calls_today() == 7

    def test_calls_today_swallows_cache_errors(self):
        with patch("app.cache.intel_cache.get_count", side_effect=RuntimeError("redis down")):
            assert _email_mining_calls_today() == 0

    def test_record_calls_increments_counter(self):
        with patch("app.cache.intel_cache.incr_count") as mock_incr:
            _record_email_mining_calls(4)
        mock_incr.assert_called_once()
        assert mock_incr.call_args.kwargs.get("amount") == 4

    def test_record_calls_noop_on_zero(self):
        with patch("app.cache.intel_cache.incr_count") as mock_incr:
            _record_email_mining_calls(0)
        mock_incr.assert_not_called()


class TestPollInboxBudgetCap:
    """poll_inbox enforces the cap before BOTH the batch and its sequential fallback."""

    def _make_inbox_message(self):
        return {
            "id": "cap-msg-1",
            "subject": "RE: RFQ",
            "from": {"emailAddress": {"address": "vendor@parts.com", "name": "Vendor"}},
            "bodyPreview": "Quote attached",
            "body": {"content": "<p>Quote attached</p>"},
            "conversationId": "cap-conv-1",
            "receivedDateTime": None,
        }

    @pytest.mark.asyncio
    async def test_under_cap_submits_batch(self, db_session, test_user, test_requisition, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "email_mining_batch_daily_cap", 1000)
        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {"value": [self._make_inbox_message()]}

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value="fake-key"),
            patch("app.email_service._email_mining_calls_today", return_value=0),
            patch("app.cache.intel_cache.incr_count") as mock_incr,
            patch(
                "app.utils.claude_client.claude_batch_submit",
                new_callable=AsyncMock,
                return_value="batch-cap-1",
            ) as mock_submit,
            patch("app.services.response_parser.clean_email_body", return_value="cleaned"),
            patch("app.services.response_parser.RESPONSE_PARSE_SCHEMA", {"type": "object"}),
            patch("app.services.response_parser.SYSTEM_PROMPT", "System prompt"),
        ):
            results = await poll_inbox(token="fake-token", db=db_session, requisition_id=test_requisition.id)

        assert len(results) == 1
        mock_submit.assert_called_once()  # under cap -> batch dispatched
        mock_incr.assert_called_once()  # today's submit counter billed

    @pytest.mark.asyncio
    async def test_over_cap_skips_batch_and_fallback(self, db_session, test_user, test_requisition, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "email_mining_batch_daily_cap", 5)
        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {"value": [self._make_inbox_message()]}

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value="fake-key"),
            patch("app.email_service._email_mining_calls_today", return_value=5),
            patch(
                "app.utils.claude_client.claude_batch_submit",
                new_callable=AsyncMock,
            ) as mock_submit,
            patch("app.email_service._parse_sequential_fallback", new_callable=AsyncMock) as mock_seq,
        ):
            results = await poll_inbox(token="fake-token", db=db_session, requisition_id=test_requisition.id)

        # Raw response row is still saved (data not lost), but NO Claude spend occurs.
        assert len(results) == 1
        mock_submit.assert_not_called()
        mock_seq.assert_not_called()


# ── _parse_sequential_fallback ───────────────────────────────────────


class TestParseSequentialFallback:
    @pytest.mark.asyncio
    async def test_successful_parse(self):
        vr = MagicMock(spec=VendorResponse)
        vr.id = 1
        vr.body = "Quote body"
        vr.subject = "RE: RFQ"

        mock_parsed = {
            "sentiment": "positive",
            "parts": [{"unit_price": 1.50}],
            "confidence": 0.9,
        }

        with patch(
            "app.email_service.parse_response_ai",
            new_callable=AsyncMock,
            return_value=mock_parsed,
        ):
            await _parse_sequential_fallback([vr], MagicMock())

        assert vr.status == "parsed"

    @pytest.mark.asyncio
    async def test_parse_returns_none(self):
        vr = MagicMock(spec=VendorResponse)
        vr.id = 1
        vr.body = "Unrecognizable"
        vr.subject = ""
        vr.status = "new"

        with patch(
            "app.email_service.parse_response_ai",
            new_callable=AsyncMock,
            return_value=None,
        ):
            await _parse_sequential_fallback([vr], MagicMock())

        # Status not changed since parse returned None
        assert vr.status == "new"

    @pytest.mark.asyncio
    async def test_parse_exception_swallowed(self):
        vr = MagicMock(spec=VendorResponse)
        vr.id = 1
        vr.body = "body"
        vr.subject = "subject"
        vr.status = "new"

        with patch(
            "app.email_service.parse_response_ai",
            new_callable=AsyncMock,
            side_effect=Exception("AI broken"),
        ):
            # Should not raise
            await _parse_sequential_fallback([vr], MagicMock())

        assert vr.status == "new"

    @pytest.mark.asyncio
    async def test_multiple_items_with_semaphore(self):
        vrs = []
        for i in range(7):
            vr = MagicMock(spec=VendorResponse)
            vr.id = i
            vr.body = f"Body {i}"
            vr.subject = f"Subject {i}"
            vrs.append(vr)

        mock_parsed = {"parts": [{"unit_price": 1.0}], "confidence": 0.9}

        with patch(
            "app.email_service.parse_response_ai",
            new_callable=AsyncMock,
            return_value=mock_parsed,
        ):
            await _parse_sequential_fallback(vrs, MagicMock())

        for vr in vrs:
            assert vr.status == "parsed"


# ── poll_inbox ───────────────────────────────────────────────────────


class TestPollInbox:
    def _make_inbox_message(
        self,
        msg_id="msg-1",
        sender_email="vendor@parts.com",
        sender_name="Vendor",
        subject="RE: RFQ",
        body_preview="Quote attached",
        body_content="<p>Quote attached</p>",
        conv_id="conv-1",
        received_at=None,
    ):
        return {
            "id": msg_id,
            "subject": subject,
            "from": {
                "emailAddress": {
                    "address": sender_email,
                    "name": sender_name,
                }
            },
            "bodyPreview": body_preview,
            "body": {"content": body_content},
            "conversationId": conv_id,
            "receivedDateTime": received_at,
        }

    @pytest.mark.asyncio
    async def test_fallback_fetch_new_message(self, db_session, test_user, test_requisition):
        """No delta -> fallback to full fetch, new unmatched message saved."""
        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {
            "value": [
                self._make_inbox_message(sender_email="vendor@parts.com"),
            ]
        }

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
            )

        assert len(results) == 1
        assert results[0]["vendor_email"] == "vendor@parts.com"

        # Check VendorResponse was saved
        vr = db_session.query(VendorResponse).first()
        assert vr is not None
        assert vr.message_id == "msg-1"

        # Check ProcessedMessage was saved
        pm = db_session.query(ProcessedMessage).first()
        assert pm is not None
        assert pm.message_id == "msg-1"

    @pytest.mark.asyncio
    async def test_delta_query_path(self, db_session, test_user, test_requisition):
        """When scanned_by_user_id is set, use delta query."""
        # Pre-create SyncState
        sync = SyncState(
            user_id=test_user.id,
            folder="inbox",
            delta_token="old-delta-token",
            last_sync_at=datetime.now(UTC),
        )
        db_session.add(sync)
        db_session.commit()

        mock_gc = AsyncMock()
        mock_gc.delta_query.return_value = (
            [self._make_inbox_message(sender_email="vendor@parts.com")],
            "new-delta-token",
        )

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
                scanned_by_user_id=test_user.id,
            )

        assert len(results) == 1
        # Delta token should be updated
        sync = db_session.query(SyncState).first()
        assert sync.delta_token == "new-delta-token"

    @pytest.mark.asyncio
    async def test_delta_query_creates_sync_state(self, db_session, test_user, test_requisition):
        """When no SyncState exists but delta succeeds, create one."""
        mock_gc = AsyncMock()
        mock_gc.delta_query.return_value = (
            [self._make_inbox_message()],
            "initial-delta-token",
        )

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
                scanned_by_user_id=test_user.id,
            )

        assert len(results) == 1
        sync = db_session.query(SyncState).first()
        assert sync is not None
        assert sync.delta_token == "initial-delta-token"

    @pytest.mark.asyncio
    async def test_delta_query_no_new_delta(self, db_session, test_user, test_requisition):
        """Delta returns no new token — existing sync state unchanged."""
        sync = SyncState(
            user_id=test_user.id,
            folder="inbox",
            delta_token="existing-token",
            last_sync_at=datetime.now(UTC),
        )
        db_session.add(sync)
        db_session.commit()

        mock_gc = AsyncMock()
        mock_gc.delta_query.return_value = (
            [self._make_inbox_message()],
            None,  # No new delta
        )

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
                scanned_by_user_id=test_user.id,
            )

        sync = db_session.query(SyncState).first()
        assert sync.delta_token == "existing-token"

    @pytest.mark.asyncio
    async def test_delta_expired_fallback(self, db_session, test_user, test_requisition):
        """When the delta token expires (410), fall back to full fetch."""
        mock_gc = AsyncMock()
        mock_gc.delta_query.side_effect = GraphSyncStateExpired("SyncStateNotFound")
        mock_gc.get_json.return_value = {"value": [self._make_inbox_message()]}

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
                scanned_by_user_id=test_user.id,
            )

        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_delta_expired_clears_stale_token(self, db_session, test_user, test_requisition):
        """A 410 (GraphSyncStateExpired) is the ONLY delta failure that clears the
        stored delta_token from SyncState."""
        sync = SyncState(
            user_id=test_user.id,
            folder="inbox",
            delta_token="stale-token-from-410",
            last_sync_at=datetime.now(UTC),
        )
        db_session.add(sync)
        db_session.commit()

        mock_gc = AsyncMock()
        mock_gc.delta_query.side_effect = GraphSyncStateExpired("410 SyncStateNotFound")
        mock_gc.get_json.return_value = {"value": [self._make_inbox_message()]}

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
                scanned_by_user_id=test_user.id,
            )

        assert len(results) == 1
        # Stale delta token must be cleared so next poll starts fresh
        sync = db_session.query(SyncState).first()
        assert sync.delta_token is None

    @pytest.mark.asyncio
    async def test_graph_error_page_keeps_delta_token_and_falls_back(self, db_session, test_user, test_requisition):
        """A typed Graph error page mid-delta must NOT clear the stored token —
        delta_query never advanced it, so the next poll resumes incrementally.

        This poll falls back to a full scan.
        """
        from app.utils.graph_client import GraphAPIError

        sync = SyncState(
            user_id=test_user.id,
            folder="inbox",
            delta_token="mid-round-token",
            last_sync_at=datetime.now(UTC),
        )
        db_session.add(sync)
        db_session.commit()

        mock_gc = AsyncMock()
        mock_gc.delta_query.side_effect = GraphAPIError(503, "max_retries")
        mock_gc.get_json.return_value = {"value": [self._make_inbox_message()]}

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
                scanned_by_user_id=test_user.id,
            )

        assert len(results) == 1  # fallback full scan still processed the inbox
        sync = db_session.query(SyncState).first()
        assert sync.delta_token == "mid-round-token"  # token kept, NOT cleared

    @pytest.mark.asyncio
    async def test_graph_error_page_auth_status_raises(self, db_session, test_user, test_requisition):
        """A 401 Graph error page raises (poll marked failed) instead of the old silent
        success with zero messages."""
        from app.utils.graph_client import GraphAPIError

        mock_gc = AsyncMock()
        mock_gc.delta_query.side_effect = GraphAPIError(401, "token expired")

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            with pytest.raises(GraphAPIError):
                await poll_inbox(
                    token="fake-token",
                    db=db_session,
                    requisition_id=test_requisition.id,
                    scanned_by_user_id=test_user.id,
                )
        mock_gc.get_json.assert_not_called()  # no fallback on auth failure

    @pytest.mark.asyncio
    async def test_transient_error_keeps_delta_token_and_fails_poll(self, db_session, test_user, test_requisition):
        """A transient/network exception (httpx.ReadTimeout re-raised by the retry
        layer) must NOT clear the stored token — it is still valid — and must surface as
        a failed poll, not fall back to a full scan."""
        import httpx

        sync = SyncState(
            user_id=test_user.id,
            folder="inbox",
            delta_token="still-valid-token",
            last_sync_at=datetime.now(UTC),
        )
        db_session.add(sync)
        db_session.commit()

        mock_gc = AsyncMock()
        mock_gc.delta_query.side_effect = httpx.ReadTimeout("timed out")

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            with pytest.raises(httpx.ReadTimeout):
                await poll_inbox(
                    token="fake-token",
                    db=db_session,
                    requisition_id=test_requisition.id,
                    scanned_by_user_id=test_user.id,
                )

        mock_gc.get_json.assert_not_called()  # failed poll, not a fallback scan
        sync = db_session.query(SyncState).first()
        assert sync.delta_token == "still-valid-token"  # token survives

    @pytest.mark.asyncio
    async def test_fallback_error_dict_raises(self, db_session, test_user, test_requisition):
        """An {"error": ...} dict from the fallback fetch (exhausted retries) must raise
        — a hard Graph outage is a failed poll, not a successful empty one."""
        from app.utils.graph_client import GraphAPIError

        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {"error": "max_retries", "detail": "All retries exhausted"}

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            with pytest.raises(GraphAPIError):
                await poll_inbox(
                    token="fake-token",
                    db=db_session,
                    requisition_id=test_requisition.id,
                )

    @pytest.mark.asyncio
    async def test_initial_sync_bounded_by_backfill_lookback(self, db_session, test_user, test_requisition):
        """The initial delta round (no stored token) must be bounded to the standard
        backfill window — otherwise the resumable-nextLink contract would drain the
        entire mailbox history across polls."""
        from app.config import settings

        mock_gc = AsyncMock()
        mock_gc.delta_query.return_value = ([], "initial-delta-token")

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
                scanned_by_user_id=test_user.id,
            )

        assert mock_gc.delta_query.call_args.kwargs["initial_lookback_days"] == settings.inbox_backfill_days

    @pytest.mark.asyncio
    async def test_fallback_fetch_failure_raises(self, db_session, test_user, test_requisition):
        """When fallback fetch fails, raise so caller can handle the error."""
        mock_gc = AsyncMock()
        mock_gc.get_json.side_effect = Exception("Network error")

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            with pytest.raises(Exception, match="Network error"):
                await poll_inbox(
                    token="fake-token",
                    db=db_session,
                    requisition_id=test_requisition.id,
                )

    @pytest.mark.asyncio
    async def test_dedup_already_processed(self, db_session, test_user, test_requisition):
        """Messages already in VendorResponse or ProcessedMessage are skipped."""
        # Pre-create a VendorResponse with msg-1
        existing_vr = VendorResponse(
            message_id="msg-1",
            vendor_name="Old Vendor",
            vendor_email="vendor@old.com",
            status="parsed",
            created_at=datetime.now(UTC),
        )
        db_session.add(existing_vr)
        db_session.commit()

        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {
            "value": [
                self._make_inbox_message(msg_id="msg-1"),
                self._make_inbox_message(msg_id="msg-2", sender_email="new@vendor.com"),
            ]
        }

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
            )

        # Only msg-2 should be processed
        assert len(results) == 1
        assert results[0]["message_id"] == "msg-2"

    @pytest.mark.asyncio
    async def test_dedup_processed_message(self, db_session, test_user, test_requisition):
        """Messages in ProcessedMessage table are also skipped."""
        pm = ProcessedMessage(
            message_id="msg-1",
            processing_type="inbox_poll",
            processed_at=datetime.now(UTC),
        )
        db_session.add(pm)
        db_session.commit()

        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {"value": [self._make_inbox_message(msg_id="msg-1")]}

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
            )

        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_noise_email_filtered(self, db_session, test_user, test_requisition):
        """Noise senders are filtered out."""
        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {
            "value": [
                self._make_inbox_message(msg_id="msg-noise", sender_email="noreply@microsoft.com"),
            ]
        }

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
            )

        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_tier1_conversation_id_match(self, db_session, test_user, test_requisition):
        """Tier 1: Match by conversationId."""
        # Create an outbound contact with a graph_conversation_id
        contact = Contact(
            requisition_id=test_requisition.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="Vendor A",
            vendor_contact="vendor@parts.com",
            graph_conversation_id="conv-match-1",
            status="sent",
            created_at=datetime.now(UTC),
        )
        db_session.add(contact)
        db_session.commit()

        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {
            "value": [
                self._make_inbox_message(
                    sender_email="vendor@parts.com",
                    conv_id="conv-match-1",
                ),
            ]
        }

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
            )

        assert len(results) == 1
        assert results[0]["matched_contact_id"] == contact.id
        assert results[0]["matched_requisition_id"] == test_requisition.id

    @pytest.mark.asyncio
    async def test_tier2_subject_token_match(self, db_session, test_user, test_requisition):
        """Tier 2: Match by [AVAIL-{id}] (legacy) subject token + email."""
        contact = Contact(
            requisition_id=test_requisition.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="Vendor B",
            vendor_contact="vendor@parts.com",
            status="sent",
            created_at=datetime.now(UTC),
        )
        db_session.add(contact)
        db_session.commit()

        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {
            "value": [
                self._make_inbox_message(
                    sender_email="vendor@parts.com",
                    subject=f"RE: [AVAIL-{test_requisition.id}] RFQ for parts",
                    conv_id="different-conv",
                ),
            ]
        }

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
            )

        assert len(results) == 1
        assert results[0]["matched_contact_id"] == contact.id

    @pytest.mark.asyncio
    async def test_tier2_subject_token_req_only(self, db_session, test_user, test_requisition):
        """Tier 2: Subject token found but sender email doesn't match any contact."""
        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {
            "value": [
                self._make_inbox_message(
                    sender_email="unknown@vendor.com",
                    subject=f"RE: [AVAIL-{test_requisition.id}] RFQ",
                    conv_id="no-match-conv",
                ),
            ]
        }

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
            )

        assert len(results) == 1
        assert results[0]["matched_requisition_id"] == test_requisition.id
        assert results[0]["matched_contact_id"] is None

    @pytest.mark.asyncio
    async def test_tier3_email_exact_match(self, db_session, test_user, test_requisition):
        """Tier 3: Exact email match (user-scoped)."""
        contact = Contact(
            requisition_id=test_requisition.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="Vendor C",
            vendor_contact="vendor@parts.com",
            status="sent",
            created_at=datetime.now(UTC),
        )
        db_session.add(contact)
        db_session.commit()

        mock_gc = AsyncMock()
        # Expired delta token → explicit fall back to the get_json full scan
        mock_gc.delta_query.side_effect = GraphSyncStateExpired("expired")
        mock_gc.get_json.return_value = {
            "value": [
                self._make_inbox_message(
                    sender_email="vendor@parts.com",
                    subject="Some other subject",
                    conv_id="no-match-conv",
                ),
            ]
        }

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
                scanned_by_user_id=test_user.id,
            )

        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_tier4_domain_match(self, db_session, test_user, test_requisition):
        """Tier 4: Domain match (user-scoped)."""
        contact = Contact(
            requisition_id=test_requisition.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="Vendor D",
            vendor_contact="sales@vendord.com",
            status="sent",
            created_at=datetime.now(UTC),
        )
        db_session.add(contact)
        db_session.commit()

        mock_gc = AsyncMock()
        # Expired delta token → explicit fall back to the get_json full scan
        mock_gc.delta_query.side_effect = GraphSyncStateExpired("expired")
        mock_gc.get_json.return_value = {
            "value": [
                self._make_inbox_message(
                    sender_email="other@vendord.com",
                    subject="Some subject",
                    conv_id="no-match-conv",
                ),
            ]
        }

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
                scanned_by_user_id=test_user.id,
            )

        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_domain_match_skips_noise_domain(self, db_session, test_user, test_requisition):
        """Domain match skips noise domains like microsoft.com."""
        contact = Contact(
            requisition_id=test_requisition.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="MS Sales",
            vendor_contact="sales@microsoft.com",
            status="sent",
            created_at=datetime.now(UTC),
        )
        db_session.add(contact)
        db_session.commit()

        mock_gc = AsyncMock()
        # Expired delta token → explicit fall back to the get_json full scan
        mock_gc.delta_query.side_effect = GraphSyncStateExpired("expired")
        # This email is from microsoft.com but with a non-noise prefix
        # However, _is_noise_email checks domain first, so it gets filtered
        mock_gc.get_json.return_value = {
            "value": [
                self._make_inbox_message(
                    sender_email="rep@microsoft.com",
                ),
            ]
        }

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
                scanned_by_user_id=test_user.id,
            )

        # Filtered by noise check
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_ai_parse_triggered(self, db_session, test_user, test_requisition):
        """When anthropic key is available, AI parse is triggered (batch path)."""
        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {"value": [self._make_inbox_message()]}

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value="fake-key"),
            patch("app.email_service._submit_parse_batch", new_callable=AsyncMock) as mock_batch,
        ):
            results = await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
            )

        assert len(results) == 1
        mock_batch.assert_called_once()

    @pytest.mark.asyncio
    async def test_ai_batch_failure_fallback(self, db_session, test_user, test_requisition):
        """When batch submit fails, fall back to sequential parsing."""
        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {"value": [self._make_inbox_message()]}

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value="fake-key"),
            patch(
                "app.email_service._submit_parse_batch",
                new_callable=AsyncMock,
                side_effect=Exception("Batch failed"),
            ),
            patch("app.email_service._parse_sequential_fallback", new_callable=AsyncMock) as mock_seq,
        ):
            results = await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
            )

        assert len(results) == 1
        mock_seq.assert_called_once()

    @pytest.mark.asyncio
    async def test_contact_status_progressed(self, db_session, test_user, test_requisition):
        """When a matched contact is found, its status is updated."""
        contact = Contact(
            requisition_id=test_requisition.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="Vendor X",
            vendor_contact="vendor@parts.com",
            graph_conversation_id="conv-progress",
            status="sent",
            created_at=datetime.now(UTC),
        )
        db_session.add(contact)
        db_session.commit()

        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {
            "value": [
                self._make_inbox_message(
                    sender_email="vendor@parts.com",
                    conv_id="conv-progress",
                ),
            ]
        }

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
            )

        assert len(results) == 1
        # Contact status should be updated (from "sent" to "responded" for unclassified)
        db_session.refresh(contact)
        # The VR has no classification set by poll_inbox (no AI), so _progress_contact_status
        # gets classification="" -> else branch -> sent->responded
        assert contact.status == "responded"

    @pytest.mark.asyncio
    async def test_message_save_exception_rollback(self, db_session, test_user, test_requisition):
        """If saving a message fails, rollback and continue with next."""
        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {
            "value": [
                self._make_inbox_message(msg_id="msg-1", sender_email="vendor1@parts.com"),
                self._make_inbox_message(msg_id="msg-2", sender_email="vendor2@parts.com"),
            ]
        }

        call_count = 0
        original_add = db_session.add

        def side_effect_add(obj):
            nonlocal call_count
            if isinstance(obj, VendorResponse):
                call_count += 1
                if call_count == 1:
                    raise Exception("DB error on first VR")
            return original_add(obj)

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
            patch.object(db_session, "add", side_effect=side_effect_add),
        ):
            results = await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
            )

        # First message fails (exception caught), second should succeed or also fail
        # The key is no uncaught exception
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_commit_failure_raises(self, db_session, test_user, test_requisition):
        """If final commit fails, exception propagates (with rollback)."""
        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {"value": [self._make_inbox_message()]}

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
            patch.object(db_session, "commit", side_effect=Exception("Commit failed")),
            pytest.raises(Exception, match="Commit failed"),
        ):
            await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
            )

    @pytest.mark.asyncio
    async def test_empty_inbox(self, db_session, test_user, test_requisition):
        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {"value": []}

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            results = await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
            )

        assert results == []

    @pytest.mark.asyncio
    async def test_empty_msg_id_skipped(self, db_session, test_user, test_requisition):
        """Messages without an id are skipped."""
        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {
            "value": [
                {
                    "id": "",
                    "subject": "No ID",
                    "from": {"emailAddress": {"address": "vendor@x.com"}},
                    "bodyPreview": "test",
                    "conversationId": "conv",
                }
            ]
        }

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
            )

        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_none_fetch_data(self, db_session, test_user, test_requisition):
        """When get_json returns None, messages list is empty."""
        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = None

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            results = await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
            )

        assert results == []

    @pytest.mark.asyncio
    async def test_delta_success_empty_messages_no_fallback(self, db_session, test_user, test_requisition):
        """Delta returns empty messages list — still counts as used_delta, no
        fallback."""
        mock_gc = AsyncMock()
        mock_gc.delta_query.return_value = ([], "new-token")

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
                scanned_by_user_id=test_user.id,
            )

        assert results == []
        # Fallback should NOT be called (used_delta=True)
        mock_gc.get_json.assert_not_called()

    @pytest.mark.asyncio
    async def test_body_from_body_content(self, db_session, test_user, test_requisition):
        """VendorResponse body comes from body.content, not bodyPreview."""
        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {
            "value": [
                self._make_inbox_message(
                    body_content="<p>Full body content</p>",
                    body_preview="Preview only",
                ),
            ]
        }

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
            )

        vr = db_session.query(VendorResponse).first()
        assert vr.body == "<p>Full body content</p>"

    @pytest.mark.asyncio
    async def test_body_fallback_to_preview(self, db_session, test_user, test_requisition):
        """When body.content is missing, use bodyPreview."""
        mock_gc = AsyncMock()
        msg = self._make_inbox_message()
        msg["body"] = {}  # no content key
        mock_gc.get_json.return_value = {"value": [msg]}

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
            )

        vr = db_session.query(VendorResponse).first()
        assert vr.body == "Quote attached"

    @pytest.mark.asyncio
    async def test_missing_sender_address(self, db_session, test_user, test_requisition):
        """When sender address is None, treated as empty string."""
        mock_gc = AsyncMock()
        msg = {
            "id": "msg-no-sender",
            "subject": "Test",
            "from": {"emailAddress": {"address": None, "name": "No Address"}},
            "bodyPreview": "body",
            "body": {"content": "body"},
            "conversationId": "conv-1",
        }
        mock_gc.get_json.return_value = {"value": [msg]}

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
            )

        # Empty email -> _is_noise_email returns True -> skipped
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_email_scoping_prevents_cross_user_leak(self, db_session, test_user, test_requisition):
        """Email/domain maps are user-scoped: another user's contacts don't match."""
        # Create another user
        other_user = User(
            email="other@trioscs.com",
            name="Other User",
            role="buyer",
            azure_id="other-azure",
            created_at=datetime.now(UTC),
        )
        db_session.add(other_user)
        db_session.flush()

        # Contact belongs to other_user
        contact = Contact(
            requisition_id=test_requisition.id,
            user_id=other_user.id,
            contact_type="email",
            vendor_name="Vendor Scoped",
            vendor_contact="vendor@scoped.com",
            status="sent",
            created_at=datetime.now(UTC),
        )
        db_session.add(contact)
        db_session.commit()

        mock_gc = AsyncMock()
        # Expired delta token → explicit fall back to the get_json full scan
        mock_gc.delta_query.side_effect = GraphSyncStateExpired("expired")
        mock_gc.get_json.return_value = {
            "value": [
                self._make_inbox_message(
                    sender_email="vendor@scoped.com",
                    conv_id="no-match-conv",
                    subject="Regular subject",
                ),
            ]
        }

        # Scan as test_user (not other_user)
        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
                scanned_by_user_id=test_user.id,
            )

        # Should be unmatched (email_exact and domain maps are user-scoped)
        assert len(results) == 1


# ── poll_inbox: cross-requisition reply fan-out ──────────────────────


class TestScopeThreadContactsToSender:
    """F7/F8: direct unit tests of _scope_thread_contacts_to_sender — the vendor-scoping
    guard that stops a Tier-1 conversation-id match from progressing the WRONG vendor's
    contacts on a legacy collided thread.

    Its four paths: (1) exact email, (2) same-domain
    fallback, (3) full-thread when single-vendor, (4) [] for a multi-vendor thread with an
    unrecognized sender (the security backstop). The poll_inbox pre-filter drops
    noise-domain SENDERS before they reach here, so the noise-domain guard on path 2 is
    only reachable via this direct test.
    """

    def _contact(self, email, vendor="V"):
        return Contact(
            contact_type="email",
            vendor_name=vendor,
            vendor_contact=email,
            status="sent",
            created_at=datetime.now(UTC),
        )

    def test_path1_exact_email_match(self):
        a1 = self._contact("a@vendora.com", "A")
        a2 = self._contact("a@vendora.com", "A")
        b1 = self._contact("b@vendorb.com", "B")
        out = _scope_thread_contacts_to_sender([a1, a2, b1], "a@vendora.com")
        assert out == [a1, a2]

    def test_path2_same_domain_fallback_scopes_to_one_vendor(self):
        a1 = self._contact("a@vendora.com", "A")
        b1 = self._contact("b@vendorb.com", "B")
        # sales@ is a different mailbox at Vendor A — domain fallback, never path 3.
        out = _scope_thread_contacts_to_sender([a1, b1], "sales@vendora.com")
        assert out == [a1]

    def test_path3_single_vendor_thread_full_fanout(self):
        a1 = self._contact("a@vendora.com", "A")
        a2 = self._contact("a@vendora.com", "A")
        # Colleague from a different mailbox, single-vendor thread → full fan-out.
        out = _scope_thread_contacts_to_sender([a1, a2], "colleague@vendora.com")
        assert out == [a1, a2]

    def test_path4_multi_vendor_unrecognized_sender_returns_empty(self):
        a1 = self._contact("a@vendora.com", "A")
        b1 = self._contact("b@vendorb.com", "B")
        # Stranger on a non-noise domain matching neither vendor → no fan-out (backstop).
        out = _scope_thread_contacts_to_sender([a1, b1], "stranger@unknownco.com")
        assert out == []

    def test_path2_noise_domain_guard_blocks_domain_fanout(self):
        noise_domain = next(iter(NOISE_DOMAINS))
        a1 = self._contact(f"alice@{noise_domain}", "A")
        b1 = self._contact(f"bob@{noise_domain}", "B")
        # An unrecognized sender on a generic shared domain must NOT match everyone on
        # that domain (path 2 is skipped for noise domains); two vendors block path 3 → [].
        out = _scope_thread_contacts_to_sender([a1, b1], f"carol@{noise_domain}")
        assert out == []


class TestPollInboxSavepointFailureNotParsed:
    """Regression: a VendorResponse whose savepoint commit fails must NOT reach
    AI parsing/billing.

    Bug: ``vr`` was appended to ``pending_parse`` BEFORE ``nested.commit()``. If
    the savepoint commit failed, the ``except`` rolled the row back but left the
    rolled-back ``vr`` in ``pending_parse`` — it was then AI-parsed and billed, and
    could spawn Offers referencing a vanished ``vendor_response_id`` that poisoned
    the poll's final ``db.commit()`` (rolling back the entire scan). Fix appends to
    ``pending_parse`` only after ``nested.commit()`` succeeds.
    """

    def _make_inbox_message(self, msg_id, sender_email):
        return {
            "id": msg_id,
            "subject": "RE: RFQ",
            "from": {"emailAddress": {"address": sender_email, "name": "Vendor"}},
            "bodyPreview": "Quote attached",
            "body": {"content": "<p>Quote attached</p>"},
            "conversationId": f"conv-{msg_id}",
            "receivedDateTime": None,
        }

    @pytest.mark.asyncio
    async def test_rolled_back_response_excluded_from_parse_and_billing(self, db_session, test_user, test_requisition):
        """First message commits; second message's savepoint commit fails.

        Only the first is parsed/billed/persisted.
        """
        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {
            "value": [
                self._make_inbox_message("msg-good", "good@parts.com"),
                self._make_inbox_message("msg-bad", "bad@parts.com"),
            ]
        }

        # Force ONLY the second message's savepoint commit to fail, exactly as a
        # Postgres savepoint-release error would (the observable outcome — row not
        # persisted, not parsed — holds identically on SQLite).
        real_begin_nested = db_session.begin_nested
        state = {"n": 0}

        def flaky_begin_nested():
            state["n"] += 1
            nested = real_begin_nested()
            if state["n"] == 2:

                def boom():
                    raise RuntimeError("savepoint commit failed")

                nested.commit = boom
            return nested

        db_session.begin_nested = flaky_begin_nested

        captured = {}

        async def fake_submit(pending, db):
            captured["parsed"] = list(pending)

        billed = MagicMock()

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value="fake-key"),
            patch("app.email_service._submit_parse_batch", side_effect=fake_submit),
            patch("app.email_service._record_email_mining_calls", billed),
        ):
            results = await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
            )

        # Only the good message produced a result and persisted a row.
        assert [r["message_id"] for r in results] == ["msg-good"]
        rows = db_session.query(VendorResponse).all()
        assert [r.message_id for r in rows] == ["msg-good"]

        # The rolled-back response must NOT have been enqueued for AI parsing…
        assert "parsed" in captured
        assert [vr.message_id for vr in captured["parsed"]] == ["msg-good"]
        # …nor billed against the Claude-call budget.
        billed.assert_called_once_with(1)


class TestPollInboxCrossRequisitionFanout:
    """Reply matching against per-(requisition, vendor) Contact rows.

    Tier-1: a conversation id maps to a LIST of Contacts — the reply is
    attributed to ALL of them (one VendorResponse per message, every Contact
    progressed). Tier-2: every [ref:] token in the subject is resolved.
    """

    def _make_inbox_message(
        self,
        msg_id="msg-1",
        sender_email="vendor@parts.com",
        subject="RE: RFQ",
        conv_id="conv-1",
    ):
        return {
            "id": msg_id,
            "subject": subject,
            "from": {"emailAddress": {"address": sender_email, "name": "Vendor"}},
            "bodyPreview": "Quote attached",
            "body": {"content": "<p>Quote attached</p>"},
            "conversationId": conv_id,
            "receivedDateTime": None,
        }

    def _fanout_contacts(self, db_session, test_user, conv_id=None):
        """Two Contacts (one per requisition) for ONE vendor email — the rows a cross-
        requisition send_batch_rfq writes."""
        req_a, req_b = _two_requisitions(db_session, test_user)
        contacts = []
        for req in (req_a, req_b):
            c = Contact(
                requisition_id=req.id,
                user_id=test_user.id,
                contact_type="email",
                vendor_name="Vendor A",
                vendor_contact="vendor@parts.com",
                graph_conversation_id=conv_id,
                status="sent",
                created_at=datetime.now(UTC),
            )
            db_session.add(c)
            contacts.append(c)
        db_session.commit()
        return req_a, req_b, contacts

    @pytest.mark.asyncio
    async def test_tier1_reply_attributed_to_all_contacts_sharing_conversation(self, db_session, test_user):
        req_a, req_b, (c1, c2) = self._fanout_contacts(db_session, test_user, conv_id="conv-shared")

        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {
            "value": [
                self._make_inbox_message(
                    sender_email="vendor@parts.com",
                    conv_id="conv-shared",
                ),
            ]
        }

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(token="fake-token", db=db_session)

        assert len(results) == 1
        # Exactly ONE VendorResponse per message (per-message, not per-requisition)
        vrs = db_session.query(VendorResponse).all()
        assert len(vrs) == 1
        assert vrs[0].contact_id == c1.id  # contacts[0] = earliest Contact
        assert results[0]["matched_contact_id"] == c1.id
        assert results[0]["matched_requisition_id"] == req_a.id
        # BOTH contacts progressed (sent -> responded)
        db_session.refresh(c1)
        db_session.refresh(c2)
        assert c1.status == "responded"
        assert c2.status == "responded"

    @pytest.mark.asyncio
    async def test_tier2_multi_token_subject_matches_all_requisitions(self, db_session, test_user):
        """A reply whose subject carries BOTH [ref:] tokens (no conversation-id match)
        resolves every (token req, sender email) pair."""
        req_a, req_b, (c1, c2) = self._fanout_contacts(db_session, test_user, conv_id=None)

        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {
            "value": [
                self._make_inbox_message(
                    sender_email="vendor@parts.com",
                    subject=f"RE: RFQ — 2 parts [ref:{req_a.id}] [ref:{req_b.id}]",
                    conv_id="unrelated-conv",
                ),
            ]
        }

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(token="fake-token", db=db_session)

        assert len(results) == 1
        assert len(db_session.query(VendorResponse).all()) == 1
        assert results[0]["matched_contact_id"] == c1.id
        db_session.refresh(c1)
        db_session.refresh(c2)
        assert c1.status == "responded"
        assert c2.status == "responded"

    @pytest.mark.asyncio
    async def test_tier2_multi_token_no_email_match_assigns_first_token_req(self, db_session, test_user):
        """Tokens found but unknown sender: the VendorResponse is assigned to the
        first token's requisition (existing single-token behavior preserved)."""
        req_a, req_b = _two_requisitions(db_session, test_user)

        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {
            "value": [
                self._make_inbox_message(
                    sender_email="unknown@vendor.com",
                    subject=f"RE: RFQ [ref:{req_a.id}] [ref:{req_b.id}]",
                    conv_id="no-match-conv",
                ),
            ]
        }

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(token="fake-token", db=db_session)

        assert len(results) == 1
        assert results[0]["matched_contact_id"] is None
        assert results[0]["matched_requisition_id"] == req_a.id

    @pytest.mark.asyncio
    async def test_send_then_reply_end_to_end(self, db_session, test_user):
        """Full loop: cross-requisition send writes fan-out Contacts sharing a
        conversation id; a reply on that conversation progresses ALL of them."""
        req_a, req_b = _two_requisitions(db_session, test_user)
        parts_map = {
            req_a.id: [{"mpn": "CROSS-MPN-A", "qty": 100}],
            req_b.id: [{"mpn": "CROSS-MPN-B", "qty": 200}],
        }
        tagged = f"RFQ [ref:{req_a.id}] [ref:{req_b.id}]"

        send_gc = AsyncMock()
        send_gc.post_json.return_value = {}
        send_gc.get_json.return_value = {
            "value": [_sent_item("sent-e2e", "conv-e2e", tagged, recipient="vendor@parts.com")]
        }
        vendor_groups = [
            {"vendor_name": "Vendor A", "vendor_email": "vendor@parts.com", "subject": "RFQ", "body": "Quote"},
        ]

        with (
            patch("app.utils.graph_client.GraphClient", return_value=send_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await send_batch_rfq(
                token="fake-token",
                db=db_session,
                user_id=test_user.id,
                vendor_groups=vendor_groups,
                requisition_parts_map=parts_map,
            )

        contacts = db_session.query(Contact).order_by(Contact.id).all()
        assert len(contacts) == 2
        assert all(c.graph_conversation_id == "conv-e2e" for c in contacts)

        poll_gc = AsyncMock()
        poll_gc.get_json.return_value = {
            "value": [
                self._make_inbox_message(
                    sender_email="vendor@parts.com",
                    conv_id="conv-e2e",
                ),
            ]
        }

        with (
            patch("app.utils.graph_client.GraphClient", return_value=poll_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(token="fake-token", db=db_session)

        assert len(results) == 1
        assert len(db_session.query(VendorResponse).all()) == 1
        for c in contacts:
            db_session.refresh(c)
            assert c.status == "responded"
        assert {c.requisition_id for c in contacts} == {req_a.id, req_b.id}

    @pytest.mark.asyncio
    async def test_tier1_collided_conversation_scoped_to_sender_vendor(self, db_session, test_user):
        """F1(b): legacy data can hold contacts of DIFFERENT vendors under one
        conversation id (the old subject-only sent-lookup).

        Tier-1 fan-out must scope to the replying vendor's email — fan out across
        requisitions for the SAME vendor, never across vendors.
        """
        req_a, req_b = _two_requisitions(db_session, test_user)
        contacts = []
        for req, vendor, email in [
            (req_a, "Vendor A", "a@vendora.com"),
            (req_b, "Vendor A", "a@vendora.com"),
            (req_a, "Vendor B", "b@vendorb.com"),
        ]:
            c = Contact(
                requisition_id=req.id,
                user_id=test_user.id,
                contact_type="email",
                vendor_name=vendor,
                vendor_contact=email,
                graph_conversation_id="conv-collided",
                status="sent",
                created_at=datetime.now(UTC),
            )
            db_session.add(c)
            contacts.append(c)
        db_session.commit()

        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {
            "value": [
                self._make_inbox_message(
                    sender_email="a@vendora.com",
                    conv_id="conv-collided",
                ),
            ]
        }
        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(token="fake-token", db=db_session)

        assert len(results) == 1
        a1, a2, b1 = contacts
        for c in contacts:
            db_session.refresh(c)
        # Vendor A's contacts progressed on BOTH requisitions; Vendor B's untouched.
        assert a1.status == "responded"
        assert a2.status == "responded"
        assert b1.status == "sent"
        vr = db_session.query(VendorResponse).one()
        assert vr.contact_id in (a1.id, a2.id)

    @pytest.mark.asyncio
    async def test_tier1_thread_reply_from_colleague_at_same_vendor_still_matches(self, db_session, test_user):
        """A reply from a different mailbox at the SAME vendor (single-vendor thread,
        e.g. a colleague answering) still fans out to the thread's contacts."""
        req_a, req_b, (c1, c2) = self._fanout_contacts(db_session, test_user, conv_id="conv-shared")

        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {
            "value": [
                self._make_inbox_message(
                    sender_email="colleague@parts.com",
                    conv_id="conv-shared",
                ),
            ]
        }
        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(token="fake-token", db=db_session)

        assert len(results) == 1
        db_session.refresh(c1)
        db_session.refresh(c2)
        assert c1.status == "responded"
        assert c2.status == "responded"

    @pytest.mark.asyncio
    async def test_tier1_collided_thread_unrecognized_sender_no_cross_vendor_fanout(self, db_session, test_user):
        """F7: path-4 backstop — a multi-vendor collided conversation with a reply from an
        UNRECOGNIZED, non-noise sender matching neither vendor must progress NO contact.

        This pins the no-cross-vendor-fan-out invariant: if _scope_thread_contacts_to_sender
        ever fell through to returning the full thread instead of [], a reply from a stranger
        would silently progress BOTH vendors' contacts — the exact mis-attribution the
        function exists to prevent. Tier-1 returns [], so the reply falls through to Tier 2+
        (no token, no email/domain match here) and is saved unmatched.
        """
        req_a, req_b = _two_requisitions(db_session, test_user)
        contacts = []
        for req, vendor, email in [
            (req_a, "Vendor A", "a@vendora.com"),
            (req_b, "Vendor A", "a@vendora.com"),
            (req_a, "Vendor B", "b@vendorb.com"),
        ]:
            c = Contact(
                requisition_id=req.id,
                user_id=test_user.id,
                contact_type="email",
                vendor_name=vendor,
                vendor_contact=email,
                graph_conversation_id="conv-collided",
                status="sent",
                created_at=datetime.now(UTC),
            )
            db_session.add(c)
            contacts.append(c)
        db_session.commit()

        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {
            "value": [
                self._make_inbox_message(
                    # Third party, NON-noise domain, matches neither vendora.com nor vendorb.com
                    sender_email="stranger@unknownco.com",
                    conv_id="conv-collided",
                ),
            ]
        }
        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(token="fake-token", db=db_session)

        assert len(results) == 1
        for c in contacts:
            db_session.refresh(c)
        # NO contact progressed — the stranger matched nobody on the collided thread.
        assert all(c.status == "sent" for c in contacts)
        # The response falls through Tiers 2-4 (no token, no email/domain match) → unmatched.
        vr = db_session.query(VendorResponse).one()
        assert vr.contact_id is None
        assert vr.status == "new"

    @pytest.mark.asyncio
    async def test_tier1_collided_thread_domain_fallback_scopes_to_one_vendor(self, db_session, test_user):
        """F8: path-2 (same-domain fallback) on a MULTI-vendor thread — a reply from a
        DIFFERENT mailbox at Vendor A (sales@vendora.com) must progress only Vendor A's
        contacts and never leak to Vendor B, even though both share one conversation id.

        The exact-email match (path 1) misses (sales@ != a@), so resolution must come from
        the domain branch, NOT the single-vendor full-thread branch (path 3 can't fire — the
        thread holds two distinct vendor emails).
        """
        req_a, req_b = _two_requisitions(db_session, test_user)
        a1 = Contact(
            requisition_id=req_a.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="Vendor A",
            vendor_contact="a@vendora.com",
            graph_conversation_id="conv-collided",
            status="sent",
            created_at=datetime.now(UTC),
        )
        a2 = Contact(
            requisition_id=req_b.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="Vendor A",
            vendor_contact="a@vendora.com",
            graph_conversation_id="conv-collided",
            status="sent",
            created_at=datetime.now(UTC),
        )
        b1 = Contact(
            requisition_id=req_a.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="Vendor B",
            vendor_contact="b@vendorb.com",
            graph_conversation_id="conv-collided",
            status="sent",
            created_at=datetime.now(UTC),
        )
        db_session.add_all([a1, a2, b1])
        db_session.commit()

        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {
            "value": [
                self._make_inbox_message(
                    sender_email="sales@vendora.com",  # different mailbox, same domain as Vendor A
                    conv_id="conv-collided",
                ),
            ]
        }
        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(token="fake-token", db=db_session)

        assert len(results) == 1
        for c in (a1, a2, b1):
            db_session.refresh(c)
        # Only Vendor A's contacts progressed via the domain fallback; Vendor B untouched.
        assert a1.status == "responded"
        assert a2.status == "responded"
        assert b1.status == "sent"
        vr = db_session.query(VendorResponse).one()
        assert vr.contact_id in (a1.id, a2.id)

    @pytest.mark.asyncio
    async def test_tier2_stale_token_filtered_against_live_requisitions(self, db_session, test_user):
        """F5: a [ref:] token pointing at a deleted requisition is dropped before
        use — the live token still matches its contact, and nothing crashes."""
        req_a, req_b, (c1, c2) = self._fanout_contacts(db_session, test_user, conv_id=None)
        stale_id = req_a.id + req_b.id + 1000  # guaranteed nonexistent

        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {
            "value": [
                self._make_inbox_message(
                    sender_email="vendor@parts.com",
                    subject=f"RE: RFQ [ref:{stale_id}] [ref:{req_a.id}]",
                    conv_id="unrelated-conv",
                ),
            ]
        }
        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(token="fake-token", db=db_session)

        assert len(results) == 1
        assert results[0]["matched_requisition_id"] == req_a.id
        db_session.refresh(c1)
        assert c1.status == "responded"

    @pytest.mark.asyncio
    async def test_tier2_stale_token_only_saves_unlinked_response(self, db_session, test_user):
        """F5: a subject whose ONLY token is stale must not be attributed to the
        dead requisition (FK crash on PG) — the reply is saved unmatched."""
        _two_requisitions(db_session, test_user)

        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {
            "value": [
                self._make_inbox_message(
                    sender_email="unknown@vendor.com",
                    subject="RE: RFQ [ref:99999]",
                    conv_id="no-match-conv",
                ),
            ]
        }
        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            results = await poll_inbox(token="fake-token", db=db_session)

        assert len(results) == 1
        assert results[0]["matched_requisition_id"] is None
        vr = db_session.query(VendorResponse).one()
        assert vr.requisition_id is None
        assert vr.status == "new"


# ── OOO / bounce contact-status repair (F2) ──────────────────────────


class TestOooBounceContactRepair:
    """The post-parse OOO/bounce correction must (a) trigger on the vocabulary the
    classifiers actually emit — exactly "ooo_bounce" (_classify_response,
    RESPONSE_PARSE_SCHEMA, ai.py reparse) — and (b) apply to ALL contacts matched for
    the message (conversation-id siblings, vendor-email scoped), on BOTH the sequential-
    fallback path and the batch path."""

    def _seed(self, db_session, test_user, conv_id="conv-ooo"):
        req_a, req_b = _two_requisitions(db_session, test_user)
        contacts = []
        for req in (req_a, req_b):
            c = Contact(
                requisition_id=req.id,
                user_id=test_user.id,
                contact_type="email",
                vendor_name="Vendor A",
                vendor_contact="vendor@parts.com",
                graph_conversation_id=conv_id,
                status="sent",
                created_at=datetime.now(UTC),
            )
            db_session.add(c)
            contacts.append(c)
        db_session.commit()
        return contacts

    def _message(self, body, subject="Automatic reply: RFQ", conv_id="conv-ooo"):
        return {
            "id": "msg-ooo-1",
            "subject": subject,
            "from": {"emailAddress": {"address": "vendor@parts.com", "name": "Vendor"}},
            "bodyPreview": body,
            "body": {"content": body},
            "conversationId": conv_id,
            "receivedDateTime": None,
        }

    async def _poll(self, db_session, message):
        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {"value": [message]}
        parsed = {"sentiment": "neutral", "parts": [], "confidence": 0.9}
        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value="fake-key"),
            patch("app.email_service._submit_parse_batch", side_effect=RuntimeError("batch down")),
            patch("app.email_service.parse_response_ai", new_callable=AsyncMock, return_value=parsed),
        ):
            return await poll_inbox(token="fake-token", db=db_session)

    @pytest.mark.asyncio
    async def test_ooo_classification_repairs_all_sibling_contacts(self, db_session, test_user):
        """Sequential-fallback path: an out-of-office reply sets status='ooo' on
        EVERY contact matched for the message, not just vr.contact_id's."""
        c1, c2 = self._seed(db_session, test_user)
        body = "I am currently out of office and will return Monday."
        results = await self._poll(db_session, self._message(body))

        assert len(results) == 1
        vr = db_session.query(VendorResponse).one()
        assert vr.classification == "ooo_bounce"  # the REAL vocabulary
        db_session.refresh(c1)
        db_session.refresh(c2)
        assert c1.status == "ooo"
        assert c2.status == "ooo"

    @pytest.mark.asyncio
    async def test_bounce_signals_set_bounced(self, db_session, test_user):
        c1, c2 = self._seed(db_session, test_user)
        body = "Undeliverable: delivery failure — recipient address rejected."
        await self._poll(db_session, self._message(body, subject="Undeliverable: RFQ"))

        db_session.refresh(c1)
        db_session.refresh(c2)
        assert c1.status == "bounced"
        assert c2.status == "bounced"

    @pytest.mark.asyncio
    async def test_terminal_contact_status_not_regressed(self, db_session, test_user):
        """A contact already quoted must not be downgraded by a late auto-reply."""
        c1, c2 = self._seed(db_session, test_user)
        c1.status = "quoted"
        db_session.commit()
        body = "Automatic reply: out of office."
        await self._poll(db_session, self._message(body))

        db_session.refresh(c1)
        db_session.refresh(c2)
        assert c1.status == "quoted"
        assert c2.status == "ooo"

    @pytest.mark.asyncio
    async def test_batch_path_repairs_contacts_when_results_arrive(self, db_session, test_user):
        """Batch path: classification arrives LATER via process_batch_results —
        the repair must run there too (it was structurally dead before)."""
        c1, c2 = self._seed(db_session, test_user, conv_id="conv-batch-ooo")
        vr = VendorResponse(
            requisition_id=c1.requisition_id,
            contact_id=c1.id,
            vendor_name="Vendor A",
            vendor_email="vendor@parts.com",
            subject="Automatic reply: RFQ",
            body="I am out of the office until next week.",
            message_id="msg-batch-ooo",
            graph_conversation_id="conv-batch-ooo",
            status="new",
            created_at=datetime.now(UTC),
        )
        db_session.add(vr)
        db_session.flush()
        pb = PendingBatch(
            batch_id="batch-ooo-1",
            batch_type="inbox_parse",
            request_map={f"vr-{vr.id}": vr.id},
            status="processing",
            submitted_at=datetime.now(UTC),
        )
        db_session.add(pb)
        db_session.commit()

        batch_results = {
            f"vr-{vr.id}": {
                "overall_sentiment": "neutral",
                "overall_classification": "ooo_bounce",
                "confidence": 0.9,
                "parts": [],
            }
        }
        with (
            patch("app.utils.claude_client.claude_batch_results", new_callable=AsyncMock, return_value=batch_results),
            patch("app.services.response_parser._normalize_parsed_parts"),
        ):
            applied = await process_batch_results(db_session)

        assert applied == 1
        db_session.refresh(c1)
        db_session.refresh(c2)
        assert c1.status == "ooo"
        assert c2.status == "ooo"


# ── process_batch_results ────────────────────────────────────────────


class TestProcessBatchResults:
    @pytest.mark.asyncio
    async def test_no_pending_batches(self, db_session):
        with (
            patch("app.utils.claude_client.claude_batch_results", new_callable=AsyncMock),
            patch("app.services.response_parser._normalize_parsed_parts"),
        ):
            count = await process_batch_results(db_session)
        assert count == 0

    @pytest.mark.asyncio
    async def test_completed_batch(self, db_session, test_user, test_requisition):
        """Batch completes with results -> apply to VendorResponse."""
        vr = VendorResponse(
            requisition_id=test_requisition.id,
            vendor_name="Vendor X",
            vendor_email="vendor@x.com",
            subject="RE: RFQ",
            body="Quote: $1.50 for LM317T",
            status="new",
            message_id="msg-batch-1",
            created_at=datetime.now(UTC),
        )
        db_session.add(vr)
        db_session.flush()

        pb = PendingBatch(
            batch_id="batch-abc",
            batch_type="inbox_parse",
            request_map={"vr-" + str(vr.id): vr.id},
            status="processing",
            submitted_at=datetime.now(UTC),
        )
        db_session.add(pb)
        db_session.commit()

        batch_results = {
            "vr-" + str(vr.id): {
                "overall_sentiment": "positive",
                "parts": [{"unit_price": 1.50, "mpn": "LM317T"}],
                "confidence": 0.9,
            }
        }

        with (
            patch(
                "app.utils.claude_client.claude_batch_results",
                new_callable=AsyncMock,
                return_value=batch_results,
            ),
            patch("app.services.response_parser._normalize_parsed_parts"),
        ):
            count = await process_batch_results(db_session)

        assert count == 1
        db_session.refresh(vr)
        assert vr.status == "parsed"
        assert vr.confidence == 0.9

        db_session.refresh(pb)
        assert pb.status == "completed"
        assert pb.result_count == 1

    @pytest.mark.asyncio
    async def test_batch_still_processing(self, db_session, test_user, test_requisition):
        """Batch returns None (still processing, not timed out)."""
        pb = PendingBatch(
            batch_id="batch-still",
            batch_type="inbox_parse",
            request_map={},
            status="processing",
            submitted_at=datetime.now(UTC),
        )
        db_session.add(pb)
        db_session.commit()

        with (
            patch(
                "app.utils.claude_client.claude_batch_results",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("app.services.response_parser._normalize_parsed_parts"),
        ):
            count = await process_batch_results(db_session)

        assert count == 0
        db_session.refresh(pb)
        assert pb.status == "processing"

    @pytest.mark.asyncio
    async def test_batch_timeout(self, db_session, test_user, test_requisition):
        """Batch submitted >24h ago and returns None -> mark failed."""
        pb = PendingBatch(
            batch_id="batch-timeout",
            batch_type="inbox_parse",
            request_map={},
            status="processing",
            submitted_at=datetime.now(UTC) - timedelta(hours=25),
        )
        db_session.add(pb)
        db_session.commit()

        with (
            patch(
                "app.utils.claude_client.claude_batch_results",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("app.services.response_parser._normalize_parsed_parts"),
        ):
            count = await process_batch_results(db_session)

        assert count == 0
        db_session.refresh(pb)
        assert pb.status == "failed"
        assert "24h" in pb.error_message

    @pytest.mark.asyncio
    async def test_batch_exception_recent(self, db_session, test_user, test_requisition):
        """Batch check raises exception, submitted recently -> stays processing."""
        pb = PendingBatch(
            batch_id="batch-err-recent",
            batch_type="inbox_parse",
            request_map={},
            status="processing",
            submitted_at=datetime.now(UTC) - timedelta(hours=1),
        )
        db_session.add(pb)
        db_session.commit()

        with (
            patch(
                "app.utils.claude_client.claude_batch_results",
                new_callable=AsyncMock,
                side_effect=Exception("API down"),
            ),
            patch("app.services.response_parser._normalize_parsed_parts"),
        ):
            count = await process_batch_results(db_session)

        assert count == 0
        db_session.refresh(pb)
        assert pb.status == "processing"

    @pytest.mark.asyncio
    async def test_batch_exception_old(self, db_session, test_user, test_requisition):
        """Batch check raises exception, submitted >24h ago -> mark failed."""
        pb = PendingBatch(
            batch_id="batch-err-old",
            batch_type="inbox_parse",
            request_map={},
            status="processing",
            submitted_at=datetime.now(UTC) - timedelta(hours=25),
        )
        db_session.add(pb)
        db_session.commit()

        with (
            patch(
                "app.utils.claude_client.claude_batch_results",
                new_callable=AsyncMock,
                side_effect=Exception("API down"),
            ),
            patch("app.services.response_parser._normalize_parsed_parts"),
        ):
            count = await process_batch_results(db_session)

        assert count == 0
        db_session.refresh(pb)
        assert pb.status == "failed"
        assert "Timed out" in pb.error_message

    @pytest.mark.asyncio
    async def test_batch_skips_already_parsed(self, db_session, test_user, test_requisition):
        """VR already in 'parsed' state -> skip it."""
        vr = VendorResponse(
            requisition_id=test_requisition.id,
            vendor_name="Vendor X",
            vendor_email="vendor@x.com",
            body="test",
            status="parsed",
            message_id="msg-already-parsed",
            created_at=datetime.now(UTC),
        )
        db_session.add(vr)
        db_session.flush()

        pb = PendingBatch(
            batch_id="batch-skip",
            batch_type="inbox_parse",
            request_map={"vr-" + str(vr.id): vr.id},
            status="processing",
            submitted_at=datetime.now(UTC),
        )
        db_session.add(pb)
        db_session.commit()

        batch_results = {"vr-" + str(vr.id): {"overall_sentiment": "positive", "parts": [], "confidence": 0.5}}

        with (
            patch(
                "app.utils.claude_client.claude_batch_results",
                new_callable=AsyncMock,
                return_value=batch_results,
            ),
            patch("app.services.response_parser._normalize_parsed_parts"),
        ):
            count = await process_batch_results(db_session)

        # Skipped because already parsed
        assert count == 0

    @pytest.mark.asyncio
    async def test_batch_skips_unknown_custom_id(self, db_session, test_user, test_requisition):
        """Custom ID not in request_map -> skip."""
        pb = PendingBatch(
            batch_id="batch-unknown",
            batch_type="inbox_parse",
            request_map={"vr-999": 999},
            status="processing",
            submitted_at=datetime.now(UTC),
        )
        db_session.add(pb)
        db_session.commit()

        batch_results = {"vr-unknown": {"overall_sentiment": "positive"}}

        with (
            patch(
                "app.utils.claude_client.claude_batch_results",
                new_callable=AsyncMock,
                return_value=batch_results,
            ),
            patch("app.services.response_parser._normalize_parsed_parts"),
        ):
            count = await process_batch_results(db_session)

        assert count == 0

    @pytest.mark.asyncio
    async def test_batch_skips_vr_not_found(self, db_session, test_user, test_requisition):
        """VR ID in request_map but VR deleted -> skip."""
        pb = PendingBatch(
            batch_id="batch-deleted",
            batch_type="inbox_parse",
            request_map={"vr-99999": 99999},
            status="processing",
            submitted_at=datetime.now(UTC),
        )
        db_session.add(pb)
        db_session.commit()

        batch_results = {"vr-99999": {"overall_sentiment": "positive", "parts": [], "confidence": 0.5}}

        with (
            patch(
                "app.utils.claude_client.claude_batch_results",
                new_callable=AsyncMock,
                return_value=batch_results,
            ),
            patch("app.services.response_parser._normalize_parsed_parts"),
        ):
            count = await process_batch_results(db_session)

        assert count == 0

    @pytest.mark.asyncio
    async def test_batch_string_json_parsing(self, db_session, test_user, test_requisition):
        """parsed_data is a JSON string -> parsed into dict."""
        vr = VendorResponse(
            requisition_id=test_requisition.id,
            vendor_name="Vendor JSON",
            vendor_email="vendor@json.com",
            body="Quote body",
            status="new",
            message_id="msg-json-str",
            created_at=datetime.now(UTC),
        )
        db_session.add(vr)
        db_session.flush()

        pb = PendingBatch(
            batch_id="batch-json",
            batch_type="inbox_parse",
            request_map={"vr-" + str(vr.id): vr.id},
            status="processing",
            submitted_at=datetime.now(UTC),
        )
        db_session.add(pb)
        db_session.commit()

        json_str = json.dumps(
            {
                "overall_sentiment": "positive",
                "parts": [{"unit_price": 2.0}],
                "confidence": 0.85,
            }
        )

        batch_results = {"vr-" + str(vr.id): json_str}

        with (
            patch(
                "app.utils.claude_client.claude_batch_results",
                new_callable=AsyncMock,
                return_value=batch_results,
            ),
            patch("app.services.response_parser._normalize_parsed_parts"),
        ):
            count = await process_batch_results(db_session)

        assert count == 1

    @pytest.mark.asyncio
    async def test_batch_unparseable_string(self, db_session, test_user, test_requisition):
        """parsed_data is a string that's not valid JSON -> skip."""
        vr = VendorResponse(
            requisition_id=test_requisition.id,
            vendor_name="Vendor Bad",
            vendor_email="vendor@bad.com",
            body="body",
            status="new",
            message_id="msg-bad-str",
            created_at=datetime.now(UTC),
        )
        db_session.add(vr)
        db_session.flush()

        pb = PendingBatch(
            batch_id="batch-bad-json",
            batch_type="inbox_parse",
            request_map={"vr-" + str(vr.id): vr.id},
            status="processing",
            submitted_at=datetime.now(UTC),
        )
        db_session.add(pb)
        db_session.commit()

        batch_results = {"vr-" + str(vr.id): "not valid json"}

        with (
            patch(
                "app.utils.claude_client.claude_batch_results",
                new_callable=AsyncMock,
                return_value=batch_results,
            ),
            patch("app.services.response_parser._normalize_parsed_parts"),
        ):
            count = await process_batch_results(db_session)

        assert count == 0

    @pytest.mark.asyncio
    async def test_batch_non_dict_result(self, db_session, test_user, test_requisition):
        """parsed_data is not a dict (e.g., a list) -> skip."""
        vr = VendorResponse(
            requisition_id=test_requisition.id,
            vendor_name="Vendor List",
            vendor_email="vendor@list.com",
            body="body",
            status="new",
            message_id="msg-list",
            created_at=datetime.now(UTC),
        )
        db_session.add(vr)
        db_session.flush()

        pb = PendingBatch(
            batch_id="batch-list",
            batch_type="inbox_parse",
            request_map={"vr-" + str(vr.id): vr.id},
            status="processing",
            submitted_at=datetime.now(UTC),
        )
        db_session.add(pb)
        db_session.commit()

        batch_results = {"vr-" + str(vr.id): [1, 2, 3]}

        with (
            patch(
                "app.utils.claude_client.claude_batch_results",
                new_callable=AsyncMock,
                return_value=batch_results,
            ),
            patch("app.services.response_parser._normalize_parsed_parts"),
        ):
            count = await process_batch_results(db_session)

        assert count == 0

    @pytest.mark.asyncio
    async def test_batch_falsy_parsed_data(self, db_session, test_user, test_requisition):
        """parsed_data is falsy (None, empty dict) -> skip."""
        vr = VendorResponse(
            requisition_id=test_requisition.id,
            vendor_name="Vendor Null",
            vendor_email="vendor@null.com",
            body="body",
            status="new",
            message_id="msg-null",
            created_at=datetime.now(UTC),
        )
        db_session.add(vr)
        db_session.flush()

        pb = PendingBatch(
            batch_id="batch-null",
            batch_type="inbox_parse",
            request_map={"vr-" + str(vr.id): vr.id},
            status="processing",
            submitted_at=datetime.now(UTC),
        )
        db_session.add(pb)
        db_session.commit()

        batch_results = {"vr-" + str(vr.id): None}

        with (
            patch(
                "app.utils.claude_client.claude_batch_results",
                new_callable=AsyncMock,
                return_value=batch_results,
            ),
            patch("app.services.response_parser._normalize_parsed_parts"),
        ):
            count = await process_batch_results(db_session)

        assert count == 0

    @pytest.mark.asyncio
    async def test_batch_commit_failure(self, db_session, test_user, test_requisition):
        """If commit fails after applying results, handle gracefully."""
        vr = VendorResponse(
            requisition_id=test_requisition.id,
            vendor_name="Vendor Commit",
            vendor_email="vendor@commit.com",
            body="body",
            status="new",
            message_id="msg-commit-fail",
            created_at=datetime.now(UTC),
        )
        db_session.add(vr)
        db_session.flush()

        pb = PendingBatch(
            batch_id="batch-commit-fail",
            batch_type="inbox_parse",
            request_map={"vr-" + str(vr.id): vr.id},
            status="processing",
            submitted_at=datetime.now(UTC),
        )
        db_session.add(pb)
        db_session.commit()

        batch_results = {
            "vr-" + str(vr.id): {
                "overall_sentiment": "positive",
                "parts": [],
                "confidence": 0.5,
            }
        }

        original_commit = db_session.commit

        commit_call_count = 0

        def failing_commit():
            nonlocal commit_call_count
            commit_call_count += 1
            # Fail on first commit (the per-batch commit)
            if commit_call_count == 1:
                raise Exception("Commit failed")
            return original_commit()

        with (
            patch(
                "app.utils.claude_client.claude_batch_results",
                new_callable=AsyncMock,
                return_value=batch_results,
            ),
            patch("app.services.response_parser._normalize_parsed_parts"),
            patch.object(db_session, "commit", side_effect=failing_commit),
        ):
            count = await process_batch_results(db_session)

        # Applied 1 but commit failed, so overall count still 1
        # The function logs error and rolls back
        assert count == 1

    @pytest.mark.asyncio
    async def test_batch_none_request_map(self, db_session, test_user, test_requisition):
        """PendingBatch with None request_map -> empty iteration."""
        pb = PendingBatch(
            batch_id="batch-none-map",
            batch_type="inbox_parse",
            request_map=None,
            status="processing",
            submitted_at=datetime.now(UTC),
        )
        db_session.add(pb)
        db_session.commit()

        batch_results = {"vr-1": {"overall_sentiment": "positive"}}

        with (
            patch(
                "app.utils.claude_client.claude_batch_results",
                new_callable=AsyncMock,
                return_value=batch_results,
            ),
            patch("app.services.response_parser._normalize_parsed_parts"),
        ):
            count = await process_batch_results(db_session)

        assert count == 0


# ── NOISE constants coverage ─────────────────────────────────────────


class TestNoiseConstants:
    def test_noise_domains_is_set(self):
        assert isinstance(NOISE_DOMAINS, set)
        assert len(NOISE_DOMAINS) > 10

    def test_noise_prefixes_is_set(self):
        assert isinstance(NOISE_PREFIXES, set)
        assert "noreply" in NOISE_PREFIXES
        assert "newsletter" in NOISE_PREFIXES
