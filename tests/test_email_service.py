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

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.email_service import (
    NOISE_DOMAINS,
    NOISE_PREFIXES,
    _apply_parsed_result,
    _build_html_body,
    _classify_response,
    _find_sent_message,
    _is_noise_email,
    _parse_sequential_fallback,
    _progress_contact_status,
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
    SyncState,
    User,
    VendorResponse,
)

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

    def test_quoted_terminal_no_change(self):
        c = self._make_contact("quoted")
        _progress_contact_status(c, self._make_vr("no_stock"), MagicMock())
        assert c.status == "quoted"

    def test_declined_terminal_no_change(self):
        c = self._make_contact("declined")
        _progress_contact_status(c, self._make_vr("quote_provided"), MagicMock())
        assert c.status == "declined"

    def test_quote_provided_sets_quoted(self):
        c = self._make_contact("sent")
        _progress_contact_status(c, self._make_vr("quote_provided"), MagicMock())
        assert c.status == "quoted"

    def test_no_stock_sets_declined(self):
        c = self._make_contact("sent")
        _progress_contact_status(c, self._make_vr("no_stock"), MagicMock())
        assert c.status == "declined"

    def test_ooo_bounce_sets_pending(self):
        c = self._make_contact("sent")
        _progress_contact_status(c, self._make_vr("ooo_bounce"), MagicMock())
        assert c.status == "pending"

    def test_clarification_needed_sets_responded(self):
        c = self._make_contact("sent")
        _progress_contact_status(c, self._make_vr("clarification_needed"), MagicMock())
        assert c.status == "responded"

    def test_counter_offer_sets_responded(self):
        c = self._make_contact("sent")
        _progress_contact_status(c, self._make_vr("counter_offer"), MagicMock())
        assert c.status == "responded"

    def test_partial_availability_sets_responded(self):
        c = self._make_contact("sent")
        _progress_contact_status(c, self._make_vr("partial_availability"), MagicMock())
        assert c.status == "responded"

    def test_unknown_classification_sent_to_responded(self):
        c = self._make_contact("sent")
        _progress_contact_status(c, self._make_vr("unknown_type"), MagicMock())
        assert c.status == "responded"

    def test_unknown_classification_opened_to_responded(self):
        c = self._make_contact("opened")
        _progress_contact_status(c, self._make_vr("anything"), MagicMock())
        assert c.status == "responded"

    def test_unknown_classification_responded_stays(self):
        c = self._make_contact("responded")
        _progress_contact_status(c, self._make_vr("something_else"), MagicMock())
        # Not in ("sent", "opened") so status doesn't change from the else branch
        assert c.status == "responded"

    def test_none_classification(self):
        c = self._make_contact("sent")
        _progress_contact_status(c, self._make_vr(None), MagicMock())
        # None -> empty string -> falls to else branch -> sent->responded
        assert c.status == "responded"

    def test_status_updated_at_is_set(self):
        c = self._make_contact("sent")
        _progress_contact_status(c, self._make_vr("quote_provided"), MagicMock())
        assert c.status_updated_at is not None


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


class TestFindSentMessage:
    @pytest.mark.asyncio
    async def test_found_matching_subject(self):
        gc = AsyncMock()
        gc.get_json.return_value = {
            "value": [
                {"id": "msg-1", "conversationId": "conv-1", "subject": "RFQ Parts [ref:10]"},
                {"id": "msg-2", "conversationId": "conv-2", "subject": "Something Else"},
            ]
        }
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await _find_sent_message(gc, "RFQ Parts [ref:10]")
        assert result["id"] == "msg-1"
        assert result["conversationId"] == "conv-1"

    @pytest.mark.asyncio
    async def test_no_matching_subject(self):
        gc = AsyncMock()
        gc.get_json.return_value = {
            "value": [
                {"id": "msg-1", "subject": "Not a match"},
            ]
        }
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await _find_sent_message(gc, "RFQ Parts [ref:10]")
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_response(self):
        gc = AsyncMock()
        gc.get_json.return_value = None
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await _find_sent_message(gc, "Subject")
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_value(self):
        gc = AsyncMock()
        gc.get_json.return_value = {"value": []}
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await _find_sent_message(gc, "Subject")
        assert result is None

    @pytest.mark.asyncio
    async def test_exception_returns_none(self):
        gc = AsyncMock()
        gc.get_json.side_effect = Exception("Network error")
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await _find_sent_message(gc, "Subject")
        assert result is None

    @pytest.mark.asyncio
    async def test_subject_whitespace_matching(self):
        gc = AsyncMock()
        gc.get_json.return_value = {
            "value": [
                {"id": "msg-1", "conversationId": "conv-1", "subject": " RFQ [ref:10] "},
            ]
        }
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await _find_sent_message(gc, " RFQ [ref:10] ")
        assert result["id"] == "msg-1"

    @pytest.mark.asyncio
    async def test_retries_on_miss_then_finds(self):
        """Verify retry loop: returns None twice, then finds the message on 3rd attempt."""
        gc = AsyncMock()
        gc.get_json.side_effect = [
            {"value": []},
            {"value": []},
            {"value": [{"id": "msg-1", "conversationId": "conv-1", "subject": "Test Subject"}]},
        ]
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await _find_sent_message(gc, "Test Subject")
        assert result["id"] == "msg-1"
        assert gc.get_json.call_count == 3

    @pytest.mark.asyncio
    async def test_early_return_on_first_match(self):
        """Verify function returns on first successful match without exhausting
        retries."""
        gc = AsyncMock()
        gc.get_json.return_value = {
            "value": [{"id": "msg-1", "conversationId": "conv-1", "subject": "Quick Find"}],
        }
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await _find_sent_message(gc, "Quick Find")
        assert result["id"] == "msg-1"
        assert gc.get_json.call_count == 1


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

        assert len(results) == 0
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
            "value": [
                {
                    "id": "sent-msg-100",
                    "conversationId": "conv-100",
                    "subject": tagged_subject,
                }
            ]
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
    async def test_group_without_body_skips_rephrase(self, db_session, test_user, test_requisition):
        """Groups with no body skip rephrase but empty body is still sent."""
        mock_gc = AsyncMock()
        mock_gc.post_json.return_value = {}
        mock_gc.get_json.return_value = {"value": []}

        mock_rephrase = AsyncMock(return_value="Rephrased")

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

        # Only one rephrase call (for the group with body)
        assert mock_rephrase.call_count == 1


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
            created_at=datetime.now(timezone.utc),
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
            created_at=datetime.now(timezone.utc),
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
            created_at=datetime.now(timezone.utc),
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
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.flush()

        parsed = {"parts": [], "confidence": 0.7}

        _apply_parsed_result(vr, parsed, db_session)
        db_session.flush()

        activity = db_session.query(ActivityLog).filter(ActivityLog.activity_type == "offer_pending_review").first()
        # No offers extracted, so no notification
        assert activity is None

    def test_notification_exception_swallowed(self):
        """If ActivityLog creation fails, no exception propagates."""
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

        # Monkey-patch the needs_action/confidence so the condition triggers
        # _apply_parsed_result sets these on vr, so we need the real flow
        parsed = {"parts": [], "confidence": 0.65}

        # Should not raise
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
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.flush()

        with (
            patch(
                "app.utils.claude_client.claude_batch_submit",
                new_callable=AsyncMock,
                return_value="batch-123",
            ),
            patch("app.services.response_parser._clean_email_body", return_value="cleaned"),
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
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.flush()

        with (
            patch(
                "app.utils.claude_client.claude_batch_submit",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("app.services.response_parser._clean_email_body", return_value="cleaned"),
            patch("app.services.response_parser.RESPONSE_PARSE_SCHEMA", {"type": "object"}),
            patch("app.services.response_parser.SYSTEM_PROMPT", "System prompt"),
            pytest.raises(RuntimeError, match="no batch_id"),
        ):
            await _submit_parse_batch([vr], db_session)


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
        assert results[0]["match_method"] == "unmatched"
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
            last_sync_at=datetime.now(timezone.utc),
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
            last_sync_at=datetime.now(timezone.utc),
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
    async def test_delta_failure_fallback(self, db_session, test_user, test_requisition):
        """When delta query fails, fall back to full fetch."""
        mock_gc = AsyncMock()
        mock_gc.delta_query.side_effect = Exception("Delta gone")
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
    async def test_delta_failure_clears_stale_token(self, db_session, test_user, test_requisition):
        """When delta query fails (e.g. 410), the stale delta_token is cleared from
        SyncState."""
        sync = SyncState(
            user_id=test_user.id,
            folder="inbox",
            delta_token="stale-token-from-410",
            last_sync_at=datetime.now(timezone.utc),
        )
        db_session.add(sync)
        db_session.commit()

        mock_gc = AsyncMock()
        mock_gc.delta_query.side_effect = Exception("410 SyncStateNotFound")
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
    async def test_fallback_fetch_failure(self, db_session, test_user, test_requisition):
        """When fallback fetch also fails, return empty."""
        mock_gc = AsyncMock()
        mock_gc.get_json.side_effect = Exception("Network error")

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            results = await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
            )

        assert results == []

    @pytest.mark.asyncio
    async def test_dedup_already_processed(self, db_session, test_user, test_requisition):
        """Messages already in VendorResponse or ProcessedMessage are skipped."""
        # Pre-create a VendorResponse with msg-1
        existing_vr = VendorResponse(
            message_id="msg-1",
            vendor_name="Old Vendor",
            vendor_email="vendor@old.com",
            status="parsed",
            created_at=datetime.now(timezone.utc),
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
            processed_at=datetime.now(timezone.utc),
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
            created_at=datetime.now(timezone.utc),
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
        assert results[0]["match_method"] == "conversation_id"
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
            created_at=datetime.now(timezone.utc),
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
        assert results[0]["match_method"] == "subject_token"
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
        assert results[0]["match_method"] == "subject_token_req_only"
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
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(contact)
        db_session.commit()

        mock_gc = AsyncMock()
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
        assert results[0]["match_method"] == "email_exact"

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
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(contact)
        db_session.commit()

        mock_gc = AsyncMock()
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
        assert results[0]["match_method"] == "domain"

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
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(contact)
        db_session.commit()

        mock_gc = AsyncMock()
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
            created_at=datetime.now(timezone.utc),
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
    async def test_commit_failure_returns_empty(self, db_session, test_user, test_requisition):
        """If final commit fails, return empty results."""
        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {"value": [self._make_inbox_message()]}

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
            patch.object(db_session, "commit", side_effect=Exception("Commit failed")),
        ):
            results = await poll_inbox(
                token="fake-token",
                db=db_session,
                requisition_id=test_requisition.id,
            )

        assert results == []

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
            created_at=datetime.now(timezone.utc),
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
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(contact)
        db_session.commit()

        mock_gc = AsyncMock()
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
        assert results[0]["match_method"] == "unmatched"


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
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.flush()

        pb = PendingBatch(
            batch_id="batch-abc",
            batch_type="inbox_parse",
            request_map={"vr-" + str(vr.id): vr.id},
            status="processing",
            submitted_at=datetime.now(timezone.utc),
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
            submitted_at=datetime.now(timezone.utc),
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
            submitted_at=datetime.now(timezone.utc) - timedelta(hours=25),
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
            submitted_at=datetime.now(timezone.utc) - timedelta(hours=1),
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
            submitted_at=datetime.now(timezone.utc) - timedelta(hours=25),
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
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.flush()

        pb = PendingBatch(
            batch_id="batch-skip",
            batch_type="inbox_parse",
            request_map={"vr-" + str(vr.id): vr.id},
            status="processing",
            submitted_at=datetime.now(timezone.utc),
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
            submitted_at=datetime.now(timezone.utc),
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
            submitted_at=datetime.now(timezone.utc),
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
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.flush()

        pb = PendingBatch(
            batch_id="batch-json",
            batch_type="inbox_parse",
            request_map={"vr-" + str(vr.id): vr.id},
            status="processing",
            submitted_at=datetime.now(timezone.utc),
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
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.flush()

        pb = PendingBatch(
            batch_id="batch-bad-json",
            batch_type="inbox_parse",
            request_map={"vr-" + str(vr.id): vr.id},
            status="processing",
            submitted_at=datetime.now(timezone.utc),
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
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.flush()

        pb = PendingBatch(
            batch_id="batch-list",
            batch_type="inbox_parse",
            request_map={"vr-" + str(vr.id): vr.id},
            status="processing",
            submitted_at=datetime.now(timezone.utc),
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
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.flush()

        pb = PendingBatch(
            batch_id="batch-null",
            batch_type="inbox_parse",
            request_map={"vr-" + str(vr.id): vr.id},
            status="processing",
            submitted_at=datetime.now(timezone.utc),
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
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.flush()

        pb = PendingBatch(
            batch_id="batch-commit-fail",
            batch_type="inbox_parse",
            request_map={"vr-" + str(vr.id): vr.id},
            status="processing",
            submitted_at=datetime.now(timezone.utc),
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
            submitted_at=datetime.now(timezone.utc),
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
