"""Tests for app.vendor_utils — vendor name normalization, merging, and fuzzy matching."""

import pytest
from unittest.mock import MagicMock

from app.vendor_utils import (
    normalize_vendor_name,
    merge_emails_into_card,
    merge_phones_into_card,
    fuzzy_match_vendor,
    find_vendor_dedup_candidates,
)


# ── normalize_vendor_name ────────────────────────────────────────────


class TestNormalizeVendorName:
    def test_empty_and_none(self):
        assert normalize_vendor_name("") == ""
        assert normalize_vendor_name("   ") == ""

    def test_basic_lowercase(self):
        assert normalize_vendor_name("Arrow Electronics") == "arrow electronics"

    def test_strip_inc(self):
        assert normalize_vendor_name("Mouser Electronics, Inc.") == "mouser electronics"

    def test_strip_llc(self):
        assert normalize_vendor_name("Acme Parts LLC") == "acme parts"

    def test_strip_ltd(self):
        assert normalize_vendor_name("RS Components Ltd.") == "rs components"

    def test_strip_corp(self):
        assert normalize_vendor_name("Digi-Key Corp.") == "digi-key"

    def test_strip_gmbh(self):
        assert normalize_vendor_name("Siemens GmbH") == "siemens"

    def test_strip_company(self):
        assert normalize_vendor_name("The Phoenix Company LLC") == "phoenix"

    def test_strip_leading_the(self):
        assert normalize_vendor_name("The Arrow Group") == "arrow group"

    def test_strip_plc(self):
        assert normalize_vendor_name("Texas Instruments PLC") == "texas instruments"

    def test_strip_sa(self):
        assert normalize_vendor_name("STMicroelectronics S.A.") == "stmicroelectronics"

    def test_strip_bv(self):
        assert normalize_vendor_name("NXP B.V.") == "nxp"

    def test_strip_ag(self):
        assert normalize_vendor_name("Infineon AG") == "infineon"

    def test_strip_corporation(self):
        assert normalize_vendor_name("Intel Corporation") == "intel"

    def test_strip_incorporated(self):
        assert normalize_vendor_name("Analog Devices Incorporated") == "analog devices"

    def test_strip_limited(self):
        assert normalize_vendor_name("Murata Limited") == "murata"

    def test_multiple_suffixes(self):
        # "Co. Ltd." → strips "Ltd." then trailing punct leaves "acme co"
        # "co" alone is not a suffix (only "co." is), so it stays
        assert normalize_vendor_name("Acme Co. Ltd.") == "acme co"

    def test_trailing_comma(self):
        assert normalize_vendor_name("Acme Electronics,") == "acme electronics"

    def test_collapse_whitespace(self):
        assert normalize_vendor_name("  Acme   Electronics  Inc.  ") == "acme electronics"

    def test_no_false_strip_partial(self):
        # Should NOT strip "co" from "Costco" — the suffix regex requires word boundary
        result = normalize_vendor_name("Costco")
        assert result == "costco"

    def test_preserves_hyphens_in_name(self):
        assert normalize_vendor_name("Digi-Key") == "digi-key"

    def test_strip_sp_z_oo(self):
        assert normalize_vendor_name("Farnell sp. z o.o.") == "farnell"

    def test_strip_pty(self):
        assert normalize_vendor_name("Element14 Pty") == "element14"

    def test_strip_aps(self):
        assert normalize_vendor_name("Nordic Semiconductor APS") == "nordic semiconductor"


# ── merge_emails_into_card ───────────────────────────────────────────


class TestMergeEmails:
    def _make_card(self, existing=None):
        card = MagicMock()
        card.emails = list(existing) if existing else []
        return card

    def test_empty_new_emails(self):
        card = self._make_card(["a@b.com"])
        assert merge_emails_into_card(card, []) == 0
        assert card.emails == ["a@b.com"]

    def test_add_new_email(self):
        card = self._make_card()
        assert merge_emails_into_card(card, ["sales@arrow.com"]) == 1
        assert card.emails == ["sales@arrow.com"]

    def test_dedup_case_insensitive(self):
        card = self._make_card(["Sales@Arrow.com"])
        assert merge_emails_into_card(card, ["sales@arrow.com"]) == 0

    def test_skip_invalid(self):
        card = self._make_card()
        assert merge_emails_into_card(card, ["notanemail", "", None, "  "]) == 0

    def test_multiple_mixed(self):
        card = self._make_card(["existing@test.com"])
        added = merge_emails_into_card(card, [
            "existing@test.com",  # dup
            "new1@test.com",
            "new2@test.com",
            "bad-email",  # no @
        ])
        assert added == 2
        assert len(card.emails) == 3

    def test_none_existing_emails(self):
        card = MagicMock()
        card.emails = None
        assert merge_emails_into_card(card, ["a@b.com"]) == 1
        assert card.emails == ["a@b.com"]


# ── merge_phones_into_card ───────────────────────────────────────────


class TestMergePhones:
    def _make_card(self, existing=None):
        card = MagicMock()
        card.phones = list(existing) if existing else []
        return card

    def test_empty_new_phones(self):
        card = self._make_card(["+1-555-0100"])
        assert merge_phones_into_card(card, []) == 0

    def test_add_new_phone(self):
        card = self._make_card()
        assert merge_phones_into_card(card, ["+1-555-0100"]) == 1
        assert card.phones == ["+1-555-0100"]

    def test_dedup_by_digits(self):
        card = self._make_card(["+1-555-0100"])
        # Same digits, different formatting
        assert merge_phones_into_card(card, ["15550100"]) == 0

    def test_skip_too_short(self):
        card = self._make_card()
        assert merge_phones_into_card(card, ["123", "45", ""]) == 0

    def test_skip_empty_and_none(self):
        card = self._make_card()
        assert merge_phones_into_card(card, ["", None, "  "]) == 0

    def test_multiple_mixed(self):
        card = self._make_card(["+1-555-0100"])
        added = merge_phones_into_card(card, [
            "+1-555-0100",     # dup
            "+1-555-0200",     # new
            "123",             # too short
            "+44-20-7946-0958",  # new
        ])
        assert added == 2
        assert len(card.phones) == 3

    def test_none_existing_phones(self):
        card = MagicMock()
        card.phones = None
        assert merge_phones_into_card(card, ["+1-555-0100"]) == 1
        assert card.phones == ["+1-555-0100"]


# ── fuzzy_match_vendor ───────────────────────────────────────────────


class TestFuzzyMatchVendor:
    def test_exact_match(self):
        results = fuzzy_match_vendor("Arrow Electronics", ["Arrow Electronics Inc."])
        assert len(results) == 1
        assert results[0]["score"] == 100
        assert results[0]["name"] == "Arrow Electronics Inc."

    def test_close_match(self):
        results = fuzzy_match_vendor(
            "Mouser Electronics",
            ["Mouser Electronics, Inc.", "Arrow Electronics"],
        )
        # Normalized forms are identical, so score should be 100
        assert any(r["name"] == "Mouser Electronics, Inc." for r in results)

    def test_no_match_below_threshold(self):
        results = fuzzy_match_vendor("Mouser", ["Completely Unrelated Name"], threshold=80)
        assert len(results) == 0

    def test_sorted_by_score_descending(self):
        results = fuzzy_match_vendor(
            "Arrow",
            ["Arrow Electronics", "Arrow Components Ltd.", "Sparrow Industries"],
            threshold=50,
        )
        if len(results) > 1:
            assert results[0]["score"] >= results[1]["score"]

    def test_empty_query(self):
        assert fuzzy_match_vendor("", ["Arrow"]) == []

    def test_empty_candidates(self):
        assert fuzzy_match_vendor("Arrow", []) == []

    def test_custom_threshold(self):
        results_high = fuzzy_match_vendor("Arrow", ["Arrow Electronics"], threshold=95)
        results_low = fuzzy_match_vendor("Arrow", ["Arrow Electronics"], threshold=50)
        assert len(results_low) >= len(results_high)

    def test_skips_empty_candidates(self):
        results = fuzzy_match_vendor("Arrow", ["Arrow Electronics", "", "   "])
        assert len(results) <= 1  # empty/whitespace candidates should be skipped


# ── find_vendor_dedup_candidates ─────────────────────────────────────


class TestFindVendorDedupCandidates:
    def test_finds_similar_vendors(self, db_session):
        from app.models import VendorCard
        from datetime import datetime, timezone

        cards = [
            VendorCard(
                normalized_name="arrow electronics",
                display_name="Arrow Electronics",
                sighting_count=100,
                created_at=datetime.now(timezone.utc),
            ),
            VendorCard(
                normalized_name="arrow electronic",
                display_name="Arrow Electronic",
                sighting_count=5,
                created_at=datetime.now(timezone.utc),
            ),
            VendorCard(
                normalized_name="mouser electronics",
                display_name="Mouser Electronics",
                sighting_count=50,
                created_at=datetime.now(timezone.utc),
            ),
        ]
        db_session.add_all(cards)
        db_session.commit()

        results = find_vendor_dedup_candidates(db_session, threshold=85)
        # Arrow Electronics vs Arrow Electronic should be flagged
        assert len(results) >= 1
        pair = results[0]
        assert pair["score"] >= 85
        assert "vendor_a" in pair
        assert "vendor_b" in pair
        assert "id" in pair["vendor_a"]
        assert "name" in pair["vendor_a"]
        assert "sightings" in pair["vendor_a"]

    def test_no_duplicates_when_all_distinct(self, db_session):
        from app.models import VendorCard
        from datetime import datetime, timezone

        cards = [
            VendorCard(
                normalized_name="arrow electronics",
                display_name="Arrow Electronics",
                sighting_count=10,
                created_at=datetime.now(timezone.utc),
            ),
            VendorCard(
                normalized_name="texas instruments",
                display_name="Texas Instruments",
                sighting_count=10,
                created_at=datetime.now(timezone.utc),
            ),
        ]
        db_session.add_all(cards)
        db_session.commit()

        results = find_vendor_dedup_candidates(db_session, threshold=85)
        assert results == []

    def test_respects_limit(self, db_session):
        from app.models import VendorCard
        from datetime import datetime, timezone

        # Create many similar vendors to exceed limit
        for i in range(10):
            db_session.add(VendorCard(
                normalized_name=f"test vendor {i}",
                display_name=f"Test Vendor {i}",
                sighting_count=10,
                created_at=datetime.now(timezone.utc),
            ))
        db_session.commit()

        results = find_vendor_dedup_candidates(db_session, threshold=50, limit=3)
        assert len(results) <= 3

    def test_empty_db(self, db_session):
        results = find_vendor_dedup_candidates(db_session)
        assert results == []

    def test_results_sorted_by_score(self, db_session):
        from app.models import VendorCard
        from datetime import datetime, timezone

        cards = [
            VendorCard(normalized_name="arrow electronics", display_name="Arrow Electronics",
                       sighting_count=10, created_at=datetime.now(timezone.utc)),
            VendorCard(normalized_name="arrow electronic", display_name="Arrow Electronic",
                       sighting_count=10, created_at=datetime.now(timezone.utc)),
            VendorCard(normalized_name="arrow electro", display_name="Arrow Electro",
                       sighting_count=10, created_at=datetime.now(timezone.utc)),
        ]
        db_session.add_all(cards)
        db_session.commit()

        results = find_vendor_dedup_candidates(db_session, threshold=70)
        if len(results) > 1:
            assert results[0]["score"] >= results[1]["score"]
