"""Tests for app.vendor_utils — vendor name normalization, merging, and fuzzy
matching."""

from datetime import UTC
from unittest.mock import MagicMock

import pytest

from app.vendor_utils import (
    find_vendor_dedup_candidates,
    fuzzy_dedup_scan,
    fuzzy_match_vendor,
    merge_emails_into_card,
    merge_phones_into_card,
    normalize_vendor_name,
)

# ── normalize_vendor_name ────────────────────────────────────────────


class TestNormalizeVendorName:
    def test_empty_and_none(self):
        assert normalize_vendor_name("") == ""
        assert normalize_vendor_name("   ") == ""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Arrow Electronics", "arrow electronics"),
            ("Mouser Electronics, Inc.", "mouser electronics"),
            ("Acme Parts LLC", "acme parts"),
            ("RS Components Ltd.", "rs components"),
            ("Digi-Key Corp.", "digi-key"),
            ("Siemens GmbH", "siemens"),
            ("The Phoenix Company LLC", "phoenix"),
            ("The Arrow Group", "arrow group"),
            ("Texas Instruments PLC", "texas instruments"),
            ("STMicroelectronics S.A.", "stmicroelectronics"),
            ("NXP B.V.", "nxp"),
            ("Infineon AG", "infineon"),
            ("Intel Corporation", "intel"),
            ("Analog Devices Incorporated", "analog devices"),
            ("Murata Limited", "murata"),
            # "Co. Ltd." → strips "Ltd." then trailing punct leaves "acme co"
            # "co" alone is not a suffix (only "co." is), so it stays
            ("Acme Co. Ltd.", "acme co"),
            ("Acme Electronics,", "acme electronics"),
            ("  Acme   Electronics  Inc.  ", "acme electronics"),
            # Should NOT strip "co" from "Costco" — the suffix regex requires word boundary
            ("Costco", "costco"),
            ("Digi-Key", "digi-key"),
            ("Farnell sp. z o.o.", "farnell"),
            ("Element14 Pty", "element14"),
            ("Nordic Semiconductor APS", "nordic semiconductor"),
        ],
        ids=[
            "basic_lowercase",
            "strip_inc",
            "strip_llc",
            "strip_ltd",
            "strip_corp",
            "strip_gmbh",
            "strip_company",
            "strip_leading_the",
            "strip_plc",
            "strip_sa",
            "strip_bv",
            "strip_ag",
            "strip_corporation",
            "strip_incorporated",
            "strip_limited",
            "multiple_suffixes",
            "trailing_comma",
            "collapse_whitespace",
            "no_false_strip_partial",
            "preserves_hyphens_in_name",
            "strip_sp_z_oo",
            "strip_pty",
            "strip_aps",
        ],
    )
    def test_normalization(self, raw, expected):
        assert normalize_vendor_name(raw) == expected


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
        added = merge_emails_into_card(
            card,
            [
                "existing@test.com",  # dup
                "new1@test.com",
                "new2@test.com",
                "bad-email",  # no @
            ],
        )
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
        added = merge_phones_into_card(
            card,
            [
                "+1-555-0100",  # dup
                "+1-555-0200",  # new
                "123",  # too short
                "+44-20-7946-0958",  # new
            ],
        )
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


# ── fuzzy_dedup_scan ──────────────────────────────────────────────────


class TestFuzzyDedupScan:
    """P4.4: the shared rapidfuzz scan behind vendor_duplicates._fuzzy_match_python
    (anchor mode) and company_utils._find_company_dedup_candidates_rapidfuzz (pairwise
    mode)."""

    def test_empty_rows_pairwise_mode(self):
        assert fuzzy_dedup_scan([], lambda r: r) == []

    def test_empty_rows_anchor_mode(self):
        assert fuzzy_dedup_scan([], lambda r: r, anchor_key="anything") == []

    def test_pairwise_threshold_boundary_included_at_exact_score(self):
        from rapidfuzz import fuzz

        rows = ["arrow electronics", "arrow electronic"]
        score = fuzz.token_sort_ratio(rows[0], rows[1])

        results = fuzzy_dedup_scan(rows, lambda r: r, threshold=score)
        assert results == [(rows[0], rows[1], score)]

    def test_pairwise_threshold_boundary_excluded_just_above_score(self):
        from rapidfuzz import fuzz

        rows = ["arrow electronics", "arrow electronic"]
        score = fuzz.token_sort_ratio(rows[0], rows[1])

        assert fuzzy_dedup_scan(rows, lambda r: r, threshold=score + 0.01) == []

    def test_pairwise_zero_threshold_scans_every_pair(self):
        rows = ["a", "b", "c", "d"]  # single-char rows never score >= any real threshold
        results = fuzzy_dedup_scan(rows, lambda r: r, threshold=0)
        assert len(results) == 6  # C(4, 2) unordered pairs, all pass threshold=0

    def test_pairwise_cap_stops_scan_early(self):
        rows = [f"arrow electronics variant {i}" for i in range(10)]
        results = fuzzy_dedup_scan(rows, lambda r: r, threshold=50, limit=3)
        assert len(results) == 3

    def test_pairwise_no_limit_does_not_truncate(self):
        rows = [f"arrow electronics variant {i}" for i in range(10)]
        results = fuzzy_dedup_scan(rows, lambda r: r, threshold=50)
        assert len(results) > 3  # every matching pair collected, no cap applied

    def test_anchor_mode_returns_row_none_score_tuples(self):
        rows = ["Arrow Electronics", "Totally Unrelated Company Name"]
        results = fuzzy_dedup_scan(rows, lambda r: r, threshold=80, anchor_key="Arrow Electronics")
        assert len(results) == 1
        row, other, score = results[0]
        assert row == "Arrow Electronics"
        assert other is None
        assert score >= 80

    def test_anchor_mode_ignores_limit_scans_every_row(self):
        rows = [f"Arrow Electronics {i}" for i in range(10)]
        results = fuzzy_dedup_scan(rows, lambda r: r, threshold=50, limit=2, anchor_key="Arrow Electronics")
        assert len(results) == 10  # caller sorts/truncates afterward; scan itself never caps

    def test_anchor_mode_below_threshold_excluded(self):
        rows = ["Totally Unrelated Company Name"]
        assert fuzzy_dedup_scan(rows, lambda r: r, threshold=80, anchor_key="Arrow Electronics") == []


# ── find_vendor_dedup_candidates ─────────────────────────────────────


class TestFindVendorDedupCandidates:
    def test_finds_similar_vendors(self, db_session):
        from datetime import datetime

        from app.models import VendorCard

        cards = [
            VendorCard(
                normalized_name="arrow electronics",
                display_name="Arrow Electronics",
                sighting_count=100,
                created_at=datetime.now(UTC),
            ),
            VendorCard(
                normalized_name="arrow electronic",
                display_name="Arrow Electronic",
                sighting_count=5,
                created_at=datetime.now(UTC),
            ),
            VendorCard(
                normalized_name="mouser electronics",
                display_name="Mouser Electronics",
                sighting_count=50,
                created_at=datetime.now(UTC),
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
        from datetime import datetime

        from app.models import VendorCard

        cards = [
            VendorCard(
                normalized_name="arrow electronics",
                display_name="Arrow Electronics",
                sighting_count=10,
                created_at=datetime.now(UTC),
            ),
            VendorCard(
                normalized_name="texas instruments",
                display_name="Texas Instruments",
                sighting_count=10,
                created_at=datetime.now(UTC),
            ),
        ]
        db_session.add_all(cards)
        db_session.commit()

        results = find_vendor_dedup_candidates(db_session, threshold=85)
        assert results == []

    def test_respects_limit(self, db_session):
        from datetime import datetime

        from app.models import VendorCard

        # Create many similar vendors to exceed limit
        for i in range(10):
            db_session.add(
                VendorCard(
                    normalized_name=f"test vendor {i}",
                    display_name=f"Test Vendor {i}",
                    sighting_count=10,
                    created_at=datetime.now(UTC),
                )
            )
        db_session.commit()

        results = find_vendor_dedup_candidates(db_session, threshold=50, limit=3)
        assert len(results) <= 3

    def test_empty_db(self, db_session):
        results = find_vendor_dedup_candidates(db_session)
        assert results == []

    def test_results_sorted_by_score(self, db_session):
        from datetime import datetime

        from app.models import VendorCard

        cards = [
            VendorCard(
                normalized_name="arrow electronics",
                display_name="Arrow Electronics",
                sighting_count=10,
                created_at=datetime.now(UTC),
            ),
            VendorCard(
                normalized_name="arrow electronic",
                display_name="Arrow Electronic",
                sighting_count=10,
                created_at=datetime.now(UTC),
            ),
            VendorCard(
                normalized_name="arrow electro",
                display_name="Arrow Electro",
                sighting_count=10,
                created_at=datetime.now(UTC),
            ),
        ]
        db_session.add_all(cards)
        db_session.commit()

        results = find_vendor_dedup_candidates(db_session, threshold=70)
        if len(results) > 1:
            assert results[0]["score"] >= results[1]["score"]

    def test_seen_pairs_skipped(self, db_session):
        """Line 195: when a pair has already been seen, it is skipped."""
        # This tests the `continue` on line 195 — pairs are deduplicated
        # by checking (min_id, max_id) tuples
        from datetime import datetime

        from app.models import VendorCard

        # Create only 2 similar cards — second iteration would try same pair
        cards = [
            VendorCard(
                normalized_name="test company alpha",
                display_name="Test Company Alpha",
                sighting_count=10,
                created_at=datetime.now(UTC),
            ),
            VendorCard(
                normalized_name="test company alpha inc",
                display_name="Test Company Alpha Inc",
                sighting_count=10,
                created_at=datetime.now(UTC),
            ),
        ]
        db_session.add_all(cards)
        db_session.commit()

        results = find_vendor_dedup_candidates(db_session, threshold=70)
        # Should have at most 1 pair (no duplicates)
        pair_keys = set()
        for r in results:
            key = (
                min(r["vendor_a"]["id"], r["vendor_b"]["id"]),
                max(r["vendor_a"]["id"], r["vendor_b"]["id"]),
            )
            assert key not in pair_keys, "Duplicate pair found"
            pair_keys.add(key)

    def test_low_sighting_duplicate_beyond_old_500_cap_is_found(self, db_session):
        """Regression for the 500-row-by-sighting_count cap: two near-duplicate,
        near-zero-sighting vendors must still be flagged even when 500+ other,
        much higher-sighting vendors exist in the table. Under the old
        `.order_by(sighting_count.desc()).limit(500)` implementation these two
        would never even be loaded, let alone compared."""
        import random
        from datetime import datetime

        from app.models import VendorCard

        # Random hex suffixes (not sequential digits) keep pairwise fuzzy scores
        # among the noise pool well below the 85 threshold — sequential numeric
        # suffixes ("Bulk Supplier 5" vs "Bulk Supplier 50") are near-identical
        # strings and would themselves collide, defeating the test's purpose.
        rng = random.Random(5)
        for _ in range(520):
            suffix = f"{rng.getrandbits(64):016x}"
            db_session.add(
                VendorCard(
                    normalized_name=f"bulk supplier {suffix}",
                    display_name=f"Bulk Supplier {suffix}",
                    sighting_count=1000,
                    created_at=datetime.now(UTC),
                )
            )
        db_session.add(
            VendorCard(
                normalized_name="zyquin components",
                display_name="Zyquin Components",
                sighting_count=0,
                created_at=datetime.now(UTC),
            )
        )
        db_session.add(
            VendorCard(
                normalized_name="zyquin component",
                display_name="Zyquin Component",
                sighting_count=1,
                created_at=datetime.now(UTC),
            )
        )
        db_session.commit()

        results = find_vendor_dedup_candidates(db_session, threshold=85, limit=50)
        names = {(r["vendor_a"]["name"], r["vendor_b"]["name"]) for r in results}
        assert ("Zyquin Components", "Zyquin Component") in names or (
            "Zyquin Component",
            "Zyquin Components",
        ) in names

    def test_uses_pg_path_when_dialect_is_postgresql(self):
        """Dispatcher routes to the pg_trgm fast path when the bound dialect is
        postgresql, leaving the SQLite blocking fallback for everything else."""
        from unittest.mock import patch

        mock_db = MagicMock()
        mock_db.bind = MagicMock()
        mock_db.bind.dialect.name = "postgresql"

        with patch("app.vendor_utils._find_vendor_dedup_candidates_pg", return_value=[]) as mock_pg:
            find_vendor_dedup_candidates(mock_db)

        mock_pg.assert_called_once_with(mock_db, 85, 50)

    def test_pg_path_empty_pairs(self):
        """PG path returns [] when no similar pairs are found."""
        from app.vendor_utils import _find_vendor_dedup_candidates_pg

        mock_db = MagicMock()
        mock_q = mock_db.query.return_value
        mock_q.filter.return_value = mock_q
        mock_q.order_by.return_value = mock_q
        mock_q.limit.return_value = mock_q
        mock_q.all.return_value = []

        result = _find_vendor_dedup_candidates_pg(mock_db, 85, 50)

        assert result == []


class TestVendorBlockingKey:
    def test_blocking_key_groups_near_duplicates(self):
        from app.vendor_utils import _vendor_blocking_key

        assert _vendor_blocking_key("arrow electronics") == _vendor_blocking_key("arrow electronic")

    def test_blocking_key_empty_string(self):
        from app.vendor_utils import _vendor_blocking_key

        assert _vendor_blocking_key("") == ""

    def test_blocking_key_strips_spaces_before_truncating(self):
        from app.vendor_utils import _vendor_blocking_key

        assert _vendor_blocking_key("a b c d e") == "abcd"


# ── _enrich_with_vendor_cards ─────────────────────────────────────────


def _make_results(sightings: list[dict]) -> dict:
    """Build a minimal results dict as produced by search_service."""
    return {"ABC123": {"sightings": sightings, "blacklisted_count": 0}}


def _make_sighting(
    vendor_name: str = "Arrow Electronics",
    mpn: str = "ABC123",
    *,
    email: str | None = None,
    phone: str | None = None,
    url: str | None = None,
    is_historical: bool = False,
    is_material_history: bool = False,
) -> dict:
    return {
        "vendor_name": vendor_name,
        "mpn_matched": mpn,
        "vendor_email": email,
        "vendor_phone": phone,
        "vendor_url": url,
        "is_historical": is_historical,
        "is_material_history": is_material_history,
    }


class TestEnrichWithVendorCards:
    def _enrich(self, results, db):
        from app.vendor_utils import _enrich_with_vendor_cards

        return _enrich_with_vendor_cards(results, db)

    def _make_card(self, db, normalized_name: str, display_name: str, **kwargs):
        from app.models import VendorCard

        card = VendorCard(
            normalized_name=normalized_name,
            display_name=display_name,
            sighting_count=kwargs.get("sighting_count", 0),
            is_blacklisted=kwargs.get("is_blacklisted", False),
            vendor_score=kwargs.get("vendor_score"),
            is_new_vendor=kwargs.get("is_new_vendor", True),
            emails=kwargs.get("emails", []),
            phones=kwargs.get("phones", []),
        )
        db.add(card)
        db.flush()
        return card

    def test_no_vendor_names_returns_early(self, db_session):
        results = _make_results([{"vendor_name": None, "mpn_matched": "X", "is_historical": False}])
        self._enrich(results, db_session)
        # No crash, no cards created
        from app.models import VendorCard

        assert db_session.query(VendorCard).count() == 0

    def test_existing_card_enriches_sighting(self, db_session):
        self._make_card(db_session, "arrow electronics", "Arrow Electronics", vendor_score=72.5, is_new_vendor=False)
        results = _make_results([_make_sighting("Arrow Electronics")])
        self._enrich(results, db_session)
        sightings = results["ABC123"]["sightings"]
        assert len(sightings) == 1
        vc = sightings[0]["vendor_card"]
        assert vc["vendor_score"] == 72.5
        assert vc["is_new_vendor"] is False

    def test_unknown_vendor_auto_creates_card(self, db_session):
        from app.models import VendorCard

        results = _make_results([_make_sighting("BrandNew Vendor Inc")])
        self._enrich(results, db_session)
        # A new VendorCard should have been created, normalized, and linked back to
        # the sighting's rendered vendor_card summary.
        card = db_session.query(VendorCard).filter(VendorCard.display_name == "BrandNew Vendor Inc").first()
        assert card is not None
        assert card.normalized_name == "brandnew vendor"
        assert card.sighting_count == 1
        assert results["ABC123"]["sightings"][0]["vendor_card"]["card_id"] == card.id

    def test_blacklisted_vendor_filtered_out(self, db_session):
        self._make_card(db_session, "bad vendor", "Bad Vendor", is_blacklisted=True)
        results = _make_results([_make_sighting("Bad Vendor")])
        self._enrich(results, db_session)
        assert results["ABC123"]["sightings"] == []
        assert results["ABC123"]["blacklisted_count"] == 1

    def test_garbage_vendor_name_filtered(self, db_session):
        # These are non-empty but garbage → filtered in the enrichment loop
        for name in ["unknown", "no seller listed", "n/a"]:
            results = _make_results([_make_sighting(name)])
            self._enrich(results, db_session)
            assert results["ABC123"]["sightings"] == [], f"Expected {name!r} to be filtered"

    def test_empty_vendor_name_early_return(self, db_session):
        # Empty string → falsy → early return, sightings left unchanged
        s = _make_sighting("")
        results = _make_results([s])
        self._enrich(results, db_session)
        # Early return: sightings dict unchanged, no crash
        assert len(results["ABC123"]["sightings"]) == 1

    def test_email_harvested_into_card(self, db_session):
        self._make_card(db_session, "arrow electronics", "Arrow Electronics")
        results = _make_results([_make_sighting("Arrow Electronics", email="sales@arrow.com")])
        self._enrich(results, db_session)
        from app.models import VendorCard

        card = db_session.query(VendorCard).filter(VendorCard.normalized_name == "arrow electronics").first()
        assert "sales@arrow.com" in (card.emails or [])

    def test_phone_harvested_into_card(self, db_session):
        self._make_card(db_session, "arrow electronics", "Arrow Electronics")
        results = _make_results([_make_sighting("Arrow Electronics", phone="+1-555-0100")])
        self._enrich(results, db_session)
        from app.models import VendorCard

        card = db_session.query(VendorCard).filter(VendorCard.normalized_name == "arrow electronics").first()
        assert "+1-555-0100" in (card.phones or [])

    def test_website_harvested_into_card(self, db_session):
        self._make_card(db_session, "arrow electronics", "Arrow Electronics")
        results = _make_results([_make_sighting("Arrow Electronics", url="https://arrow.com")])
        self._enrich(results, db_session)
        from app.models import VendorCard

        card = db_session.query(VendorCard).filter(VendorCard.normalized_name == "arrow electronics").first()
        assert card.website == "https://arrow.com"

    def test_historical_sightings_not_harvested(self, db_session):
        self._make_card(db_session, "arrow electronics", "Arrow Electronics")
        s = _make_sighting("Arrow Electronics", email="old@arrow.com", is_historical=True)
        results = _make_results([s])
        self._enrich(results, db_session)
        from app.models import VendorCard

        card = db_session.query(VendorCard).filter(VendorCard.normalized_name == "arrow electronics").first()
        assert "old@arrow.com" not in (card.emails or [])

    def test_material_history_sightings_not_harvested(self, db_session):
        self._make_card(db_session, "arrow electronics", "Arrow Electronics")
        s = _make_sighting("Arrow Electronics", email="stale@arrow.com", is_material_history=True)
        results = _make_results([s])
        self._enrich(results, db_session)
        from app.models import VendorCard

        card = db_session.query(VendorCard).filter(VendorCard.normalized_name == "arrow electronics").first()
        assert "stale@arrow.com" not in (card.emails or [])

    def test_multiple_sightings_multiple_vendors(self, db_session):
        self._make_card(db_session, "arrow electronics", "Arrow Electronics")
        self._make_card(db_session, "mouser electronics", "Mouser Electronics", is_blacklisted=True)
        results = _make_results(
            [
                _make_sighting("Arrow Electronics"),
                _make_sighting("Mouser Electronics"),
                _make_sighting("unknown"),
            ]
        )
        self._enrich(results, db_session)
        sightings = results["ABC123"]["sightings"]
        assert len(sightings) == 1  # Only Arrow survives
        assert sightings[0]["vendor_name"] == "Arrow Electronics"
        assert results["ABC123"]["blacklisted_count"] == 1

    def test_empty_results_no_crash(self, db_session):
        self._enrich({}, db_session)  # No error

    def test_sighting_card_id_set(self, db_session):
        card = self._make_card(db_session, "arrow electronics", "Arrow Electronics")
        results = _make_results([_make_sighting("Arrow Electronics")])
        self._enrich(results, db_session)
        sightings = results["ABC123"]["sightings"]
        assert sightings[0]["vendor_card"]["card_id"] == card.id

    def test_review_avg_rating_computed(self, db_session, test_user):
        from app.models import VendorReview

        card = self._make_card(db_session, "arrow electronics", "Arrow Electronics")
        review = VendorReview(vendor_card_id=card.id, user_id=test_user.id, rating=4)
        db_session.add(review)
        db_session.flush()

        results = _make_results([_make_sighting("Arrow Electronics")])
        self._enrich(results, db_session)
        sightings = results["ABC123"]["sightings"]
        assert sightings[0]["vendor_card"]["avg_rating"] == 4.0
        assert sightings[0]["vendor_card"]["review_count"] == 1
