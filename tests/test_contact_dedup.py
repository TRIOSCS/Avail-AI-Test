"""tests/test_contact_dedup.py — unit tests for app/services/contact_dedup.py
(ISS-025 shared normalization for Find Contacts dedup against saved contacts).

Called by: pytest
Depends on: app.services.contact_dedup
"""

from types import SimpleNamespace

from app.services.contact_dedup import (
    existing_contact_keys,
    is_existing_contact,
    normalize_contact_email,
    normalize_contact_name,
)


class TestNormalizeContactEmail:
    def test_lowercases_and_strips(self):
        assert normalize_contact_email("  Bob@DigiKey.com ") == "bob@digikey.com"

    def test_none_returns_empty_string(self):
        assert normalize_contact_email(None) == ""

    def test_empty_returns_empty_string(self):
        assert normalize_contact_email("") == ""


class TestNormalizeContactName:
    def test_lowercases_and_collapses_whitespace(self):
        assert normalize_contact_name("  Dana   Wu ") == "dana wu"

    def test_none_returns_empty_string(self):
        assert normalize_contact_name(None) == ""

    def test_single_internal_space_unchanged(self):
        assert normalize_contact_name("Jane Doe") == "jane doe"


class TestExistingContactKeys:
    def test_builds_email_and_name_sets(self):
        rows = [
            SimpleNamespace(email="Bob@DigiKey.com", full_name="Bob Jones"),
            SimpleNamespace(email=None, full_name="  Dana   Wu "),
        ]
        emails, names = existing_contact_keys(rows)
        assert emails == {"bob@digikey.com"}
        # Names are collected for EVERY row, even ones that also have an email.
        assert names == {"bob jones", "dana wu"}

    def test_skips_rows_with_neither_email_nor_name(self):
        rows = [SimpleNamespace(email=None, full_name=None)]
        emails, names = existing_contact_keys(rows)
        assert emails == set()
        assert names == set()

    def test_empty_iterable(self):
        assert existing_contact_keys([]) == (set(), set())


class TestIsExistingContact:
    def test_email_match_case_insensitive(self):
        existing_emails = {"bob@digikey.com"}
        existing_names: set[str] = set()
        assert is_existing_contact("Bob@DigiKey.com", "Someone Else", existing_emails, existing_names) is True

    def test_email_no_match(self):
        existing_emails = {"bob@digikey.com"}
        assert is_existing_contact("alice@digikey.com", "Alice", existing_emails, set()) is False

    def test_no_email_falls_back_to_name_match(self):
        existing_names = {"dana wu"}
        assert is_existing_contact(None, "  Dana   Wu ", set(), existing_names) is True

    def test_no_email_no_name_match(self):
        existing_names = {"dana wu"}
        assert is_existing_contact("", "Someone New", set(), existing_names) is False

    def test_no_email_no_name_returns_false(self):
        assert is_existing_contact(None, None, set(), set()) is False

    def test_email_present_never_falls_back_to_name(self):
        # A candidate WITH an email that doesn't match must not be rescued by a name hit.
        existing_emails: set[str] = set()
        existing_names = {"bob jones"}
        assert is_existing_contact("new@example.com", "Bob Jones", existing_emails, existing_names) is False
