"""tests/test_vendor_utils_ext.py — Extended tests for app/vendor_utils.py."""

import os

os.environ["TESTING"] = "1"

from unittest.mock import MagicMock

from app.vendor_utils import (
    GENERIC_EMAIL_DOMAINS,
    fuzzy_match_vendor,
    fuzzy_score_vendor,
    merge_emails_into_card,
    merge_phones_into_card,
    normalize_vendor_name,
)


class TestNormalizeVendorName:
    def test_none_returns_empty(self):
        assert normalize_vendor_name("") == ""

    def test_lowercases(self):
        assert normalize_vendor_name("Arrow Electronics") == "arrow electronics"

    def test_strips_inc(self):
        assert normalize_vendor_name("Mouser Electronics, Inc.") == "mouser electronics"

    def test_strips_llc(self):
        assert normalize_vendor_name("Texas Micro LLC") == "texas micro"

    def test_strips_ltd(self):
        assert normalize_vendor_name("Components Ltd.") == "components"

    def test_strips_corp(self):
        assert normalize_vendor_name("Digi-Key Corp.") == "digi-key"

    def test_strips_leading_the(self):
        # "company" is a suffix, so it gets stripped too
        result = normalize_vendor_name("The Phoenix Company")
        assert "the" not in result
        assert "phoenix" in result

    def test_strips_gmbh(self):
        assert normalize_vendor_name("Würth GmbH") == "würth"

    def test_strips_co(self):
        assert normalize_vendor_name("Acme Co.") == "acme"

    def test_collapses_whitespace(self):
        assert normalize_vendor_name("Arrow   Electronics") == "arrow electronics"

    def test_strips_trailing_comma(self):
        assert normalize_vendor_name("Arrow Electronics,") == "arrow electronics"

    def test_plain_name_unchanged(self):
        assert normalize_vendor_name("arrow electronics") == "arrow electronics"

    def test_strips_sa(self):
        result = normalize_vendor_name("Acme S.A.")
        assert "s.a" not in result.lower()


class TestMergeEmailsIntoCard:
    def _card(self, emails=None):
        card = MagicMock()
        card.emails = emails or []
        return card

    def test_new_email_added(self):
        card = self._card(["existing@example.com"])
        count = merge_emails_into_card(card, ["new@example.com"])
        assert count == 1
        assert "new@example.com" in card.emails

    def test_duplicate_not_added(self):
        card = self._card(["same@example.com"])
        count = merge_emails_into_card(card, ["same@example.com"])
        assert count == 0
        assert len(card.emails) == 1

    def test_case_insensitive_dedup(self):
        card = self._card(["Test@Example.com"])
        count = merge_emails_into_card(card, ["test@example.com"])
        assert count == 0

    def test_no_at_sign_rejected(self):
        card = self._card([])
        count = merge_emails_into_card(card, ["notanemail"])
        assert count == 0

    def test_empty_list_no_change(self):
        card = self._card(["existing@example.com"])
        count = merge_emails_into_card(card, [])
        assert count == 0

    def test_none_list_no_change(self):
        card = self._card(["existing@example.com"])
        count = merge_emails_into_card(card, None)
        assert count == 0

    def test_multiple_new_emails(self):
        card = self._card([])
        count = merge_emails_into_card(card, ["a@example.com", "b@example.com"])
        assert count == 2

    def test_empty_card_emails(self):
        card = MagicMock()
        card.emails = None
        count = merge_emails_into_card(card, ["new@example.com"])
        assert count == 1


class TestMergePhonesIntoCard:
    def _card(self, phones=None):
        card = MagicMock()
        card.phones = phones or []
        return card

    def test_new_phone_added(self):
        card = self._card([])
        count = merge_phones_into_card(card, ["+14155551234"])
        assert count == 1
        assert "+14155551234" in card.phones

    def test_duplicate_digits_not_added(self):
        card = self._card(["415-555-1234"])
        count = merge_phones_into_card(card, ["(415) 555-1234"])
        assert count == 0

    def test_short_number_rejected(self):
        card = self._card([])
        count = merge_phones_into_card(card, ["12345"])
        assert count == 0

    def test_empty_list_no_change(self):
        card = self._card(["1234567890"])
        count = merge_phones_into_card(card, [])
        assert count == 0

    def test_none_list_no_change(self):
        card = self._card(["1234567890"])
        count = merge_phones_into_card(card, None)
        assert count == 0

    def test_empty_card_phones(self):
        card = MagicMock()
        card.phones = None
        count = merge_phones_into_card(card, ["+14155551234"])
        assert count == 1


class TestFuzzyScoreVendor:
    def test_identical_names_score_100(self):
        score = fuzzy_score_vendor("Arrow Electronics", "Arrow Electronics")
        assert score == 100

    def test_different_names_low_score(self):
        score = fuzzy_score_vendor("Arrow Electronics", "NE555P Supplier")
        assert score < 60

    def test_empty_name_returns_0(self):
        assert fuzzy_score_vendor("", "Arrow Electronics") == 0

    def test_suffix_stripped_for_match(self):
        score = fuzzy_score_vendor("Arrow Electronics Inc.", "Arrow Electronics LLC")
        assert score > 80

    def test_case_insensitive(self):
        score = fuzzy_score_vendor("ARROW ELECTRONICS", "arrow electronics")
        assert score == 100


class TestFuzzyMatchVendor:
    def test_exact_match_found(self):
        results = fuzzy_match_vendor("Arrow Electronics", ["Arrow Electronics", "Mouser"])
        assert len(results) >= 1
        assert results[0]["score"] == 100

    def test_no_match_below_threshold(self):
        results = fuzzy_match_vendor("Arrow Electronics", ["Totally Different", "Other Co"])
        assert all(r["score"] < 80 for r in results)

    def test_sorted_by_score_desc(self):
        candidates = ["Arrow Electronics Ltd.", "Arrows", "Mouser Electronics"]
        results = fuzzy_match_vendor("Arrow Electronics", candidates)
        if len(results) > 1:
            assert results[0]["score"] >= results[-1]["score"]

    def test_empty_query_returns_empty(self):
        results = fuzzy_match_vendor("", ["Arrow Electronics"])
        assert results == []

    def test_threshold_respected(self):
        results = fuzzy_match_vendor("Arrow", ["Arrow Electronics", "Mouser"], threshold=90)
        # Result has score >= 90 or is empty
        assert all(r["score"] >= 90 for r in results)


class TestGenericEmailDomains:
    def test_gmail_is_generic(self):
        assert "gmail.com" in GENERIC_EMAIL_DOMAINS

    def test_yahoo_is_generic(self):
        assert "yahoo.com" in GENERIC_EMAIL_DOMAINS

    def test_company_domain_not_generic(self):
        assert "arrow.com" not in GENERIC_EMAIL_DOMAINS
