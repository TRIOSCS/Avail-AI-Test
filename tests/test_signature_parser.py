"""Tests for signature_parser — regex extraction, AI fallback, orchestrator, and
caching.

Covers _extract_signature_block, parse_signature_regex, parse_signature_ai,
extract_signature, and cache_signature_extract.

Called by: pytest
Depends on: app.services.signature_parser, conftest fixtures
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.models import EmailSignatureExtract
from app.services.signature_parser import (
    _extract_signature_block,
    cache_signature_extract,
    extract_signature,
    parse_signature_ai,
    parse_signature_regex,
)
from tests.conftest import engine  # noqa: F401

# ── _extract_signature_block tests ─────────────────────────────────────


class TestExtractSignatureBlock:
    """Tests for _extract_signature_block (lines 87-108)."""

    @pytest.mark.parametrize(
        "body",
        ["", None, "   "],
        ids=["empty", "none", "whitespace"],
    )
    def test_blank_body_returns_empty(self, body):
        assert _extract_signature_block(body) == ""

    @pytest.mark.parametrize(
        "body, expected",
        [
            ("Hello,\nPlease see attached.\n--\nJohn Doe\nSales Manager", ["John Doe", "Sales Manager"]),
            ("I will send the quote shortly.\nThanks,\nJane Smith\nDirector", ["Thanks", "Jane Smith"]),
            ("See the pricing below.\nRegards,\nBob Jones\nVP Sales", ["Bob Jones"]),
            ("Let me know if you need anything.\nBest,\nAlice\nEngineer", ["Alice"]),
            ("We look forward to working with you.\nSincerely,\nTom\nCEO", ["Tom"]),
            ("Talk soon.\nCheers,\nMike", ["Mike"]),
            ("Please confirm.\nWarm regards,\nSarah", ["Sarah"]),
            ("Attached is the PO.\nKind regards,\nDave", ["Dave"]),
            ("Let me know.\nBest regards,\nEve", ["Eve"]),
            ("Got it.\nSent from my iPhone\nJohn", ["Sent from"]),
            ("Please review.\n___\nContact Info\nPhone: 555-1234", ["Contact Info"]),
            ("Thanks for reaching out.\n——\nJohn Doe\nSales", ["John Doe"]),
        ],
        ids=[
            "dash",
            "thanks",
            "regards",
            "best",
            "sincerely",
            "cheers",
            "warm_regards",
            "kind_regards",
            "best_regards",
            "sent_from",
            "underscore",
            "em_dash",
        ],
    )
    def test_delimiter(self, body, expected):
        result = _extract_signature_block(body)
        for fragment in expected:
            assert fragment in result

    def test_no_delimiter_uses_last_15_lines(self):
        lines = [f"Line {i}" for i in range(30)]
        body = "\n".join(lines)
        result = _extract_signature_block(body)
        assert "Line 15" in result
        assert "Line 29" in result
        assert "Line 14" not in result

    def test_short_body_no_delimiter(self):
        body = "John Doe\nSales Manager\njohn@example.com"
        result = _extract_signature_block(body)
        assert "John Doe" in result
        assert "john@example.com" in result


# ── parse_signature_regex tests ────────────────────────────────────────


class TestParseSignatureRegex:
    """Tests for parse_signature_regex (lines 119-217)."""

    def test_empty_body_returns_zero_confidence(self):
        result = parse_signature_regex("")
        assert result["confidence"] == 0.0

    def test_full_signature_with_dash_delimiter(self):
        """Full signature using -- delimiter so name is first real line."""
        body = (
            "Please see the quote.\n"
            "--\n"
            "John Doe\n"
            "Sales Manager\n"
            "Acme Electronics\n"
            "Phone: 555-123-4567\n"
            "john.doe@acme.com\n"
            "https://www.linkedin.com/in/johndoe\n"
            "www.acme.com\n"
        )
        result = parse_signature_regex(body)
        assert result["full_name"] == "John Doe"
        assert result["title"] == "Sales Manager"
        assert result["email"] == "john.doe@acme.com"
        assert result["phone"] is not None
        assert result["linkedin_url"] is not None
        assert "linkedin.com" in result["linkedin_url"]
        assert result["website"] is not None
        assert result["confidence"] >= 0.7

    @pytest.mark.parametrize(
        "body",
        [
            "--\nJohn Doe\nTel: +1-555-123-4567",
            "--\nJohn\nOffice: 555-111-2222",
            "--\nJohn\nDirect: 555-111-2222",
            # Cell/Mobile labels are part of the regex match; the pre-match label check
            # only triggers when redundant label text precedes the match. Both labeled
            # phones get captured and the first goes to result["phone"].
            "--\nJohn Doe\nCell: 555-111-2222\nMobile: 555-222-3333",
            # 'Cell:' at line start means the label is inside the match, not before it,
            # so phone is used instead of the labeled-phones list.
            "--\nJohn Doe\nCell: 555-111-2222",
        ],
        ids=[
            "tel_label",
            "office_label",
            "direct_label",
            "cell_and_mobile_labels",
            "cell_label_at_line_start",
        ],
    )
    def test_labeled_phone_extracted(self, body):
        result = parse_signature_regex(body)
        assert result["phone"] is not None

    def test_bare_phone_fallback(self):
        body = "--\nJohn Doe\n555-444-5555"
        result = parse_signature_regex(body)
        assert result["phone"] == "555-444-5555"

    def test_email_extraction(self):
        body = "--\nJohn Doe\njohn@example.com"
        result = parse_signature_regex(body)
        assert result["email"] == "john@example.com"

    @pytest.mark.parametrize(
        "body",
        [
            "--\nJohn Doe\nlinkedin.com/in/johndoe",
            "--\nJohn Doe\nhttps://linkedin.com/in/johndoe",
        ],
        ids=["bare", "with_https"],
    )
    def test_linkedin_extraction(self, body):
        result = parse_signature_regex(body)
        assert result["linkedin_url"] == "https://linkedin.com/in/johndoe"

    def test_website_extraction_skips_linkedin(self):
        body = "--\nJohn Doe\nhttps://linkedin.com/in/johndoe\nwww.acme-electronics.com\n"
        result = parse_signature_regex(body)
        assert result["website"] is not None
        assert "linkedin" not in result["website"].lower()
        assert "acme" in result["website"].lower()

    def test_name_detection_proper_case(self):
        body = "--\nJohn Smith\nSales Director\nAcme Corp"
        result = parse_signature_regex(body)
        assert result["full_name"] == "John Smith"

    def test_name_with_particles(self):
        body = "--\nJean de La Fontaine\nManager"
        result = parse_signature_regex(body)
        assert result["full_name"] == "Jean de La Fontaine"

    def test_title_detection_keywords(self):
        body = "--\nJohn Doe\nVice President of Sales"
        result = parse_signature_regex(body)
        assert result["title"] is not None
        assert "Vice President" in result["title"]

    def test_company_name_after_name_and_title(self):
        body = "--\nJohn Doe\nSales Manager\nAcme Electronics Inc"
        result = parse_signature_regex(body)
        assert result["company_name"] is not None

    @pytest.mark.parametrize(
        "body",
        [
            "--\njohn@example.com\nJohn Doe\nManager",
            "--\nPhone: 555-111-2222\nJohn Doe",
            "--\nhttps://example.com\nJohn Doe",
            "Sent from my iPhone\nJohn Doe\nManager",
        ],
        ids=["email_line", "phone_line", "url_line", "sent_from_line"],
    )
    def test_skips_non_name_line_for_name(self, body):
        result = parse_signature_regex(body)
        assert result["full_name"] == "John Doe"

    def test_confidence_scales_with_fields(self):
        # Minimal signature — only name
        body = "--\nJohn Doe"
        result = parse_signature_regex(body)
        low_confidence = result["confidence"]

        # Rich signature
        body_rich = (
            "--\n"
            "John Doe\n"
            "Sales Manager\n"
            "Acme Corp\n"
            "Phone: 555-123-4567\n"
            "Mobile: 555-987-6543\n"
            "john@acme.com\n"
            "www.acme.com\n"
        )
        result_rich = parse_signature_regex(body_rich)
        high_confidence = result_rich["confidence"]
        assert high_confidence > low_confidence

    def test_confidence_capped_at_0_9(self):
        body = (
            "--\n"
            "John Doe\n"
            "CEO\n"
            "Acme Corp\n"
            "Phone: 555-111-2222\n"
            "Mobile: 555-333-4444\n"
            "john@acme.com\n"
            "https://linkedin.com/in/johndoe\n"
            "www.acme.com\n"
        )
        result = parse_signature_regex(body)
        assert result["confidence"] <= 0.9

    def test_single_word_name(self):
        body = "--\nPrince\nMusician"
        result = parse_signature_regex(body)
        assert result["full_name"] == "Prince"

    def test_short_token_not_detected_as_name(self):
        """Single character lines (< 2 chars) are skipped."""
        body = "--\nA\n555-111-2222"
        result = parse_signature_regex(body)
        assert result["full_name"] is None


# ── parse_signature_ai tests ──────────────────────────────────────────


class TestParseSignatureAI:
    """Tests for parse_signature_ai (lines 225-272)."""

    def test_empty_body_returns_zero_confidence(self):
        result = asyncio.get_event_loop().run_until_complete(parse_signature_ai(""))
        assert result["confidence"] == 0.0

    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    def test_none_body_returns_zero_confidence(self, mock_claude):
        result = asyncio.get_event_loop().run_until_complete(parse_signature_ai(None))
        assert result["confidence"] == 0.0
        mock_claude.assert_not_called()

    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    def test_successful_ai_parse(self, mock_claude):
        mock_claude.return_value = {
            "full_name": "John Doe",
            "title": "Sales Manager",
            "company_name": "Acme Corp",
            "phone": "555-123-4567",
            "mobile": "555-987-6543",
            "website": "acme.com",
            "address": "123 Main St",
            "linkedin_url": "https://linkedin.com/in/johndoe",
        }
        body = "--\nJohn Doe\nSales Manager\nAcme Corp"
        result = asyncio.get_event_loop().run_until_complete(
            parse_signature_ai(body, sender_name="John Doe", sender_email="john@acme.com")
        )
        assert result["full_name"] == "John Doe"
        assert result["title"] == "Sales Manager"
        assert result["email"] == "john@acme.com"
        assert result["confidence"] > 0.5

    @pytest.mark.parametrize(
        "return_value, side_effect",
        [
            (None, None),
            ("not a dict", None),
            (None, Exception("API timeout")),
        ],
        ids=["returns_none", "returns_non_dict", "raises"],
    )
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    def test_ai_bad_response_returns_zero_confidence(self, mock_claude, return_value, side_effect):
        mock_claude.return_value = return_value
        mock_claude.side_effect = side_effect
        body = "--\nJohn Doe"
        result = asyncio.get_event_loop().run_until_complete(parse_signature_ai(body))
        assert result["confidence"] == 0.0

    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    def test_ai_confidence_capped_at_0_95(self, mock_claude):
        mock_claude.return_value = {
            "full_name": "John Doe",
            "title": "CEO",
            "company_name": "Acme",
            "phone": "555-1111",
            "mobile": "555-2222",
            "website": "acme.com",
            "address": "123 Main",
            "linkedin_url": "https://linkedin.com/in/jdoe",
        }
        body = "--\nJohn Doe\nCEO"
        result = asyncio.get_event_loop().run_until_complete(parse_signature_ai(body, sender_email="john@acme.com"))
        # 9 fields (8 from AI + email) * 0.08 + 0.5 = 1.22 -> capped at 0.95
        assert result["confidence"] == 0.95

    @pytest.mark.parametrize(
        "sender_email, expected_email",
        [
            ("john@example.com", "john@example.com"),
            ("", None),
        ],
        ids=["with_sender_email", "no_sender_email"],
    )
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    def test_ai_sender_email_in_result(self, mock_claude, sender_email, expected_email):
        mock_claude.return_value = {"full_name": "John"}
        body = "--\nJohn"
        result = asyncio.get_event_loop().run_until_complete(parse_signature_ai(body, sender_email=sender_email))
        assert result["email"] == expected_email

    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    def test_ai_truncates_long_body(self, mock_claude):
        mock_claude.return_value = {"full_name": "John"}
        body = "--\n" + "A" * 5000
        asyncio.get_event_loop().run_until_complete(parse_signature_ai(body))
        call_args = mock_claude.call_args
        prompt = call_args[0][0]
        # sig_block is truncated to 2000 chars before being embedded in prompt
        assert len(prompt) < 5000


# ── extract_signature orchestrator tests ───────────────────────────────


class TestExtractSignature:
    """Tests for extract_signature (lines 277-292)."""

    def test_high_confidence_regex_skips_ai(self):
        body = (
            "--\n"
            "John Doe\n"
            "Sales Manager\n"
            "Acme Corp\n"
            "Phone: 555-123-4567\n"
            "Mobile: 555-987-6543\n"
            "john@acme.com\n"
            "www.acme.com\n"
        )
        with patch("app.services.signature_parser.parse_signature_ai") as mock_ai:
            result = asyncio.get_event_loop().run_until_complete(extract_signature(body))
            mock_ai.assert_not_called()
        assert result["extraction_method"] == "regex"
        assert result["confidence"] >= 0.7

    @patch("app.services.signature_parser.parse_signature_ai", new_callable=AsyncMock)
    def test_low_confidence_regex_calls_ai(self, mock_ai):
        mock_ai.return_value = {
            "full_name": "John Doe",
            "title": "Manager",
            "confidence": 0.85,
        }
        # Minimal body that gives low regex confidence
        body = "--\nJohn"
        result = asyncio.get_event_loop().run_until_complete(
            extract_signature(body, sender_name="John", sender_email="john@test.com")
        )
        mock_ai.assert_called_once()
        assert result["extraction_method"] == "claude_ai"

    @pytest.mark.parametrize(
        "return_value, side_effect",
        [
            ({"confidence": 0.1}, None),
            (None, Exception("Claude down")),
        ],
        ids=["ai_lower_confidence", "ai_exception"],
    )
    @patch("app.services.signature_parser.parse_signature_ai", new_callable=AsyncMock)
    def test_keeps_regex_when_ai_does_not_win(self, mock_ai, return_value, side_effect):
        mock_ai.return_value = return_value
        mock_ai.side_effect = side_effect
        body = "--\nJohn Doe\nManager"
        result = asyncio.get_event_loop().run_until_complete(extract_signature(body))
        assert result["extraction_method"] == "regex"


# ── cache_signature_extract tests ──────────────────────────────────────


class TestCacheSignatureExtract:
    """Tests for cache_signature_extract (lines 297-344)."""

    def test_creates_new_record(self, db_session):
        extract = {
            "full_name": "Jane Smith",
            "title": "Director",
            "company_name": "Test Corp",
            "phone": "555-111-2222",
            "mobile": "555-333-4444",
            "website": "testcorp.com",
            "address": "456 Oak Ave",
            "linkedin_url": "https://linkedin.com/in/janesmith",
            "extraction_method": "regex",
            "confidence": 0.8,
        }
        cache_signature_extract(db_session, "jane@testcorp.com", extract)

        record = (
            db_session.query(EmailSignatureExtract)
            .filter(EmailSignatureExtract.sender_email == "jane@testcorp.com")
            .first()
        )
        assert record is not None
        assert record.full_name == "Jane Smith"
        assert record.title == "Director"
        assert record.company_name == "Test Corp"
        assert record.phone == "555-111-2222"
        assert record.confidence == 0.8
        assert record.extraction_method == "regex"

    def test_email_lowercased(self, db_session):
        extract = {"full_name": "Test", "confidence": 0.5}
        cache_signature_extract(db_session, "UPPER@CASE.COM", extract)

        record = (
            db_session.query(EmailSignatureExtract)
            .filter(EmailSignatureExtract.sender_email == "upper@case.com")
            .first()
        )
        assert record is not None

    def test_upsert_increments_seen_count(self, db_session):
        initial = EmailSignatureExtract(
            sender_email="repeat@example.com",
            full_name="First Parse",
            extraction_method="regex",
            confidence=0.4,
            seen_count=1,
            created_at=datetime.now(UTC),
        )
        db_session.add(initial)
        db_session.commit()
        db_session.refresh(initial)

        extract = {"full_name": "Second Parse", "confidence": 0.3}
        cache_signature_extract(db_session, "repeat@example.com", extract)
        db_session.refresh(initial)

        assert initial.seen_count == 2
        assert initial.full_name == "First Parse"  # Not overwritten (lower confidence)

    def test_upsert_updates_fields_with_higher_confidence(self, db_session):
        initial = EmailSignatureExtract(
            sender_email="update@example.com",
            full_name="Old Name",
            title="Old Title",
            extraction_method="regex",
            confidence=0.4,
            seen_count=1,
            created_at=datetime.now(UTC),
        )
        db_session.add(initial)
        db_session.commit()
        db_session.refresh(initial)

        extract = {
            "full_name": "New Name",
            "title": "New Title",
            "company_name": "New Corp",
            "phone": "555-999-8888",
            "extraction_method": "claude_ai",
            "confidence": 0.9,
        }
        cache_signature_extract(db_session, "update@example.com", extract)
        db_session.refresh(initial)

        assert initial.full_name == "New Name"
        assert initial.title == "New Title"
        assert initial.company_name == "New Corp"
        assert initial.confidence == 0.9
        assert initial.extraction_method == "claude_ai"
        assert initial.seen_count == 2

    def test_upsert_skips_null_fields_on_update(self, db_session):
        initial = EmailSignatureExtract(
            sender_email="partial@example.com",
            full_name="Keep This",
            title="Keep Title",
            extraction_method="regex",
            confidence=0.3,
            seen_count=1,
            created_at=datetime.now(UTC),
        )
        db_session.add(initial)
        db_session.commit()
        db_session.refresh(initial)

        extract = {
            "full_name": None,
            "title": "Updated Title",
            "company_name": "New Co",
            "confidence": 0.8,
            "extraction_method": "claude_ai",
        }
        cache_signature_extract(db_session, "partial@example.com", extract)
        db_session.refresh(initial)

        assert initial.full_name == "Keep This"  # Preserved (None not applied)
        assert initial.title == "Updated Title"
        assert initial.company_name == "New Co"

    def test_flush_error_rolls_back(self, db_session):
        extract = {"full_name": "Test", "confidence": 0.5}
        with patch.object(db_session, "flush", side_effect=Exception("DB error")):
            cache_signature_extract(db_session, "error@example.com", extract)

    def test_new_record_defaults(self, db_session):
        extract = {"confidence": 0.5, "extraction_method": "regex"}
        cache_signature_extract(db_session, "defaults@example.com", extract)

        record = (
            db_session.query(EmailSignatureExtract)
            .filter(EmailSignatureExtract.sender_email == "defaults@example.com")
            .first()
        )
        assert record is not None
        assert record.full_name is None
        assert record.title is None
        assert record.extraction_method == "regex"
        assert record.confidence == 0.5
