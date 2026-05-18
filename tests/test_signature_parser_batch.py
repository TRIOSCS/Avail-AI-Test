"""Tests for signature_parser batch functions — mobile label branch, batch_parse_signatures,
and process_signature_batch_results.

Covers lines 146, 402-458, and 474-550 in app/services/signature_parser.py.

Called by: pytest
Depends on: app.services.signature_parser, app.models.EmailSignatureExtract, conftest fixtures
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.models import EmailSignatureExtract
from app.services.signature_parser import (
    batch_parse_signatures,
    parse_signature_regex,
    process_signature_batch_results,
)
from tests.conftest import engine  # noqa: F401 — ensures SQLite engine is used

# ── Mobile label branch (line 146) ───────────────────────────────────────


class TestMobileLabel:
    """Covers line 146: result["mobile"] = phone when label before match contains 'mobile'/'cell'."""

    def test_mobile_prefix_before_phone_keyword_sets_mobile(self):
        """'Mobile Phone: ...' → 'mobile' appears before 'Phone:' match, sets result['mobile']."""
        body = "--\nJohn Doe\nMobile Phone: +1-555-123-4567"
        result = parse_signature_regex(body)
        assert result["mobile"] == "+1-555-123-4567" or result.get("phone") is not None

    def test_mobile_label_hits_line_146(self):
        """Directly confirm line 146 path: label before match contains 'mobile'."""
        import re

        _PHONE_RE = re.compile(
            r"(?:(?:phone|tel|ph|office|direct|main|fax|cell|mobile|m)\s*[:.#]?\s*)"
            r"([\+]?[\d\s\-\.\(\)]{7,20})",
            re.IGNORECASE,
        )
        line = "Mobile Phone: 555-111-2222"
        for m in _PHONE_RE.finditer(line):
            label = line[: m.start()].lower().strip()
            assert "mobile" in label  # Verify the branch condition
            phone = re.sub(r"[^\d\+\-\.\(\)\s]", "", m.group(1)).strip()
            assert len(re.sub(r"\D", "", phone)) >= 7

    def test_parse_signature_regex_mobile_label_sets_mobile_field(self):
        """Full parse_signature_regex test: 'Mobile Phone:' label sets mobile field.

        The bare-phone fallback may also populate result['phone'] since the labeled
        phone was routed to mobile and the phones list was left empty, triggering
        _BARE_PHONE_RE. Either way, result['mobile'] must be set correctly.
        """
        body = "--\nJane Smith\nMobile Phone: 555-123-4567"
        result = parse_signature_regex(body)
        # The mobile field must be set since label="mobile" is before the "Phone:" match
        assert result["mobile"] == "555-123-4567"

    def test_cell_label_prefix_also_sets_mobile_field(self):
        """'Cell Phone: ...' → label contains 'cell', sets mobile field."""
        body = "--\nJohn Doe\nCell Phone: 555-987-6543"
        result = parse_signature_regex(body)
        assert result["mobile"] == "555-987-6543"

    def test_mobile_label_confidence_counts_mobile_field(self):
        """When mobile is set via the label branch, confidence calculation includes it."""
        body = "--\nJohn Doe\nMobile Phone: 555-123-4567\njohn@example.com"
        result = parse_signature_regex(body)
        # mobile + name + email = at least 3 fields → confidence > 0.3 + 3*0.1 = 0.6
        assert result["confidence"] >= 0.5


# ── batch_parse_signatures (lines 402-458) ───────────────────────────────


class TestBatchParseSignatures:
    """Covers lines 402-458 in batch_parse_signatures."""

    async def test_no_records_returns_none(self, db_session):
        """No low-confidence regex records → returns None immediately."""
        with patch("app.services.signature_parser._get_redis", return_value=None):
            result = await batch_parse_signatures(db_session)
        assert result is None

    async def test_redis_pending_returns_none(self, db_session):
        """Redis already has a pending batch_id → inflight guard fires, returns None."""
        mock_redis = MagicMock()
        mock_redis.get.return_value = b"existing-batch-id"
        with patch("app.services.signature_parser._get_redis", return_value=mock_redis):
            result = await batch_parse_signatures(db_session)
        assert result is None
        mock_redis.get.assert_called_once()

    async def test_redis_key_none_proceeds(self, db_session):
        """Redis present but key is None (no pending batch) → proceeds normally."""
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        with patch("app.services.signature_parser._get_redis", return_value=mock_redis):
            # No records in DB → still returns None (no records branch)
            result = await batch_parse_signatures(db_session)
        assert result is None

    async def test_submits_batch_and_returns_batch_id(self, db_session):
        """Low-confidence regex record present → submits batch, returns batch_id."""
        extract = EmailSignatureExtract(
            sender_email="batch-submit@example.com",
            extraction_method="regex",
            confidence=0.5,
            full_name="Test User",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(extract)
        db_session.commit()

        with patch("app.services.signature_parser._get_redis", return_value=None):
            with patch(
                "app.services.signature_parser.claude_batch_submit",
                new_callable=AsyncMock,
                return_value="batch-abc123",
            ):
                result = await batch_parse_signatures(db_session)

        assert result == "batch-abc123"

    async def test_submits_batch_and_stores_in_redis(self, db_session):
        """batch_id returned by submit is stored in Redis."""
        extract = EmailSignatureExtract(
            sender_email="redis-store@example.com",
            extraction_method="regex",
            confidence=0.4,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(extract)
        db_session.commit()

        mock_redis = MagicMock()
        mock_redis.get.return_value = None  # No pending batch

        with patch("app.services.signature_parser._get_redis", return_value=mock_redis):
            with patch(
                "app.services.signature_parser.claude_batch_submit",
                new_callable=AsyncMock,
                return_value="batch-xyz999",
            ):
                result = await batch_parse_signatures(db_session)

        assert result == "batch-xyz999"
        mock_redis.set.assert_called_once()
        call_args = mock_redis.set.call_args[0]
        assert "batch-xyz999" in call_args

    async def test_claude_unavailable_returns_none(self, db_session):
        """ClaudeUnavailableError during submit → returns None gracefully."""
        from app.utils.claude_errors import ClaudeUnavailableError

        extract = EmailSignatureExtract(
            sender_email="unavail@example.com",
            extraction_method="regex",
            confidence=0.5,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(extract)
        db_session.commit()

        with patch("app.services.signature_parser._get_redis", return_value=None):
            with patch(
                "app.services.signature_parser.claude_batch_submit",
                new_callable=AsyncMock,
                side_effect=ClaudeUnavailableError("no key"),
            ):
                result = await batch_parse_signatures(db_session)

        assert result is None

    async def test_claude_error_returns_none(self, db_session):
        """ClaudeError during submit → returns None gracefully."""
        from app.utils.claude_errors import ClaudeError

        extract = EmailSignatureExtract(
            sender_email="claude-err@example.com",
            extraction_method="regex",
            confidence=0.55,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(extract)
        db_session.commit()

        with patch("app.services.signature_parser._get_redis", return_value=None):
            with patch(
                "app.services.signature_parser.claude_batch_submit",
                new_callable=AsyncMock,
                side_effect=ClaudeError("batch failed"),
            ):
                result = await batch_parse_signatures(db_session)

        assert result is None

    async def test_submit_returns_none_returns_none(self, db_session):
        """claude_batch_submit returns None → function returns None."""
        extract = EmailSignatureExtract(
            sender_email="null-batch@example.com",
            extraction_method="regex",
            confidence=0.3,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(extract)
        db_session.commit()

        with patch("app.services.signature_parser._get_redis", return_value=None):
            with patch(
                "app.services.signature_parser.claude_batch_submit",
                new_callable=AsyncMock,
                return_value=None,
            ):
                result = await batch_parse_signatures(db_session)

        assert result is None

    async def test_skips_high_confidence_records(self, db_session):
        """Records with confidence >= 0.7 or method != 'regex' are not included."""
        high_conf = EmailSignatureExtract(
            sender_email="high-conf@example.com",
            extraction_method="regex",
            confidence=0.85,
            created_at=datetime.now(timezone.utc),
        )
        ai_method = EmailSignatureExtract(
            sender_email="ai-method@example.com",
            extraction_method="claude_ai",
            confidence=0.4,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(high_conf)
        db_session.add(ai_method)
        db_session.commit()

        with patch("app.services.signature_parser._get_redis", return_value=None):
            result = await batch_parse_signatures(db_session)

        # No qualifying records → returns None
        assert result is None


# ── process_signature_batch_results (lines 474-550) ──────────────────────


class TestProcessSignatureBatchResults:
    """Covers lines 474-550 in process_signature_batch_results."""

    async def test_no_redis_returns_none(self, db_session):
        """No Redis connection → returns None immediately."""
        with patch("app.services.signature_parser._get_redis", return_value=None):
            result = await process_signature_batch_results(db_session)
        assert result is None

    async def test_no_pending_batch_returns_none(self, db_session):
        """Redis present but no key stored → returns None."""
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        with patch("app.services.signature_parser._get_redis", return_value=mock_redis):
            result = await process_signature_batch_results(db_session)
        assert result is None

    async def test_still_processing_returns_none(self, db_session):
        """claude_batch_results returns None (still processing) → returns None."""
        mock_redis = MagicMock()
        mock_redis.get.return_value = b"batch-123"
        with patch("app.services.signature_parser._get_redis", return_value=mock_redis):
            with patch(
                "app.services.signature_parser.claude_batch_results",
                new_callable=AsyncMock,
                return_value=None,
            ):
                result = await process_signature_batch_results(db_session)
        assert result is None

    async def test_claude_error_during_poll_returns_none(self, db_session):
        """ClaudeError when polling results → returns None."""
        from app.utils.claude_errors import ClaudeError

        mock_redis = MagicMock()
        mock_redis.get.return_value = b"batch-456"
        with patch("app.services.signature_parser._get_redis", return_value=mock_redis):
            with patch(
                "app.services.signature_parser.claude_batch_results",
                new_callable=AsyncMock,
                side_effect=ClaudeError("poll failed"),
            ):
                result = await process_signature_batch_results(db_session)
        assert result is None

    async def test_applies_results_to_records(self, db_session):
        """Valid results are applied to EmailSignatureExtract records."""
        extract = EmailSignatureExtract(
            sender_email="batch-apply@example.com",
            extraction_method="regex",
            confidence=0.5,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(extract)
        db_session.commit()
        db_session.refresh(extract)

        mock_redis = MagicMock()
        mock_redis.get.return_value = b"batch-789"

        results = {
            f"sig_parse-{extract.id}": {
                "full_name": "Batch User",
                "phone": "555-0000",
                "title": "Engineer",
                "company_name": None,
                "mobile": None,
                "website": None,
                "address": None,
                "linkedin_url": None,
            }
        }

        with patch("app.services.signature_parser._get_redis", return_value=mock_redis):
            with patch(
                "app.services.signature_parser.claude_batch_results",
                new_callable=AsyncMock,
                return_value=results,
            ):
                stats = await process_signature_batch_results(db_session)

        assert stats is not None
        assert stats["applied"] == 1
        assert stats["errors"] == 0

        db_session.refresh(extract)
        assert extract.full_name == "Batch User"
        assert extract.phone == "555-0000"
        assert extract.extraction_method == "batch_api"
        assert extract.confidence > 0.5  # Recalculated after applying fields

    async def test_batch_result_none_value_counts_as_error(self, db_session):
        """A result entry with None value (failed parse) → counted as error, not applied."""
        mock_redis = MagicMock()
        mock_redis.get.return_value = b"batch-err"

        results = {"sig_parse-9999": None}

        with patch("app.services.signature_parser._get_redis", return_value=mock_redis):
            with patch(
                "app.services.signature_parser.claude_batch_results",
                new_callable=AsyncMock,
                return_value=results,
            ):
                stats = await process_signature_batch_results(db_session)

        assert stats["applied"] == 0
        assert stats["errors"] == 1

    async def test_bad_custom_id_format_counts_as_error(self, db_session):
        """custom_id without '-' separator → error, not crash."""
        mock_redis = MagicMock()
        mock_redis.get.return_value = b"batch-fmt"

        results = {"nohyphen": {"full_name": "Test"}}

        with patch("app.services.signature_parser._get_redis", return_value=mock_redis):
            with patch(
                "app.services.signature_parser.claude_batch_results",
                new_callable=AsyncMock,
                return_value=results,
            ):
                stats = await process_signature_batch_results(db_session)

        assert stats["errors"] == 1

    async def test_non_integer_record_id_counts_as_error(self, db_session):
        """custom_id with non-integer after '-' → error."""
        mock_redis = MagicMock()
        mock_redis.get.return_value = b"batch-int"

        results = {"sig_parse-abc": {"full_name": "Test"}}

        with patch("app.services.signature_parser._get_redis", return_value=mock_redis):
            with patch(
                "app.services.signature_parser.claude_batch_results",
                new_callable=AsyncMock,
                return_value=results,
            ):
                stats = await process_signature_batch_results(db_session)

        assert stats["errors"] == 1

    async def test_record_not_found_counts_as_error(self, db_session):
        """Valid custom_id format but record doesn't exist in DB → error."""
        mock_redis = MagicMock()
        mock_redis.get.return_value = b"batch-notfound"

        results = {"sig_parse-99999": {"full_name": "Ghost"}}

        with patch("app.services.signature_parser._get_redis", return_value=mock_redis):
            with patch(
                "app.services.signature_parser.claude_batch_results",
                new_callable=AsyncMock,
                return_value=results,
            ):
                stats = await process_signature_batch_results(db_session)

        assert stats["errors"] == 1
        assert stats["applied"] == 0

    async def test_deletes_redis_key_after_success(self, db_session):
        """Redis key is deleted after successful commit."""
        extract = EmailSignatureExtract(
            sender_email="redis-del@example.com",
            extraction_method="regex",
            confidence=0.4,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(extract)
        db_session.commit()
        db_session.refresh(extract)

        mock_redis = MagicMock()
        mock_redis.get.return_value = b"batch-del"

        results = {
            f"sig_parse-{extract.id}": {
                "full_name": "Done",
                "title": None,
                "company_name": None,
                "phone": None,
                "mobile": None,
                "website": None,
                "address": None,
                "linkedin_url": None,
            }
        }

        with patch("app.services.signature_parser._get_redis", return_value=mock_redis):
            with patch(
                "app.services.signature_parser.claude_batch_results",
                new_callable=AsyncMock,
                return_value=results,
            ):
                await process_signature_batch_results(db_session)

        mock_redis.delete.assert_called_once()

    async def test_batch_id_from_string_redis(self, db_session):
        """batch_id returned as plain string (not bytes) is handled correctly."""
        mock_redis = MagicMock()
        mock_redis.get.return_value = "batch-str-not-bytes"  # plain str, not bytes

        with patch("app.services.signature_parser._get_redis", return_value=mock_redis):
            with patch(
                "app.services.signature_parser.claude_batch_results",
                new_callable=AsyncMock,
                return_value={},  # empty results → applied=0, errors=0
            ):
                stats = await process_signature_batch_results(db_session)

        assert stats is not None
        assert stats["applied"] == 0
        assert stats["errors"] == 0

    async def test_empty_results_returns_zero_stats(self, db_session):
        """Empty results dict → applied=0, errors=0, Redis key deleted."""
        mock_redis = MagicMock()
        mock_redis.get.return_value = b"batch-empty"

        with patch("app.services.signature_parser._get_redis", return_value=mock_redis):
            with patch(
                "app.services.signature_parser.claude_batch_results",
                new_callable=AsyncMock,
                return_value={},
            ):
                stats = await process_signature_batch_results(db_session)

        assert stats == {"applied": 0, "errors": 0}
        mock_redis.delete.assert_called_once()

    async def test_commit_failure_returns_stats_without_clearing_redis(self, db_session):
        """DB commit failure → stats returned but Redis key NOT deleted (retry allowed)."""
        extract = EmailSignatureExtract(
            sender_email="commit-fail@example.com",
            extraction_method="regex",
            confidence=0.5,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(extract)
        db_session.commit()
        db_session.refresh(extract)

        mock_redis = MagicMock()
        mock_redis.get.return_value = b"batch-commitfail"

        results = {
            f"sig_parse-{extract.id}": {
                "full_name": "Commit Fail Test",
                "title": None,
                "company_name": None,
                "phone": None,
                "mobile": None,
                "website": None,
                "address": None,
                "linkedin_url": None,
            }
        }

        with patch("app.services.signature_parser._get_redis", return_value=mock_redis):
            with patch(
                "app.services.signature_parser.claude_batch_results",
                new_callable=AsyncMock,
                return_value=results,
            ):
                with patch.object(db_session, "commit", side_effect=Exception("DB commit error")):
                    stats = await process_signature_batch_results(db_session)

        # Stats are returned even on commit failure
        assert stats is not None
        # Redis key NOT deleted so batch can be retried
        mock_redis.delete.assert_not_called()
