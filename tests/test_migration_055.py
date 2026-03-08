"""Tests for migration 055 data cleanup helper functions.

Tests _dedup_site_contacts, _normalize_phones, and _extract_phones_from_site_name
using mock database connections to isolate the logic from real SQL execution.

Called by: pytest
Depends on: alembic/versions/055_data_cleanup.py, app/utils/phone_utils.py
"""

import importlib.util
import os
from unittest.mock import MagicMock, patch

import pytest

# Load the migration module directly since alembic/versions has no __init__.py
_MIGRATION_PATH = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions", "055_data_cleanup.py")
_spec = importlib.util.spec_from_file_location("migration_055", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

PHONE_RE = _mod.PHONE_RE
_dedup_site_contacts = _mod._dedup_site_contacts
_normalize_phones = _mod._normalize_phones
_extract_phones_from_site_name = _mod._extract_phones_from_site_name


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Row:
    """Mock DB row supporting attribute access and iteration over values
    (needed by ``sum(1 for v in c if v is not None)`` scoring in dedup)."""

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            object.__setattr__(self, k, v)

    def __iter__(self):
        return iter(vars(self).values())


def _ns(**kwargs):
    """Shorthand for creating a _Row mock."""
    return _Row(**kwargs)


def _sql_text(call_obj):
    """Extract the SQL string from a conn.execute(text(...), params) call."""
    if call_obj.args and hasattr(call_obj.args[0], "text"):
        return call_obj.args[0].text
    return ""


def _calls_matching(call_list, keyword):
    """Return calls whose SQL text contains the given keyword."""
    return [c for c in call_list if keyword in _sql_text(c).upper()]


# ---------------------------------------------------------------------------
# _dedup_site_contacts
# ---------------------------------------------------------------------------


class TestDedupSiteContacts:
    """Tests for _dedup_site_contacts helper."""

    def test_merge_two_duplicates_fills_missing_fields(self):
        """Two contacts with same email — best keeps its fields, gets missing ones from other."""
        c1 = _ns(
            id=1,
            customer_site_id=10,
            email="alice@acme.com",
            full_name="Alice",
            title=None,
            phone="+11234567890",
            notes=None,
            linkedin_url=None,
        )
        c2 = _ns(
            id=2,
            customer_site_id=10,
            email="ALICE@acme.com",
            full_name=None,
            title="VP Sales",
            phone=None,
            notes="Important",
            linkedin_url=None,
        )

        dupe_row = _ns(customer_site_id=10, em="alice@acme.com", ids=[1, 2])

        conn = MagicMock()
        conn.execute.return_value.fetchall.side_effect = [
            [dupe_row],
            [c1, c2],
        ]

        _dedup_site_contacts(conn)

        calls = conn.execute.call_args_list
        update_calls = _calls_matching(calls, "UPDATE")
        assert len(update_calls) > 0, "Should UPDATE to fill missing fields from loser"

        delete_calls = _calls_matching(calls, "DELETE")
        assert len(delete_calls) == 1, "Should DELETE the duplicate(s)"

    def test_no_duplicates_no_changes(self):
        """When there are no duplicate groups, no UPDATEs or DELETEs happen."""
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []

        _dedup_site_contacts(conn)

        assert conn.execute.call_count == 1

    def test_best_contact_is_one_with_most_non_null_fields(self):
        """The contact with more non-None fields wins and is kept."""
        c1 = _ns(
            id=1,
            customer_site_id=10,
            email="bob@test.com",
            full_name=None,
            title=None,
            phone=None,
            notes=None,
            linkedin_url=None,
        )
        c2 = _ns(
            id=2,
            customer_site_id=10,
            email="bob@test.com",
            full_name="Bob",
            title="CTO",
            phone="+11234567890",
            notes="Key contact",
            linkedin_url="linkedin.com/bob",
        )

        dupe_row = _ns(customer_site_id=10, em="bob@test.com", ids=[1, 2])

        conn = MagicMock()
        conn.execute.return_value.fetchall.side_effect = [
            [dupe_row],
            [c1, c2],
        ]

        _dedup_site_contacts(conn)

        delete_calls = _calls_matching(conn.execute.call_args_list, "DELETE")
        assert len(delete_calls) == 1
        # c1 (id=1) should be deleted; c2 (id=2) kept as best
        delete_ids = delete_calls[0].args[1]["ids"]
        assert 1 in delete_ids
        assert 2 not in delete_ids


# ---------------------------------------------------------------------------
# _normalize_phones
# ---------------------------------------------------------------------------


class TestNormalizePhones:
    """Tests for _normalize_phones helper."""

    def test_normalizes_various_formats(self):
        """Phone numbers in non-E.164 format get normalized via UPDATE."""
        row = _ns(id=1, phone="(123) 456-7890")

        conn = MagicMock()
        results = [[row]] + [[] for _ in range(5)]
        conn.execute.return_value.fetchall.side_effect = results

        with patch(
            "app.utils.phone_utils.format_phone_e164",
            side_effect=lambda raw: "+11234567890" if raw == "(123) 456-7890" else None,
        ):
            _normalize_phones(conn)

        update_calls = _calls_matching(conn.execute.call_args_list, "UPDATE")
        assert len(update_calls) == 1

    def test_skips_already_formatted(self):
        """Phones starting with '+' are filtered by SQL WHERE — no rows returned."""
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []

        with patch("app.utils.phone_utils.format_phone_e164"):
            _normalize_phones(conn)

        update_calls = _calls_matching(conn.execute.call_args_list, "UPDATE")
        assert len(update_calls) == 0

    def test_skips_when_normalized_equals_raw(self):
        """If format_phone_e164 returns the same value as input, no UPDATE issued."""
        row = _ns(id=1, phone="5551234567")
        conn = MagicMock()
        results = [[row]] + [[] for _ in range(5)]
        conn.execute.return_value.fetchall.side_effect = results

        with patch("app.utils.phone_utils.format_phone_e164", return_value="5551234567"):
            _normalize_phones(conn)

        update_calls = _calls_matching(conn.execute.call_args_list, "UPDATE")
        assert len(update_calls) == 0

    def test_skips_when_normalized_is_none(self):
        """If format_phone_e164 returns None (unparseable), no UPDATE issued."""
        row = _ns(id=1, phone="not-a-phone")
        conn = MagicMock()
        results = [[row]] + [[] for _ in range(5)]
        conn.execute.return_value.fetchall.side_effect = results

        with patch("app.utils.phone_utils.format_phone_e164", return_value=None):
            _normalize_phones(conn)

        update_calls = _calls_matching(conn.execute.call_args_list, "UPDATE")
        assert len(update_calls) == 0

    def test_processes_all_table_col_pairs(self):
        """All 6 table/column pairs are queried."""
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []

        with patch("app.utils.phone_utils.format_phone_e164"):
            _normalize_phones(conn)

        select_calls = _calls_matching(conn.execute.call_args_list, "SELECT")
        assert len(select_calls) == 6


# ---------------------------------------------------------------------------
# _extract_phones_from_site_name
# ---------------------------------------------------------------------------


class TestExtractPhonesFromSiteName:
    """Tests for _extract_phones_from_site_name helper."""

    def test_extracts_phone_into_contact_phone(self):
        """Phone in site_name extracted to contact_phone when it's empty."""
        row = _ns(id=1, site_name="Acme Corp (123) 456-7890", contact_phone=None, contact_phone_2=None)
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [row]

        with patch("app.utils.phone_utils.format_phone_e164", return_value="+11234567890"):
            _extract_phones_from_site_name(conn)

        update_calls = _calls_matching(conn.execute.call_args_list, "UPDATE")
        assert len(update_calls) == 1
        sql = _sql_text(update_calls[0])
        # Should target contact_phone (not contact_phone_2) since it's empty
        assert "contact_phone" in sql
        # Verify it's not contact_phone_2
        assert "contact_phone_2" not in sql

    def test_extracts_phone_into_contact_phone_2_when_phone_exists(self):
        """Phone goes to contact_phone_2 when contact_phone is already populated."""
        row = _ns(id=1, site_name="Acme Corp (123) 456-7890", contact_phone="+19876543210", contact_phone_2=None)
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [row]

        with patch("app.utils.phone_utils.format_phone_e164", return_value="+11234567890"):
            _extract_phones_from_site_name(conn)

        update_calls = _calls_matching(conn.execute.call_args_list, "UPDATE")
        assert len(update_calls) == 1
        sql = _sql_text(update_calls[0])
        assert "contact_phone_2" in sql

    def test_no_phone_in_site_name_no_changes(self):
        """Site name without phone number produces no UPDATE."""
        row = _ns(id=1, site_name="Acme Corporation", contact_phone=None, contact_phone_2=None)
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [row]

        with patch("app.utils.phone_utils.format_phone_e164"):
            _extract_phones_from_site_name(conn)

        assert conn.execute.call_count == 1

    def test_cleans_site_name_after_extraction(self):
        """Site name is cleaned up after phone extraction (extra spaces, dashes removed)."""
        row = _ns(id=1, site_name="Acme Corp - (123) 456-7890 - Dallas", contact_phone=None, contact_phone_2=None)
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [row]

        with patch("app.utils.phone_utils.format_phone_e164", return_value="+11234567890"):
            _extract_phones_from_site_name(conn)

        update_calls = _calls_matching(conn.execute.call_args_list, "UPDATE")
        assert len(update_calls) == 1
        clean_name = update_calls[0].args[1]["name"]
        assert "  " not in clean_name
        # Should not have trailing dashes/commas
        assert not clean_name.endswith("-")
        assert not clean_name.endswith(",")

    def test_skips_when_e164_returns_none(self):
        """If extracted digits don't form a valid phone, no UPDATE."""
        row = _ns(id=1, site_name="Order 123456789", contact_phone=None, contact_phone_2=None)
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [row]

        with patch("app.utils.phone_utils.format_phone_e164", return_value=None):
            _extract_phones_from_site_name(conn)

        update_calls = _calls_matching(conn.execute.call_args_list, "UPDATE")
        assert len(update_calls) == 0


# ---------------------------------------------------------------------------
# PHONE_RE regex
# ---------------------------------------------------------------------------


class TestPhoneRegex:
    """Tests for the PHONE_RE regex used by _extract_phones_from_site_name."""

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Acme (123) 456-7890", "(123) 456-7890"),
            ("Site +1-555-123-4567 HQ", "+1-555-123-4567"),
            ("Corp 123.456.7890 TX", "123.456.7890"),
            ("No phone here", None),
            ("Short 12345", None),
        ],
    )
    def test_phone_regex_matching(self, text, expected):
        match = PHONE_RE.search(text)
        if expected is None:
            assert match is None
        else:
            assert match is not None
            assert match.group(0).strip() == expected
