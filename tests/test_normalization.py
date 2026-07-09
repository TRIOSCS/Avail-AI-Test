"""Comprehensive tests for app/utils/normalization.py functions."""

import pytest

from app.utils.normalization import (
    detect_currency,
    fuzzy_mpn_match,
    normalize_condition,
    normalize_date_code,
    normalize_lead_time,
    normalize_moq,
    normalize_mpn,
    normalize_mpn_key,
    normalize_packaging,
    normalize_price,
    normalize_quantity,
    parse_substitute_mpns,
    parse_website_domain,
)

# ── normalize_quantity ────────────────────────────────────────────────


class TestNormalizeQuantity:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param(500, 500, id="int_positive"),
            pytest.param(0, None, id="int_zero"),
            pytest.param(-10, None, id="int_negative"),
            pytest.param(100.7, 100, id="float_positive"),
            pytest.param(0.0, None, id="float_zero"),
            pytest.param("5000", 5000, id="string_plain"),
            pytest.param("50,000", 50000, id="string_with_commas"),
            pytest.param("50K", 50000, id="string_k_suffix"),
            pytest.param("50k", 50000, id="string_k_lower"),
            pytest.param("2M", 2000000, id="string_m_suffix"),
            pytest.param("1.5K", 1500, id="string_fractional_k"),
            pytest.param("50000+", 50000, id="string_plus_suffix"),
            pytest.param("  1000  ", 1000, id="string_with_spaces"),
            pytest.param(None, None, id="none"),
            pytest.param("", None, id="empty_string"),
            pytest.param("abc", None, id="non_numeric_string"),
        ],
    )
    def test_normalize_quantity(self, value, expected):
        assert normalize_quantity(value) == expected


# ── normalize_price ──────────────────────────────────────────────────


class TestNormalizePrice:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param(1.50, 1.50, id="float_positive"),
            pytest.param(10, 10.0, id="int_positive"),
            pytest.param(0, None, id="zero"),
            pytest.param(-5.0, None, id="negative"),
            pytest.param("$1,234.56", 1234.56, id="dollar_sign"),
            pytest.param("€12.50", 12.50, id="euro_sign"),
            pytest.param("£99.99", 99.99, id="pound_sign"),
            pytest.param("¥500", 500.0, id="yen_sign"),
            pytest.param("USD 25.00", 25.0, id="currency_code_usd"),
            pytest.param("EUR 10.50", 10.50, id="currency_code_eur"),
            pytest.param("10,000.50", 10000.50, id="commas_stripped"),
            pytest.param("0.38-0.42", 0.38, id="range_takes_lower"),
            pytest.param("$2.50/ea", 2.50, id="per_ea_suffix"),
            pytest.param("1.00 each", 1.00, id="per_unit_suffix"),
            pytest.param("1.5k", 1500.0, id="k_suffix"),
            pytest.param("2M", 2000000.0, id="m_suffix"),
            pytest.param(None, None, id="none"),
            pytest.param("", None, id="empty_string"),
            pytest.param("call for pricing", None, id="non_numeric"),
            # HK$ is not reliably parsed — $ matches first in symbol iteration
            pytest.param("HK$50.00", None, id="hk_dollar"),
        ],
    )
    def test_normalize_price(self, value, expected):
        assert normalize_price(value) == expected


# ── normalize_lead_time ──────────────────────────────────────────────


class TestNormalizeLeadTime:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("stock", 0, id="stock"),
            pytest.param("In Stock", 0, id="in_stock"),
            pytest.param("immediate", 0, id="immediate"),
            pytest.param("30 days", 30, id="days_single"),
            pytest.param("4 weeks", 28, id="weeks_single"),
            pytest.param("4-6 weeks", 35, id="weeks_range"),
            pytest.param("2-3 wks", 17, id="weeks_abbreviated"),
            pytest.param("2 months", 60, id="months"),
            pytest.param("10 days ARO", 10, id="aro"),
            # Small numbers assumed to be weeks
            pytest.param("6", 42, id="ambiguous_small_number"),
            pytest.param(None, None, id="none"),
            pytest.param("", None, id="empty"),
            pytest.param("contact us", None, id="no_numbers"),
        ],
    )
    def test_normalize_lead_time(self, value, expected):
        assert normalize_lead_time(value) == expected


# ── normalize_date_code ──────────────────────────────────────────────


class TestNormalizeDateCode:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("2024+", "2024+", id="year_plus"),
            pytest.param("DC 23/45", "23/45", id="dc_prefix"),
            pytest.param("date code: 2023", "2023", id="date_code_prefix"),
            pytest.param("2023", "2023", id="plain_year"),
            pytest.param("N/A", None, id="na"),
            pytest.param("TBD", None, id="tbd"),
            pytest.param("unknown", None, id="unknown"),
            pytest.param("-", None, id="dash"),
            pytest.param(None, None, id="none"),
            pytest.param("", None, id="empty"),
            pytest.param("2345", "2345", id="year_week"),
        ],
    )
    def test_normalize_date_code(self, value, expected):
        assert normalize_date_code(value) == expected


# ── normalize_moq ───────────────────────────────────────────────────


class TestNormalizeMoq:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param(500, 500, id="plain_int"),
            pytest.param("MOQ: 500", 500, id="moq_prefix"),
            pytest.param("Minimum 100", 100, id="min_prefix"),
            pytest.param("10K", 10000, id="k_suffix"),
            pytest.param(None, None, id="none"),
            pytest.param("", None, id="empty"),
        ],
    )
    def test_normalize_moq(self, value, expected):
        assert normalize_moq(value) == expected


# ── detect_currency ──────────────────────────────────────────────────


class TestDetectCurrency:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("$50.00", "USD", id="dollar"),
            pytest.param("€10.00", "EUR", id="euro"),
            pytest.param("£25.00", "GBP", id="pound"),
            pytest.param("¥1000", "JPY", id="yen"),
            pytest.param("EUR", "EUR", id="code_string"),
            # HK$ detection works when checking raw string symbols
            # but $ matches first since dict iteration order has $ before HK$
            pytest.param("HK$100", "USD", id="hk_dollar"),
            pytest.param(None, "USD", id="none"),
            pytest.param("", "USD", id="empty"),
            pytest.param("42.00", "USD", id="unknown_defaults_usd"),
        ],
    )
    def test_detect_currency(self, value, expected):
        assert detect_currency(value) == expected


# ── normalize_mpn_key ────────────────────────────────────────────────


class TestNormalizeMpnKey:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("LM2596S-5.0", "lm2596s50", id="strips_dashes"),
            pytest.param("LM 317 T", "lm317t", id="strips_spaces"),
            pytest.param("IC-7805#REV.A", "ic7805reva", id="strips_special"),
            pytest.param("", "", id="empty"),
            pytest.param(None, "", id="none"),
        ],
    )
    def test_normalize_mpn_key(self, value, expected):
        assert normalize_mpn_key(value) == expected


# ── fuzzy_mpn_match ──────────────────────────────────────────────────


class TestFuzzyMpnMatch:
    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [
            pytest.param("LM317T", "LM317T", True, id="exact_match"),
            pytest.param("lm317t", "LM317T", True, id="case_insensitive"),
            pytest.param("LM2596S-5.0", "LM2596S5.0", True, id="dash_vs_no_dash"),
            # Trailing revision is detected regardless of argument order (shorter
            # base passed first). Suffix "A" is <= 2 chars → likely the same part.
            pytest.param("SN74HC595N", "SN74HC595NA", True, id="trailing_revision"),
            pytest.param("LM317T", "NE555P", False, id="completely_different"),
            pytest.param(None, "LM317T", False, id="none_a"),
            pytest.param("LM317T", None, False, id="none_b"),
            pytest.param(None, None, False, id="both_none"),
            # MPNs shorter than 3 chars after normalize are rejected
            pytest.param("AB", "AB", False, id="short_mpn_rejected"),
            # Single-char trailing suffix (revision) IS a fuzzy match, either order.
            pytest.param("LM317T", "LM317TA", True, id="one_prefix_of_other_short_suffix"),
            # Empty string MPN returns False.
            pytest.param("", "LM317T", False, id="empty_string_a"),
            pytest.param("LM317T", "", False, id="empty_string_b"),
        ],
    )
    def test_fuzzy_mpn_match(self, a, b, expected):
        assert fuzzy_mpn_match(a, b) is expected


# ── normalize_condition ──────────────────────────────────────────────


class TestNormalizeCondition:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("New", "new", id="new"),
            pytest.param("Factory New", "new", id="factory_new"),
            pytest.param("Brand New", "new", id="brand_new"),
            pytest.param("Original", "new", id="original"),
            pytest.param("OEM", "new", id="oem"),
            pytest.param("Genuine", "new", id="genuine"),
            pytest.param("Refurbished", "refurb", id="refurbished"),
            pytest.param("refurb", "refurb", id="refurb"),
            pytest.param("Reconditioned", "refurb", id="reconditioned"),
            pytest.param("Reclaimed", "refurb", id="reclaimed"),
            pytest.param("Used", "used", id="used"),
            pytest.param("Pulls", "used", id="pulls"),
            pytest.param("Surplus", "used", id="surplus"),
            pytest.param("Excess", "used", id="excess"),
            pytest.param("FACTORY NEW", "new", id="mixed_case"),
            pytest.param("Grade A", None, id="unknown"),
            pytest.param(None, None, id="none"),
            pytest.param("", None, id="empty"),
        ],
    )
    def test_normalize_condition(self, value, expected):
        assert normalize_condition(value) == expected


# ── normalize_packaging ──────────────────────────────────────────────


class TestNormalizePackaging:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("Tape and Reel", "reel", id="tape_and_reel"),
            pytest.param("Tape & Reel", "reel", id="tape_ampersand_reel"),
            pytest.param("T&R", "reel", id="t_and_r"),
            pytest.param("Cut Tape", "cut_tape", id="cut_tape"),
            pytest.param("Tray", "tray", id="tray"),
            pytest.param("Reel", "reel", id="reel"),
            pytest.param("Tube", "tube", id="tube"),
            pytest.param("Bulk", "bulk", id="bulk"),
            pytest.param("Bag", "bag", id="bag"),
            pytest.param("Loose", "bulk", id="loose"),
            pytest.param("CT", "cut_tape", id="ct"),
            pytest.param("DIP", "tube", id="dip"),
            pytest.param("SMD", "reel", id="smd"),
            pytest.param("TR", "reel", id="tr_shorthand"),
            pytest.param("Custom Box", "box", id="box"),
            pytest.param("Custom Wrap", None, id="unknown"),
            pytest.param(None, None, id="none"),
            pytest.param("", None, id="empty"),
            pytest.param("TAPE AND REEL", "reel", id="mixed_case"),
        ],
    )
    def test_normalize_packaging(self, value, expected):
        assert normalize_packaging(value) == expected


# ── normalize_mpn ────────────────────────────────────────────────────


class TestNormalizeMpn:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("lm317t", "LM317T", id="basic_uppercase"),
            pytest.param("  LM317T  ", "LM317T", id="strips_whitespace"),
            pytest.param("LM2596S-5.0", "LM2596S-5.0", id="preserves_dashes"),
            pytest.param("IC-78.05", "IC-78.05", id="preserves_dots"),
            pytest.param("LM 317 T", "LM317T", id="collapses_internal_spaces"),
            pytest.param("'LM317T'", "LM317T", id="strips_single_quotes"),
            pytest.param('"LM317T"', "LM317T", id="strips_double_quotes"),
            pytest.param("AB", None, id="short_rejected"),
            pytest.param(None, None, id="none"),
            pytest.param("", None, id="empty"),
        ],
    )
    def test_normalize_mpn(self, value, expected):
        assert normalize_mpn(value) == expected


# ── Additional edge cases for uncovered branches ─────────────────────


class TestNormalizePriceEdgeCases:
    """Cover lines 85-86: price range where first part is not a float."""

    def test_range_with_non_numeric_first_part(self):
        # "abc-0.42" → "-" found but "abc" can't be float → falls through
        # to other parsing which also fails → None
        result = normalize_price("abc-0.42")
        # After stripping currency symbols and the range check fails,
        # "abc-0.42" is parsed normally which fails → None
        assert result is None

    def test_range_starting_with_dash_is_treated_as_negative(self):
        # "-0.38-0.42" starts with "-" so range is not applied
        result = normalize_price("-0.38")
        assert result is None  # negative price returns None


class TestNormalizeQuantityEdgeCases:
    """Cover lines 163-164: K/M multiplier with non-numeric prefix."""

    def test_k_suffix_with_non_numeric_prefix(self):
        # "XK" ends with "K" but "X" can't be float → falls through to plain parse
        result = normalize_quantity("XK")
        assert result is None

    def test_m_suffix_with_non_numeric_prefix(self):
        result = normalize_quantity("abcM")
        assert result is None


class TestNormalizeLeadTimeEdgeCases:
    """Cover line 212: ambiguous number > 52 treated as days."""

    def test_large_ambiguous_number_treated_as_days(self):
        # Number > 52 with no unit → assume days (multiplier=1)
        result = normalize_lead_time("90")
        assert result == 90  # 90 * 1 = 90 days

    def test_ambiguous_number_at_boundary(self):
        # Exactly 52 → assume weeks (52 * 7 = 364)
        result = normalize_lead_time("52")
        assert result == 364  # 52 weeks


class TestNormalizeDateCodeEdgeCases:
    """Cover line 278: date code with fewer than 2 digits → None."""

    def test_single_digit_returns_none(self):
        result = normalize_date_code("A1")
        # "A1" has 1 digit — not enough → None
        assert result is None

    def test_no_digits_returns_none(self):
        result = normalize_date_code("XYZ")
        assert result is None


class TestFuzzyMpnMatchRevision:
    """Cover line 400: short suffix means likely revision (True)."""

    def test_two_char_suffix_revision(self):
        # "LM317TN" vs "LM317T" — 1-char suffix "N" → likely revision
        result = fuzzy_mpn_match("LM317TN", "LM317T")
        assert result is True

    def test_two_char_suffix_exactly(self):
        # Up to 2 chars difference is a revision
        result = fuzzy_mpn_match("NE555P", "NE555")
        assert result is True

    def test_revision_match_is_symmetric(self):
        # The match must hold regardless of argument order — the base MPN passed
        # first (the common "search the base part, listing carries a suffix" case)
        # must still match. Regression guard for the old asymmetric str.replace.
        assert fuzzy_mpn_match("LM317T", "LM317TN") is True
        assert fuzzy_mpn_match("LM317TN", "LM317T") is True
        assert fuzzy_mpn_match("17P9905", "17P9905-LF") is True
        assert fuzzy_mpn_match("17P9905-LF", "17P9905") is True

    def test_long_suffix_still_rejected(self):
        # A 3+ char trailing difference is NOT a revision (different part).
        assert fuzzy_mpn_match("LM317T", "LM317TABC") is False


class TestParseSubstituteMpns:
    """Cover lines 417-435: parse_substitute_mpns function."""

    def test_empty_list_returns_empty(self):
        result = parse_substitute_mpns([], "LM317T")
        assert result == []

    def test_none_list_returns_empty(self):
        # None is treated as falsy
        result = parse_substitute_mpns(None, "LM317T")
        assert result == []

    def test_basic_substitute_list(self):
        subs = [{"mpn": "LM317AT", "manufacturer": "TI"}]
        result = parse_substitute_mpns(subs, "LM317T")
        assert len(result) == 1
        assert result[0]["mpn"] == "LM317AT"
        assert result[0]["manufacturer"] == "TI"

    def test_deduplication(self):
        # Same MPN twice should deduplicate
        subs = [
            {"mpn": "LM317AT", "manufacturer": "TI"},
            {"mpn": "LM317AT", "manufacturer": "Texas Instruments"},
        ]
        result = parse_substitute_mpns(subs, "LM317T")
        assert len(result) == 1

    def test_primary_mpn_excluded(self):
        # Primary MPN should not be in substitute list
        subs = [
            {"mpn": "LM317T", "manufacturer": "TI"},
            {"mpn": "LM317AT", "manufacturer": "TI"},
        ]
        result = parse_substitute_mpns(subs, "LM317T")
        assert len(result) == 1
        assert result[0]["mpn"] == "LM317AT"

    def test_blank_mpn_skipped(self):
        subs = [
            {"mpn": "", "manufacturer": "TI"},
            {"mpn": "LM317AT", "manufacturer": "TI"},
        ]
        result = parse_substitute_mpns(subs, "LM317T")
        assert len(result) == 1

    def test_limit_enforced(self):
        subs = [{"mpn": f"MPN{i:03d}", "manufacturer": "TI"} for i in range(25)]
        result = parse_substitute_mpns(subs, "BASELINE")
        assert len(result) <= 20

    def test_manufacturer_field_optional(self):
        subs = [{"mpn": "NE555P"}]
        result = parse_substitute_mpns(subs, "LM317T")
        assert len(result) == 1
        assert result[0]["manufacturer"] == ""

    def test_legacy_string_form_substitutes_do_not_crash(self):
        # Legacy DB rows hold plain strings, e.g. ["LM338T"] — must not raise.
        result = parse_substitute_mpns(["LM338T"], "LM317T")
        assert len(result) == 1
        assert result[0]["mpn"] == "LM338T"

    def test_non_dict_non_string_entries_skipped(self):
        # Malformed rows may hold neither dicts nor strings — skip, don't crash.
        result = parse_substitute_mpns([123, {"mpn": "LM338T"}], "LM317T")
        assert len(result) == 1
        assert result[0]["mpn"] == "LM338T"
        assert result[0]["manufacturer"] == ""

    def test_mixed_string_and_dict_forms(self):
        result = parse_substitute_mpns(["ABC123", {"mpn": "DEF456", "manufacturer": "TI"}], "LM317T")
        mpns = {r["mpn"] for r in result}
        assert mpns == {"ABC123", "DEF456"}

    def test_dict_with_none_mpn_skipped_not_crash(self):
        result = parse_substitute_mpns([{"mpn": None}, {"mpn": "LM317AT"}], "LM317T")
        assert len(result) == 1
        assert result[0]["mpn"] == "LM317AT"

    def test_mpn_normalized_to_uppercase(self):
        subs = [{"mpn": "lm317at", "manufacturer": "TI"}]
        result = parse_substitute_mpns(subs, "LM317T")
        assert len(result) == 1
        assert result[0]["mpn"] == "LM317AT"


class TestFuzzyMpnMatchBoundary:
    """Boundary coverage for the symmetric revision-suffix check."""

    def test_identical_after_strip_is_exact_not_revision(self):
        # Equal stripped keys → caught by the exact/stripped branch (suffix length 0),
        # never misclassified as a 0-length revision.
        assert fuzzy_mpn_match("LM-317-T", "LM317T") is True

    def test_three_char_suffix_rejected_both_orders(self):
        assert fuzzy_mpn_match("LM317TABC", "LM317T") is False
        assert fuzzy_mpn_match("LM317T", "LM317TABC") is False


class TestParseWebsiteDomain:
    """parse_website_domain — validated urlsplit-based domain extractor (F12),
    consolidated out of app.routers.sightings._parse_website_domain (removed) so
    app.services.company_import_service._company_domain can share it instead of a
    narrower ad-hoc regex."""

    def test_bare_domain(self):
        assert parse_website_domain("acme.com") == "acme.com"

    def test_strips_scheme_and_www(self):
        assert parse_website_domain("https://www.acme.com/contact") == "acme.com"
        assert parse_website_domain("http://www.acme.com") == "acme.com"

    def test_strips_only_one_leading_www(self):
        """Never a blanket str.replace that would mangle 'wwwacme.com' or eat
        'www.www.acme.com' entirely."""
        assert parse_website_domain("www.wwwacme.com") == "wwwacme.com"

    def test_lowercases(self):
        assert parse_website_domain("ACME.COM") == "acme.com"

    def test_subdomain_preserved(self):
        assert parse_website_domain("shop.acme.com") == "shop.acme.com"

    def test_rejects_host_with_no_dot(self):
        assert parse_website_domain("localhost") == ""

    def test_rejects_user_at_host_port_junk(self):
        """A pasted "user@host:8080" credential/port string must NOT naively parse into
        a bogus host — this is exactly the validation gap the naive str.replace-based
        extractors elsewhere in the codebase don't guard."""
        assert parse_website_domain("user@host:8080") == ""

    def test_rejects_empty_string(self):
        assert parse_website_domain("") == ""

    def test_rejects_whitespace_only(self):
        assert parse_website_domain("   ") == ""


class TestCompanyDomainDelegatesToSharedValidator:
    """app.services.company_import_service._company_domain wraps the shared
    parse_website_domain instead of duplicating a narrower regex."""

    def test_valid_website_extracts_domain(self):
        from app.services.company_import_service import _company_domain

        assert _company_domain("https://www.acme.com/about") == "acme.com"

    def test_none_website_returns_none(self):
        from app.services.company_import_service import _company_domain

        assert _company_domain(None) is None

    def test_empty_website_returns_none(self):
        from app.services.company_import_service import _company_domain

        assert _company_domain("") is None

    def test_junk_user_at_host_rejected_like_sightings(self):
        """Regression: the consolidated extractor must reject junk the same way the
        validated sightings.py extractor always did — not silently accept a bogus
        domain the way the old narrower regex-based _company_domain might have."""
        from app.services.company_import_service import _company_domain

        assert _company_domain("user@host:8080") is None
