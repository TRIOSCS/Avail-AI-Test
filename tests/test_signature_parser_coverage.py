"""
test_signature_parser_coverage.py -- Full coverage tests for signature_parser.py

Targets missing lines: 123, 130, 165, 171, 202-249, 254-270, 314-316
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.services.signature_parser import (
    _extract_signature_block,
    cache_signature_extract,
    extract_signature,
    parse_signature_ai,
    parse_signature_gradient,
    parse_signature_regex,
)


class TestExtractSignatureBlock:
    def test_empty_body_returns_empty(self):
        assert _extract_signature_block("") == ""
        assert _extract_signature_block(None) == ""

    def test_delimiter_dash(self):
        body = "Hello\n\n---\nJohn Doe\nCEO\nAcme Corp"
        block = _extract_signature_block(body)
        assert "John Doe" in block

    def test_delimiter_regards(self):
        body = "Please review.\n\nRegards,\nJane Smith\nDirector"
        block = _extract_signature_block(body)
        assert "Jane Smith" in block

    def test_delimiter_best(self):
        body = "See attached.\n\nBest,\nBob\nManager"
        block = _extract_signature_block(body)
        assert "Bob" in block

    def test_no_delimiter_uses_last_15(self):
        lines = [f"Line {i}" for i in range(20)]
        body = "\n".join(lines)
        block = _extract_signature_block(body)
        assert "Line 5" in block
        assert "Line 19" in block


class TestParseSignatureRegex:
    def test_empty_body(self):
        result = parse_signature_regex("")
        assert result == {"confidence": 0.0}

    def test_basic_extraction(self):
        body = (
            "Hello please review.\n\n---\nJohn Doe\nCEO\nAcme Corp\nPhone: 555-123-4567\njohn@acme.com\nwww.acme.com\n"
        )
        result = parse_signature_regex(body)
        assert result["full_name"] == "John Doe"
        assert result["title"] == "CEO"
        assert result["phone"] is not None
        assert result["email"] == "john@acme.com"
        assert result["website"] is not None
        assert result["confidence"] > 0.3

    def test_mobile_phone_label(self):
        """Line 123: mobile label in text before phone regex match sets mobile."""
        body = "Hello.\n\n---\nJohn Doe\nMobile/Cell | Tel: 555-987-6543\njohn@acme.com\n"
        result = parse_signature_regex(body)
        assert result["mobile"] == "555-987-6543"

    def test_cell_phone_label(self):
        """Line 123: cell label triggers mobile field."""
        body = "Hello.\n\n---\nJohn Doe\nCell/Direct | Office: 555-111-2222\njohn@acme.com\n"
        result = parse_signature_regex(body)
        assert result["mobile"] == "555-111-2222"

    def test_bare_phone_fallback(self):
        """Line 130: bare phone regex fallback when no labeled phones found."""
        body = "Hello.\n\n---\nJohn Doe\n555-444-3333\njohn@acme.com\n"
        result = parse_signature_regex(body)
        assert result["phone"] == "555-444-3333"

    def test_linkedin_url_without_scheme(self):
        body = "Hello.\n\n---\nJohn Doe\nlinkedin.com/in/johndoe\n"
        result = parse_signature_regex(body)
        assert result["linkedin_url"] == "https://linkedin.com/in/johndoe"

    def test_linkedin_url_with_https(self):
        body = "Hello.\n\n---\nJohn Doe\nhttps://linkedin.com/in/johndoe\n"
        result = parse_signature_regex(body)
        assert result["linkedin_url"] == "https://linkedin.com/in/johndoe"

    def test_website_skips_linkedin(self):
        body = "Hello.\n\n---\nJohn Doe\nlinkedin.com/in/johndoe\nwww.acme.com\n"
        result = parse_signature_regex(body)
        assert result["website"] == "acme.com"

    def test_skip_short_line(self):
        """Line 165: skip lines shorter than 2 chars."""
        body = "Hello.\n\n---\nA\nJohn Doe\nCEO\n"
        result = parse_signature_regex(body)
        assert result["full_name"] == "John Doe"

    def test_skip_email_line_for_name(self):
        body = "Hello.\n\n---\njohn@acme.com\nJohn Doe\nCEO\n"
        result = parse_signature_regex(body)
        assert result["full_name"] == "John Doe"

    def test_skip_phone_line_for_name(self):
        body = "Hello.\n\n---\nPhone: 555-123-4567\nJohn Doe\nCEO\n"
        result = parse_signature_regex(body)
        assert result["full_name"] == "John Doe"

    def test_skip_linkedin_line_for_name(self):
        body = "Hello.\n\n---\nlinkedin.com/in/johndoe\nJohn Doe\nCEO\n"
        result = parse_signature_regex(body)
        assert result["full_name"] == "John Doe"

    def test_skip_http_line_for_name(self):
        body = "Hello.\n\n---\nhttps://example.com\nJohn Doe\nCEO\n"
        result = parse_signature_regex(body)
        assert result["full_name"] == "John Doe"

    def test_skip_sent_from_line(self):
        """Line 171: skip 'sent from' lines."""
        body = "Hello.\n\n---\nSent from my iPhone\nJohn Doe\nDirector\n"
        result = parse_signature_regex(body)
        assert result["full_name"] == "John Doe"

    def test_skip_dash_delimiter_line(self):
        """Line 171: skip '---' delimiter lines."""
        body = "Hello.\n\n---\nJohn Doe\nManager\n"
        result = parse_signature_regex(body)
        assert result["full_name"] == "John Doe"

    def test_skip_underscore_delimiter_line(self):
        """Line 171: skip '___' delimiter lines."""
        body = "Hello.\n\n___\nJohn Doe\nAnalyst\n"
        result = parse_signature_regex(body)
        assert result["full_name"] == "John Doe"

    def test_company_name_after_name_and_title(self):
        body = "Hello.\n\n---\nJohn Doe\nVP Sales\nAcme Corp International\n"
        result = parse_signature_regex(body)
        assert result["full_name"] == "John Doe"
        assert result["company_name"] is not None

    def test_name_with_particles(self):
        body = "Hello.\n\n---\nJan van Houten\nCEO\n"
        result = parse_signature_regex(body)
        assert result["full_name"] == "Jan van Houten"

    def test_confidence_capped_at_09(self):
        body = (
            "Hello.\n\n---\n"
            "John Doe\nCEO\nAcme Corp\n"
            "Phone: 555-123-4567\njohn@acme.com\n"
            "www.acme.com\nlinkedin.com/in/johndoe\n"
        )
        result = parse_signature_regex(body)
        assert result["confidence"] == 0.9


class TestParseSignatureAI:
    @pytest.mark.asyncio
    async def test_empty_body_returns_zero(self):
        result = await parse_signature_ai("")
        assert result == {"confidence": 0.0}

    @pytest.mark.asyncio
    async def test_successful_ai_parse(self):
        body = "Hello.\n\n---\nJohn Doe\nCEO, Acme Corp\njohn@acme.com\n"
        ai_response = {
            "full_name": "John Doe",
            "title": "CEO",
            "company_name": "Acme Corp",
            "phone": "555-123-4567",
            "mobile": None,
            "website": "acme.com",
            "linkedin_url": None,
            "address": "123 Main St",
        }
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=ai_response):
            result = await parse_signature_ai(body, "John Doe", "john@acme.com")
        assert result["full_name"] == "John Doe"
        assert result["title"] == "CEO"
        assert result["company_name"] == "Acme Corp"
        assert result["email"] == "john@acme.com"
        assert result["confidence"] > 0.5

    @pytest.mark.asyncio
    async def test_ai_returns_none(self):
        body = "Hello.\n\n---\nJohn Doe\nCEO\n"
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=None):
            result = await parse_signature_ai(body)
        assert result == {"confidence": 0.0}

    @pytest.mark.asyncio
    async def test_ai_returns_non_dict(self):
        body = "Hello.\n\n---\nJohn Doe\nCEO\n"
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value="bad"):
            result = await parse_signature_ai(body)
        assert result == {"confidence": 0.0}

    @pytest.mark.asyncio
    async def test_ai_exception(self):
        body = "Hello.\n\n---\nJohn Doe\nCEO\n"
        with patch(
            "app.utils.claude_client.claude_json", new_callable=AsyncMock, side_effect=RuntimeError("API error")
        ):
            result = await parse_signature_ai(body)
        assert result == {"confidence": 0.0}

    @pytest.mark.asyncio
    async def test_ai_confidence_capped_at_095(self):
        body = "Hello.\n\n---\nJohn Doe\nCEO\n"
        ai_response = {
            "full_name": "John Doe",
            "title": "CEO",
            "company_name": "Acme",
            "phone": "555-123-4567",
            "mobile": "555-987-6543",
            "website": "acme.com",
            "linkedin_url": "https://linkedin.com/in/jd",
            "address": "123 Main St",
        }
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=ai_response):
            result = await parse_signature_ai(body, "John Doe", "john@acme.com")
        assert result["confidence"] == 0.95

    @pytest.mark.asyncio
    async def test_ai_no_sender_email_yields_none(self):
        body = "Hello.\n\n---\nJohn Doe\nCEO\n"
        ai_response = {
            "full_name": "John",
            "title": None,
            "company_name": None,
            "phone": None,
            "mobile": None,
            "website": None,
            "linkedin_url": None,
            "address": None,
        }
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=ai_response):
            result = await parse_signature_ai(body, "John", "")
        assert result["email"] is None


class TestParseSignatureGradient:
    """Tests for parse_signature_gradient (lines 257-303)."""

    @pytest.mark.asyncio
    async def test_empty_body_returns_zero(self):
        with patch("app.services.gradient_service.gradient_json", new_callable=AsyncMock):
            result = await parse_signature_gradient("")
        assert result == {"confidence": 0.0}

    @pytest.mark.asyncio
    async def test_successful_gradient_parse(self):
        body = "Hello.\n\n---\nJohn Doe\nCEO, Acme Corp\njohn@acme.com\n"
        gradient_response = {
            "full_name": "John Doe",
            "title": "CEO",
            "company_name": "Acme Corp",
            "phone": "555-123-4567",
            "mobile": None,
            "website": "acme.com",
            "linkedin_url": None,
            "address": "123 Main St",
        }
        with patch(
            "app.services.gradient_service.gradient_json",
            new_callable=AsyncMock,
            return_value=gradient_response,
        ):
            result = await parse_signature_gradient(body, "John Doe", "john@acme.com")
        assert result["full_name"] == "John Doe"
        assert result["title"] == "CEO"
        assert result["company_name"] == "Acme Corp"
        assert result["email"] == "john@acme.com"
        assert result["confidence"] > 0.5

    @pytest.mark.asyncio
    async def test_gradient_returns_none(self):
        body = "Hello.\n\n---\nJohn Doe\nCEO\n"
        with patch(
            "app.services.gradient_service.gradient_json",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await parse_signature_gradient(body)
        assert result == {"confidence": 0.0}

    @pytest.mark.asyncio
    async def test_gradient_returns_non_dict(self):
        body = "Hello.\n\n---\nJohn Doe\nCEO\n"
        with patch(
            "app.services.gradient_service.gradient_json",
            new_callable=AsyncMock,
            return_value="not a dict",
        ):
            result = await parse_signature_gradient(body)
        assert result == {"confidence": 0.0}

    @pytest.mark.asyncio
    async def test_gradient_exception(self):
        body = "Hello.\n\n---\nJohn Doe\nCEO\n"
        with patch(
            "app.services.gradient_service.gradient_json",
            new_callable=AsyncMock,
            side_effect=RuntimeError("API error"),
        ):
            result = await parse_signature_gradient(body)
        assert result == {"confidence": 0.0}

    @pytest.mark.asyncio
    async def test_gradient_confidence_capped_at_095(self):
        body = "Hello.\n\n---\nJohn Doe\nCEO\n"
        gradient_response = {
            "full_name": "John Doe",
            "title": "CEO",
            "company_name": "Acme",
            "phone": "555-123-4567",
            "mobile": "555-987-6543",
            "website": "acme.com",
            "linkedin_url": "https://linkedin.com/in/jd",
            "address": "123 Main St",
        }
        with patch(
            "app.services.gradient_service.gradient_json",
            new_callable=AsyncMock,
            return_value=gradient_response,
        ):
            result = await parse_signature_gradient(body, "John Doe", "john@acme.com")
        assert result["confidence"] == 0.95

    @pytest.mark.asyncio
    async def test_gradient_no_sender_email_yields_none(self):
        body = "Hello.\n\n---\nJohn Doe\nCEO\n"
        gradient_response = {
            "full_name": "John",
            "title": None,
            "company_name": None,
            "phone": None,
            "mobile": None,
            "website": None,
            "linkedin_url": None,
            "address": None,
        }
        with patch(
            "app.services.gradient_service.gradient_json",
            new_callable=AsyncMock,
            return_value=gradient_response,
        ):
            result = await parse_signature_gradient(body, "John", "")
        assert result["email"] is None


class TestExtractSignatureGradientPath:
    """Tests for Gradient path in extract_signature (lines 319-324)."""

    @pytest.mark.asyncio
    async def test_gradient_beats_regex(self):
        """When Gradient confidence > regex confidence, Gradient is returned."""
        low_regex = {"full_name": "John", "confidence": 0.4}
        gradient_result = {"full_name": "John Doe", "title": "CEO", "confidence": 0.85}
        mock_settings = type("S", (), {"do_gradient_api_key": "fake-key"})()

        with (
            patch("app.services.signature_parser.parse_signature_regex", return_value=low_regex),
            patch(
                "app.services.signature_parser.parse_signature_gradient",
                new_callable=AsyncMock,
                return_value=gradient_result,
            ),
            patch(
                "app.services.signature_parser.parse_signature_ai",
                new_callable=AsyncMock,
                return_value={"confidence": 0.3},
            ),
            patch("app.config.settings", mock_settings),
        ):
            result = await extract_signature("body", "John", "john@acme.com")
        assert result["extraction_method"] == "gradient_ai"
        assert result["confidence"] == 0.85

    @pytest.mark.asyncio
    async def test_gradient_lower_than_regex_falls_through(self):
        """When Gradient confidence < regex confidence, falls through to Claude."""
        low_regex = {"full_name": "John", "confidence": 0.5}
        gradient_result = {"full_name": "John", "confidence": 0.3}
        higher_ai = {"full_name": "John Doe", "confidence": 0.8}
        mock_settings = type("S", (), {"do_gradient_api_key": "fake-key"})()

        with (
            patch("app.services.signature_parser.parse_signature_regex", return_value=low_regex),
            patch(
                "app.services.signature_parser.parse_signature_gradient",
                new_callable=AsyncMock,
                return_value=gradient_result,
            ),
            patch("app.services.signature_parser.parse_signature_ai", new_callable=AsyncMock, return_value=higher_ai),
            patch("app.config.settings", mock_settings),
        ):
            result = await extract_signature("body", "John", "john@acme.com")
        assert result["extraction_method"] == "claude_ai"

    @pytest.mark.asyncio
    async def test_gradient_exception_falls_through_to_ai(self):
        """When Gradient raises, falls through to Claude AI."""
        low_regex = {"full_name": "John", "confidence": 0.4}
        higher_ai = {"full_name": "John Doe", "confidence": 0.8}
        mock_settings = type("S", (), {"do_gradient_api_key": "fake-key"})()

        with (
            patch("app.services.signature_parser.parse_signature_regex", return_value=low_regex),
            patch(
                "app.services.signature_parser.parse_signature_gradient",
                new_callable=AsyncMock,
                side_effect=RuntimeError("Gradient down"),
            ),
            patch("app.services.signature_parser.parse_signature_ai", new_callable=AsyncMock, return_value=higher_ai),
            patch("app.config.settings", mock_settings),
        ):
            result = await extract_signature("body", "John", "john@acme.com")
        assert result["extraction_method"] == "claude_ai"

    @pytest.mark.asyncio
    async def test_no_gradient_key_skips_gradient(self):
        """When do_gradient_api_key is empty, Gradient is skipped."""
        low_regex = {"full_name": "John", "confidence": 0.4}
        higher_ai = {"full_name": "John Doe", "confidence": 0.8}
        mock_settings = type("S", (), {"do_gradient_api_key": ""})()

        with (
            patch("app.services.signature_parser.parse_signature_regex", return_value=low_regex),
            patch("app.services.signature_parser.parse_signature_ai", new_callable=AsyncMock, return_value=higher_ai),
            patch("app.config.settings", mock_settings),
        ):
            result = await extract_signature("body", "John", "john@acme.com")
        # Should go to claude_ai, not gradient
        assert result["extraction_method"] == "claude_ai"


class TestExtractSignature:
    @pytest.mark.asyncio
    async def test_high_confidence_regex_returns_regex(self):
        high_conf = {
            "full_name": "John Doe",
            "title": "CEO",
            "company_name": "Acme",
            "phone": "555-1234567",
            "email": "john@acme.com",
            "confidence": 0.8,
        }
        with patch("app.services.signature_parser.parse_signature_regex", return_value=high_conf):
            result = await extract_signature("body")
        assert result["extraction_method"] == "regex"
        assert result["confidence"] == 0.8

    @pytest.mark.asyncio
    async def test_low_regex_falls_back_to_ai(self):
        low_regex = {"full_name": "John", "confidence": 0.4}
        high_ai = {"full_name": "John Doe", "title": "CEO", "confidence": 0.85}
        mock_settings = type("S", (), {"do_gradient_api_key": ""})()
        with (
            patch("app.services.signature_parser.parse_signature_regex", return_value=low_regex),
            patch("app.services.signature_parser.parse_signature_ai", new_callable=AsyncMock, return_value=high_ai),
            patch("app.config.settings", mock_settings),
        ):
            result = await extract_signature("body")
        assert result["extraction_method"] == "claude_ai"
        assert result["confidence"] == 0.85

    @pytest.mark.asyncio
    async def test_ai_lower_than_regex_returns_regex(self):
        low_regex = {"full_name": "John", "confidence": 0.5}
        lower_ai = {"confidence": 0.3}
        with (
            patch("app.services.signature_parser.parse_signature_regex", return_value=low_regex),
            patch("app.services.signature_parser.parse_signature_ai", new_callable=AsyncMock, return_value=lower_ai),
        ):
            result = await extract_signature("body")
        assert result["extraction_method"] == "regex"

    @pytest.mark.asyncio
    async def test_ai_exception_falls_back_to_regex(self):
        low_regex = {"full_name": "John", "confidence": 0.4}
        mock_settings = type("S", (), {"do_gradient_api_key": ""})()
        with (
            patch("app.services.signature_parser.parse_signature_regex", return_value=low_regex),
            patch(
                "app.services.signature_parser.parse_signature_ai",
                new_callable=AsyncMock,
                side_effect=RuntimeError("fail"),
            ),
            patch("app.config.settings", mock_settings),
        ):
            result = await extract_signature("body")
        assert result["extraction_method"] == "regex"
        assert result["confidence"] == 0.4


class TestCacheSignatureExtract:
    def test_creates_new_record(self, db_session):
        from app.models import EmailSignatureExtract

        extract = {
            "full_name": "John Doe",
            "title": "CEO",
            "company_name": "Acme",
            "phone": "555-1234567",
            "mobile": "555-9876543",
            "website": "acme.com",
            "address": "123 Main St",
            "linkedin_url": "https://linkedin.com/in/jd",
            "extraction_method": "regex",
            "confidence": 0.8,
        }
        cache_signature_extract(db_session, "john@acme.com", extract)
        record = db_session.query(EmailSignatureExtract).filter_by(sender_email="john@acme.com").first()
        assert record is not None
        assert record.full_name == "John Doe"
        assert record.title == "CEO"
        assert record.confidence == 0.8

    def test_updates_existing_higher_confidence(self, db_session):
        from app.models import EmailSignatureExtract

        initial = EmailSignatureExtract(
            sender_email="john@acme.com",
            full_name="John",
            confidence=0.5,
            extraction_method="regex",
            seen_count=1,
        )
        db_session.add(initial)
        db_session.flush()
        extract = {
            "full_name": "John Doe",
            "title": "CEO",
            "company_name": "Acme Corp",
            "phone": "555-1234567",
            "mobile": None,
            "website": "acme.com",
            "address": None,
            "linkedin_url": None,
            "extraction_method": "claude_ai",
            "confidence": 0.9,
        }
        cache_signature_extract(db_session, "john@acme.com", extract)
        record = db_session.query(EmailSignatureExtract).filter_by(sender_email="john@acme.com").first()
        assert record.full_name == "John Doe"
        assert record.confidence == 0.9
        assert record.seen_count == 2

    def test_updates_existing_lower_confidence_only_increments_count(self, db_session):
        from app.models import EmailSignatureExtract

        initial = EmailSignatureExtract(
            sender_email="john@acme.com",
            full_name="John Doe",
            confidence=0.9,
            extraction_method="claude_ai",
            seen_count=3,
        )
        db_session.add(initial)
        db_session.flush()
        extract = {"full_name": "John", "confidence": 0.4, "extraction_method": "regex"}
        cache_signature_extract(db_session, "john@acme.com", extract)
        record = db_session.query(EmailSignatureExtract).filter_by(sender_email="john@acme.com").first()
        assert record.full_name == "John Doe"
        assert record.confidence == 0.9
        assert record.seen_count == 4

    def test_flush_error_triggers_rollback(self, db_session):
        extract = {"full_name": "Jane", "confidence": 0.6, "extraction_method": "regex"}
        original_flush = db_session.flush
        original_rollback = db_session.rollback
        flush_called = False
        rollback_called = False

        def bad_flush(*args, **kwargs):
            nonlocal flush_called
            flush_called = True
            raise Exception("DB error")

        def track_rollback(*args, **kwargs):
            nonlocal rollback_called
            rollback_called = True
            return original_rollback(*args, **kwargs)

        db_session.flush = bad_flush
        db_session.rollback = track_rollback
        cache_signature_extract(db_session, "jane@acme.com", extract)
        assert flush_called
        assert rollback_called
        db_session.flush = original_flush
        db_session.rollback = original_rollback
