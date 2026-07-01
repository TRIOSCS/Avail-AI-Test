"""tests/test_utils_normalization_ext.py — Extended tests for
app/utils/normalization.py."""

import os

os.environ["TESTING"] = "1"

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
)


class TestNormalizePrice:
    def test_none_returns_none(self):
        assert normalize_price(None) is None

    def test_integer_input(self):
        assert normalize_price(10) == 10.0

    def test_zero_returns_none(self):
        assert normalize_price(0) is None

    def test_float_input(self):
        assert normalize_price(1.5) == 1.5

    def test_string_number(self):
        assert normalize_price("1.23") == 1.23

    def test_comma_separated(self):
        assert normalize_price("1,234.56") == 1234.56

    def test_dollar_sign_stripped(self):
        assert normalize_price("$5.00") == 5.0

    def test_euro_sign_stripped(self):
        assert normalize_price("€10.50") == 10.5

    def test_range_takes_lower(self):
        assert normalize_price("0.38-0.42") == 0.38

    def test_km_shorthand_k(self):
        assert normalize_price("1.5k") == 1500.0

    def test_km_shorthand_M(self):
        assert normalize_price("2M") == 2_000_000.0

    def test_per_unit_suffix_stripped(self):
        assert normalize_price("5.00/ea") == 5.0

    def test_usd_code_stripped(self):
        assert normalize_price("USD 3.50") == 3.5

    def test_negative_returns_none(self):
        assert normalize_price("-1.0") is None

    def test_empty_string_returns_none(self):
        assert normalize_price("") is None


class TestDetectCurrency:
    def test_empty_returns_usd(self):
        assert detect_currency("") == "USD"

    def test_none_returns_usd(self):
        assert detect_currency(None) == "USD"

    def test_dollar_sign(self):
        assert detect_currency("$5.00") == "USD"

    def test_euro_sign(self):
        assert detect_currency("€10.00") == "EUR"

    def test_pound_sign(self):
        assert detect_currency("£8.00") == "GBP"

    def test_yen_sign(self):
        assert detect_currency("¥500") == "JPY"

    def test_usd_code(self):
        assert detect_currency("USD") == "USD"

    def test_eur_code(self):
        assert detect_currency("EUR") == "EUR"

    def test_unknown_defaults_usd(self):
        assert detect_currency("5.00") == "USD"


class TestNormalizeQuantity:
    def test_none_returns_none(self):
        assert normalize_quantity(None) is None

    def test_integer(self):
        assert normalize_quantity(1000) == 1000

    def test_float(self):
        assert normalize_quantity(500.0) == 500

    def test_string_number(self):
        assert normalize_quantity("5000") == 5000

    def test_comma_separated(self):
        assert normalize_quantity("50,000") == 50000

    def test_k_suffix(self):
        assert normalize_quantity("50K") == 50000

    def test_lowercase_k(self):
        assert normalize_quantity("100k") == 100000

    def test_m_suffix(self):
        assert normalize_quantity("2M") == 2_000_000

    def test_plus_sign_stripped(self):
        assert normalize_quantity("50000+") == 50000

    def test_zero_returns_none(self):
        assert normalize_quantity(0) is None

    def test_negative_float_returns_none(self):
        assert normalize_quantity(-1.0) is None

    def test_text_returns_none(self):
        assert normalize_quantity("n/a") is None


class TestNormalizeLeadTime:
    def test_none_returns_none(self):
        assert normalize_lead_time(None) is None

    def test_empty_returns_none(self):
        assert normalize_lead_time("") is None

    def test_stock(self):
        assert normalize_lead_time("stock") == 0

    def test_in_stock(self):
        assert normalize_lead_time("in stock") == 0

    def test_immediate(self):
        assert normalize_lead_time("immediate") == 0

    def test_weeks(self):
        result = normalize_lead_time("4 weeks")
        assert result == 28

    def test_week_range_midpoint(self):
        result = normalize_lead_time("4-6 weeks")
        assert result == 35  # midpoint of 4,6 * 7

    def test_days(self):
        result = normalize_lead_time("30 days")
        assert result == 30

    def test_months(self):
        result = normalize_lead_time("2 months")
        assert result == 60

    def test_no_unit_small_number_assumes_weeks(self):
        result = normalize_lead_time("8")
        assert result == 56  # 8 * 7

    def test_aro_is_days(self):
        result = normalize_lead_time("14 ARO")
        assert result == 14

    def test_no_digits_returns_none(self):
        assert normalize_lead_time("TBD") is None


class TestNormalizeCondition:
    def test_none_returns_none(self):
        assert normalize_condition(None) is None

    def test_new(self):
        assert normalize_condition("new") == "new"

    def test_factory_new(self):
        assert normalize_condition("factory new") == "new"

    def test_brand_new(self):
        assert normalize_condition("brand new") == "new"

    def test_refurbished(self):
        assert normalize_condition("refurbished") == "refurb"

    def test_reconditioned(self):
        assert normalize_condition("reconditioned") == "refurb"

    def test_used(self):
        assert normalize_condition("used") == "used"

    def test_pulls(self):
        assert normalize_condition("pulls") == "used"

    def test_surplus(self):
        assert normalize_condition("surplus") == "used"

    def test_unknown_returns_none(self):
        assert normalize_condition("A-Grade") is None

    def test_case_insensitive(self):
        assert normalize_condition("NEW") == "new"
        assert normalize_condition("Refurbished") == "refurb"


class TestNormalizeDateCode:
    def test_none_returns_none(self):
        assert normalize_date_code(None) is None

    def test_empty_returns_none(self):
        assert normalize_date_code("") is None

    def test_na_returns_none(self):
        assert normalize_date_code("N/A") is None

    def test_tbd_returns_none(self):
        assert normalize_date_code("TBD") is None

    def test_year_passes_through(self):
        assert normalize_date_code("2024") == "2024"

    def test_year_with_plus(self):
        assert normalize_date_code("2024+") == "2024+"

    def test_dc_prefix_stripped(self):
        result = normalize_date_code("DC 2024")
        assert result == "2024"

    def test_date_code_prefix_stripped(self):
        result = normalize_date_code("Date Code: 2301")
        assert "2301" in result

    def test_yyww_format(self):
        assert normalize_date_code("2345") == "2345"


class TestNormalizeMoq:
    def test_none_returns_none(self):
        assert normalize_moq(None) is None

    def test_integer(self):
        assert normalize_moq(500) == 500

    def test_moq_prefix(self):
        assert normalize_moq("MOQ: 1000") == 1000

    def test_minimum_prefix(self):
        assert normalize_moq("minimum 500") == 500

    def test_k_suffix(self):
        assert normalize_moq("10K") == 10000

    def test_min_prefix_short(self):
        assert normalize_moq("min 250") == 250


class TestNormalizePackaging:
    def test_none_returns_none(self):
        assert normalize_packaging(None) is None

    def test_reel(self):
        assert normalize_packaging("reel") == "reel"

    def test_tape_and_reel(self):
        assert normalize_packaging("tape and reel") == "reel"

    def test_tube(self):
        assert normalize_packaging("tube") == "tube"

    def test_tray(self):
        assert normalize_packaging("tray") == "tray"

    def test_bulk(self):
        assert normalize_packaging("bulk") == "bulk"

    def test_cut_tape(self):
        assert normalize_packaging("cut tape") == "cut_tape"

    def test_case_insensitive(self):
        assert normalize_packaging("REEL") == "reel"

    def test_unknown_returns_none(self):
        assert normalize_packaging("mystery") is None


class TestNormalizeMpn:
    def test_none_returns_none(self):
        assert normalize_mpn(None) is None

    def test_empty_returns_none(self):
        assert normalize_mpn("") is None

    def test_uppercase(self):
        assert normalize_mpn("lm317t") == "LM317T"

    def test_strips_whitespace(self):
        assert normalize_mpn("  LM317T  ") == "LM317T"

    def test_strips_quotes(self):
        assert normalize_mpn('"LM317T"') == "LM317T"

    def test_strips_trailing_punctuation(self):
        assert normalize_mpn("LM317T,") == "LM317T"

    def test_collapses_internal_whitespace(self):
        assert normalize_mpn("LM 317 T") == "LM317T"

    def test_short_mpn_returns_none(self):
        assert normalize_mpn("AB") is None

    def test_preserves_dashes(self):
        assert normalize_mpn("LM2596S-5.0") == "LM2596S-5.0"


class TestNormalizeMpnKey:
    def test_empty_returns_empty(self):
        assert normalize_mpn_key("") == ""

    def test_none_returns_empty(self):
        assert normalize_mpn_key(None) == ""

    def test_strips_dashes(self):
        assert normalize_mpn_key("LM2596S-5.0") == "lm2596s50"

    def test_strips_spaces(self):
        assert normalize_mpn_key("LM 317 T") == "lm317t"

    def test_lowercased(self):
        assert normalize_mpn_key("LM317T") == "lm317t"


class TestFuzzyMpnMatch:
    def test_exact_match(self):
        assert fuzzy_mpn_match("LM317T", "LM317T")

    def test_case_insensitive(self):
        assert fuzzy_mpn_match("lm317t", "LM317T")

    def test_none_a_returns_false(self):
        assert not fuzzy_mpn_match(None, "LM317T")

    def test_none_b_returns_false(self):
        assert not fuzzy_mpn_match("LM317T", None)

    def test_trailing_revision_match(self):
        # One character suffix difference
        assert fuzzy_mpn_match("LM317T", "LM317TE")

    def test_different_mpns_no_match(self):
        assert not fuzzy_mpn_match("LM317T", "NE555P")

    def test_dash_vs_nodash(self):
        assert fuzzy_mpn_match("LM-317T", "LM317T")


class TestParseSubstituteMpns:
    def test_none_returns_empty(self):
        assert parse_substitute_mpns(None, "LM317T") == []

    def test_empty_list_returns_empty(self):
        assert parse_substitute_mpns([], "LM317T") == []

    def test_dict_format(self):
        subs = [{"mpn": "LM338T", "manufacturer": "TI"}]
        result = parse_substitute_mpns(subs, "LM317T")
        assert len(result) == 1
        assert result[0]["mpn"] == "LM338T"
        assert result[0]["manufacturer"] == "TI"

    def test_legacy_string_format(self):
        subs = ["LM338T"]
        result = parse_substitute_mpns(subs, "LM317T")
        assert len(result) == 1
        assert result[0]["mpn"] == "LM338T"

    def test_deduplicates_primary_mpn(self):
        subs = [{"mpn": "LM317T", "manufacturer": "TI"}]
        result = parse_substitute_mpns(subs, "LM317T")
        assert len(result) == 0  # primary excluded

    def test_deduplicates_duplicates(self):
        subs = [{"mpn": "LM338T"}, {"mpn": "LM338T"}]
        result = parse_substitute_mpns(subs, "LM317T")
        assert len(result) == 1

    def test_source_preserved(self):
        subs = [{"mpn": "LM338T", "manufacturer": "TI", "source": "fru_crosswalk"}]
        result = parse_substitute_mpns(subs, "LM317T")
        assert result[0]["source"] == "fru_crosswalk"

    def test_respects_limit(self):
        subs = [{"mpn": f"PART{i:04d}"} for i in range(25)]
        result = parse_substitute_mpns(subs, "OTHER", limit=5)
        assert len(result) == 5

    def test_empty_mpn_skipped(self):
        subs = [{"mpn": ""}, {"mpn": "LM338T"}]
        result = parse_substitute_mpns(subs, "LM317T")
        assert len(result) == 1

    def test_non_dict_non_string_skipped(self):
        subs = [123, {"mpn": "LM338T"}]
        result = parse_substitute_mpns(subs, "LM317T")
        assert len(result) == 1
