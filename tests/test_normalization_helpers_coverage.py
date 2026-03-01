"""
test_normalization_helpers_coverage.py — Additional coverage for app/utils/normalization_helpers.py

Covers uncovered lines:
- normalize_phone_e164: 11+ digits path, 7-9 digits path (lines 58-66)
- clean_contact_name: roman numeral fix, various department patterns
- fix_encoding: mojibake patterns
- normalize_country/normalize_us_state: whitespace-only edge case
"""

from app.utils.normalization_helpers import (
    clean_contact_name,
    fix_encoding,
    normalize_country,
    normalize_phone_e164,
    normalize_us_state,
)

# ── normalize_phone_e164 edge cases ─────────────────────────────────


class TestNormalizePhoneEdgeCases:
    def test_11_plus_digits_no_plus(self):
        """11+ digits without + prefix — assumes country code included (line 59)."""
        result = normalize_phone_e164("442079460958")
        assert result == "+442079460958"

    def test_7_digit_number(self):
        """7 digit number gets +1 prefix (line 63-64)."""
        result = normalize_phone_e164("5551234")
        assert result == "+15551234"

    def test_8_digit_number(self):
        """8 digit number gets +1 prefix."""
        result = normalize_phone_e164("55512345")
        assert result == "+155512345"

    def test_9_digit_number(self):
        """9 digit number gets +1 prefix."""
        result = normalize_phone_e164("555123456")
        assert result == "+1555123456"

    def test_whitespace_only(self):
        """Whitespace-only string returns None."""
        assert normalize_phone_e164("   ") is None

    def test_extension_x_format(self):
        """Extension with 'x' prefix is stripped."""
        result = normalize_phone_e164("555-123-4567 x123")
        assert result == "+15551234567"

    def test_extension_keyword(self):
        """Extension with 'extension' keyword is stripped."""
        result = normalize_phone_e164("555-123-4567 extension 456")
        assert result == "+15551234567"


# ── clean_contact_name edge cases ───────────────────────────────────


class TestCleanContactNameEdgeCases:
    def test_obrien_casing(self):
        """O'Brien pattern is correctly cased."""
        name, is_person = clean_contact_name("john o'brien")
        assert name == "John O'Brien"
        assert is_person is True

    def test_mcdonald_casing(self):
        """McDonald pattern is correctly cased."""
        name, is_person = clean_contact_name("john mcdonald")
        # title() -> "John Mcdonald", then Mc([a-z]) regex -> "John McDonald"
        assert name == "John McDonald"
        assert is_person is True

    def test_roman_numeral_ii(self):
        """Roman numeral II is uppercased."""
        name, is_person = clean_contact_name("john smith ii")
        assert "II" in name

    def test_roman_numeral_iii(self):
        """Roman numeral III is uppercased."""
        name, is_person = clean_contact_name("james wilson iii")
        assert "III" in name

    def test_roman_numeral_iv(self):
        """Roman numeral IV is uppercased."""
        name, is_person = clean_contact_name("robert jones iv")
        assert "IV" in name

    def test_purchasing_department(self):
        """Purchasing is detected as department."""
        _, is_person = clean_contact_name("Purchasing")
        assert is_person is False

    def test_procurement_department(self):
        _, is_person = clean_contact_name("Procurement Department")
        assert is_person is False

    def test_customer_service_department(self):
        _, is_person = clean_contact_name("Customer Service")
        assert is_person is False

    def test_accounting_department(self):
        _, is_person = clean_contact_name("Accounting")
        assert is_person is False

    def test_rfq_department(self):
        _, is_person = clean_contact_name("RFQ Department")
        assert is_person is False

    def test_quotes_department(self):
        _, is_person = clean_contact_name("Quotes")
        assert is_person is False

    def test_trailing_punctuation_stripped(self):
        """Trailing punctuation artifacts are stripped."""
        name, _ = clean_contact_name("John Smith -")
        assert name == "John Smith"

    def test_extension_dash_format(self):
        """Extension with dash format stripped."""
        name, _ = clean_contact_name("Jane Doe - Ext: 500")
        assert name == "Jane Doe"

    def test_whitespace_only(self):
        """Whitespace-only returns empty."""
        name, is_person = clean_contact_name("   ")
        assert name == ""
        assert is_person is False


# ── fix_encoding edge cases ─────────────────────────────────────────


class TestFixEncodingEdgeCases:
    def test_empty_string(self):
        """Empty string returns empty (falsy input returned as-is)."""
        assert fix_encoding("") == ""

    def test_mojibake_apostrophe(self):
        """Common mojibake apostrophe pattern is fixed."""
        # \u00e2\u0080\u0099 is the UTF-8 bytes of RIGHT SINGLE QUOTATION MARK
        # when misinterpreted as Latin-1
        result = fix_encoding("\u00e2\u0080\u0099")
        assert result == "'"

    def test_intl_uppercase(self):
        """INT?L pattern is fixed."""
        assert fix_encoding("INT?L") == "Int'l"

    def test_intl_mixed_case(self):
        """Int?l pattern is fixed."""
        assert fix_encoding("Int?l") == "Int'l"

    def test_no_change_for_clean_text(self):
        """Clean text passes through unchanged."""
        assert fix_encoding("Normal Company Name") == "Normal Company Name"


# ── normalize_country edge cases ────────────────────────────────────


class TestNormalizeCountryEdgeCases:
    def test_whitespace_only(self):
        """Whitespace-only returns None."""
        assert normalize_country("   ") is None

    def test_uae(self):
        assert normalize_country("UAE") == "AE"

    def test_prc(self):
        assert normalize_country("PRC") == "CN"

    def test_costa_rica(self):
        assert normalize_country("Costa Rica") == "CR"

    def test_puerto_rico(self):
        assert normalize_country("Puerto Rico") == "PR"


# ── normalize_us_state edge cases ───────────────────────────────────


class TestNormalizeUSStateEdgeCases:
    def test_whitespace_only(self):
        """Whitespace-only returns None."""
        assert normalize_us_state("   ") is None

    def test_dc_abbreviation(self):
        assert normalize_us_state("DC") == "DC"

    def test_dc_dotted(self):
        assert normalize_us_state("d.c.") == "DC"

    def test_territory_guam(self):
        assert normalize_us_state("Guam") == "GU"

    def test_territory_american_samoa(self):
        assert normalize_us_state("American Samoa") == "AS"

    def test_unknown_passthrough(self):
        """Unknown state passes through unchanged."""
        assert normalize_us_state("SomeProvince") == "SomeProvince"
