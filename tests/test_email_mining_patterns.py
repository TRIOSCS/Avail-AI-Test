"""test_email_mining_patterns.py — Tests for email mining regex patterns.

Covers: MPN_PATTERN, PHONE_PATTERN, OFFER_PATTERNS from email_mining.py.

Called by: pytest
Depends on: app.connectors.email_mining
"""

import re

import pytest

from app.connectors.email_mining import MPN_PATTERN, OFFER_PATTERNS, PHONE_PATTERN


class TestMPNPattern:
    """MPN_PATTERN should match electronic component part numbers."""

    @pytest.mark.parametrize(
        "mpn",
        [
            "LM358N",
            "STM32F103C8T6",
            "SN74HC595N",
            "AD7124-8BCPZ",
            "MAX232CPE",
        ],
    )
    def test_standard_mpns(self, mpn: str):
        assert MPN_PATTERN.search(mpn), f"MPN_PATTERN should match '{mpn}'"

    @pytest.mark.parametrize(
        "mpn",
        [
            "MC34063A-D",
            "74HC595-SOP16",
            "LM2596S-5.0",
        ],
    )
    def test_mpns_with_dashes(self, mpn: str):
        assert MPN_PATTERN.search(mpn), f"MPN_PATTERN should match '{mpn}'"

    @pytest.mark.parametrize(
        "word",
        [
            "Hello",
            "the",
            "From",
            "Dear",
            "Hi",
        ],
    )
    def test_no_false_positives_on_common_words(self, word: str):
        assert not MPN_PATTERN.search(word), f"MPN_PATTERN should NOT match '{word}'"


class TestPhonePattern:
    """PHONE_PATTERN should match various phone number formats."""

    @pytest.mark.parametrize(
        "text,expected_digits",
        [
            ("(555) 123-4567", "5551234567"),
            ("555-123-4567", "5551234567"),
            ("555.123.4567", "5551234567"),
        ],
    )
    def test_us_formats(self, text: str, expected_digits: str):
        match = PHONE_PATTERN.search(text)
        assert match, f"PHONE_PATTERN should match '{text}'"
        digits = re.sub(r"\D", "", match.group(1))
        assert digits.endswith(expected_digits)

    @pytest.mark.parametrize(
        "text",
        [
            "+1-555-123-4567",
            "+1 555 123 4567",
            "+1.555.123.4567",
        ],
    )
    def test_international_formats(self, text: str):
        match = PHONE_PATTERN.search(text)
        assert match, f"PHONE_PATTERN should match '{text}'"

    @pytest.mark.parametrize(
        "text",
        [
            "Phone: 555-123-4567",
            "Tel: 555.123.4567",
            "Cell: (555) 123-4567",
            "Direct: 555 123 4567",
        ],
    )
    def test_with_label(self, text: str):
        match = PHONE_PATTERN.search(text)
        assert match, f"PHONE_PATTERN should match '{text}'"


class TestOfferPatterns:
    """OFFER_PATTERNS should detect vendor offer emails."""

    @pytest.mark.parametrize(
        "text",
        [
            "We have 5000 pcs in stock",
            "Currently in stock and ready to ship",
        ],
    )
    def test_stock_notifications(self, text: str):
        assert any(re.search(p, text) for p in OFFER_PATTERNS), f"OFFER_PATTERNS should match stock text: '{text}'"

    @pytest.mark.parametrize(
        "text",
        [
            "Please find our quotation attached",
            "Here is the quote you requested",
        ],
    )
    def test_quote_emails(self, text: str):
        assert any(re.search(p, text) for p in OFFER_PATTERNS), f"OFFER_PATTERNS should match quote text: '{text}'"

    @pytest.mark.parametrize(
        "text",
        [
            "Lead time is 4-6 weeks",
            "Current lead time: 8 weeks ARO",
        ],
    )
    def test_lead_time_mentions(self, text: str):
        assert any(re.search(p, text) for p in OFFER_PATTERNS), f"OFFER_PATTERNS should match lead time text: '{text}'"

    @pytest.mark.parametrize(
        "text",
        [
            "Thank you for your email",
            "Meeting scheduled for Tuesday",
            "Please see the agenda below",
        ],
    )
    def test_non_offer_emails_do_not_match(self, text: str):
        assert not any(re.search(p, text) for p in OFFER_PATTERNS), (
            f"OFFER_PATTERNS should NOT match non-offer text: '{text}'"
        )
