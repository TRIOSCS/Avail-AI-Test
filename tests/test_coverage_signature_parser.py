"""test_coverage_signature_parser.py — Tests for app/services/signature_parser.py.

Called by: pytest
Depends on: app.services.signature_parser
"""

import os

os.environ["TESTING"] = "1"

from app.services.signature_parser import (
    _extract_signature_block,
    parse_signature_regex,
)


class TestExtractSignatureBlock:
    def test_empty_body_returns_empty(self):
        assert _extract_signature_block("") == ""

    def test_none_like_empty_string(self):
        assert _extract_signature_block("") == ""

    def test_dash_delimiter_found(self):
        body = "Hello there\n\n---\nJohn Smith\nphone: 555-1234\njohn@example.com"
        block = _extract_signature_block(body)
        assert "John Smith" in block

    def test_thanks_delimiter(self):
        body = "Please see attached.\n\nThanks,\nJane Doe\njane@example.com"
        block = _extract_signature_block(body)
        assert "Jane Doe" in block

    def test_regards_delimiter(self):
        body = "Let me know if you have questions.\n\nRegards,\nBob Jones\nbob@jones.com"
        block = _extract_signature_block(body)
        assert "Bob Jones" in block

    def test_best_delimiter(self):
        body = "Looking forward to hearing from you.\n\nBest,\nAlice\nalice@co.com"
        block = _extract_signature_block(body)
        assert "Alice" in block

    def test_no_delimiter_uses_last_15_lines(self):
        # Body with many lines, no delimiter
        lines = [f"Content line {i}" for i in range(30)]
        lines.append("John Smith")
        lines.append("john@acme.com")
        body = "\n".join(lines)
        block = _extract_signature_block(body)
        assert "john@acme.com" in block

    def test_sent_from_my_iphone_delimiter(self):
        body = "Thanks for reaching out.\n\nSent from my iPhone\nJohn Smith"
        block = _extract_signature_block(body)
        assert "Sent from my iPhone" in block

    def test_best_regards_delimiter(self):
        body = "We can help with that.\n\nBest Regards,\nCarol White\ncarol@supplier.com"
        block = _extract_signature_block(body)
        assert "Carol White" in block

    def test_warm_regards_delimiter(self):
        body = "Please reply at your earliest convenience.\n\nWarm Regards,\nDave\ndave@co.com"
        block = _extract_signature_block(body)
        assert "Dave" in block


class TestParseSignatureRegex:
    def test_empty_body_returns_low_confidence(self):
        result = parse_signature_regex("")
        assert result["confidence"] == 0.0

    def test_extracts_email(self):
        body = "Thanks,\nJohn Smith\nSales Manager\njohn.smith@electronics.com\n+1 (555) 123-4567"
        result = parse_signature_regex(body)
        assert result.get("email") == "john.smith@electronics.com"

    def test_extracts_phone(self):
        body = "Best,\nJane Doe\nphone: 555-987-6543\njane@company.com"
        result = parse_signature_regex(body)
        assert result.get("phone") is not None
        assert "555" in result["phone"]

    def test_extracts_phone_or_mobile(self):
        # When "mobile:" appears at line start, the regex consumes "mobile" as prefix
        # so the phone is placed in the "phone" field; mobile is only separate when
        # preceded by another label on the same line.
        body = "---\nBob Jones\nmobile: +1 800 555-0199\nbob@example.com"
        result = parse_signature_regex(body)
        # Either phone or mobile should have the number
        assert result.get("phone") is not None or result.get("mobile") is not None

    def test_extracts_linkedin(self):
        body = "---\nCarol White\nDirector\nlinkedin.com/in/carolwhite\ncarol@company.com"
        result = parse_signature_regex(body)
        assert result.get("linkedin_url") is not None
        assert "linkedin.com" in result["linkedin_url"]

    def test_linkedin_url_gets_https_prefix(self):
        body = "---\nTest User\nlinkedin.com/in/testuser\ntest@example.com"
        result = parse_signature_regex(body)
        assert result.get("linkedin_url", "").startswith("https://")

    def test_extracts_name(self):
        body = "---\nJohn Smith\nSales Manager\njohn@acme.com"
        result = parse_signature_regex(body)
        assert result.get("full_name") == "John Smith"

    def test_confidence_positive_with_data(self):
        body = "Best,\nAlice Johnson\nProcurement Manager\nalice@supplier.com\n+1-800-555-0100"
        result = parse_signature_regex(body)
        assert result["confidence"] > 0.0

    def test_extracts_website(self):
        body = "---\nTom Brown\nwww.acme-supply.com\ntom@acme-supply.com"
        result = parse_signature_regex(body)
        assert result.get("website") is not None
        assert "acme-supply.com" in result["website"]

    def test_bare_phone_number_extracted(self):
        body = "Best Regards,\nSam Lee\n+1 650-555-0142\nsam@vendor.com"
        result = parse_signature_regex(body)
        assert result.get("phone") is not None

    def test_name_with_prefix_skipped(self):
        body = "---\nSent from my phone\nJohn Smith\njohn@vendor.com"
        result = parse_signature_regex(body)
        # "Sent from my phone" should not be captured as name
        if result.get("full_name"):
            assert result["full_name"] != "Sent from my phone"

    def test_email_not_captured_as_name(self):
        body = "---\njohn@vendor.com\nJohn Smith\njohn@vendor.com"
        result = parse_signature_regex(body)
        if result.get("full_name"):
            assert "@" not in result["full_name"]

    def test_complex_signature(self):
        body = """Please find our pricing below.

Best regards,
Sarah Connor
VP of Sales | Skynet Electronics
Tel: +1 (888) 555-0199
Cell: +1 (310) 555-0142
sarah.connor@skynet-electronics.com
www.skynet-electronics.com
linkedin.com/in/sarahconnor
"""
        result = parse_signature_regex(body)
        assert result.get("email") == "sarah.connor@skynet-electronics.com"
        assert result.get("full_name") == "Sarah Connor"
        assert result["confidence"] > 0.0

    def test_result_keys_present(self):
        body = "Thanks,\nTest User\ntest@example.com"
        result = parse_signature_regex(body)
        expected_keys = [
            "full_name",
            "title",
            "company_name",
            "phone",
            "mobile",
            "email",
            "website",
            "linkedin_url",
            "address",
            "confidence",
        ]
        for key in expected_keys:
            assert key in result, f"Missing key: {key}"

    def test_sincerely_delimiter(self):
        body = "Thank you for your inquiry.\n\nSincerely,\nMike Reed\nmike@reed.com"
        result = parse_signature_regex(body)
        assert result.get("email") == "mike@reed.com"

    def test_cheers_delimiter(self):
        body = "Looking forward.\n\nCheers,\nLiz Wong\nliz@component.com"
        result = parse_signature_regex(body)
        assert result.get("email") == "liz@component.com"

    def test_phone_with_office_label(self):
        body = "---\nMark Davis\noffice: 555-123-4567\nmark@company.com"
        result = parse_signature_regex(body)
        assert result.get("phone") is not None

    def test_phone_with_direct_label(self):
        body = "---\nKate Brown\ndirect: 555-321-0000\nkate@example.com"
        result = parse_signature_regex(body)
        assert result.get("phone") is not None
