"""Tests for app/schemas/crm.py — CRM Pydantic validation models."""

import pytest
from pydantic import ValidationError

from app.schemas.crm import (
    AddContactsToVendor,
    AddContactToSite,
    CompanyCreate,
    CompanyUpdate,
    CustomerImportRow,
    EnrichDomainRequest,
    OfferCreate,
    OfferUpdate,
    OneDriveAttach,
    QuoteCreate,
    QuoteLineItem,
    QuoteReopen,
    QuoteResult,
    QuoteSendOverride,
    QuoteUpdate,
    SuggestedContactItem,
    SuggestedSiteContact,
)

# ── Company schemas ──────────────────────────────────────────────────


class TestCompanyCreate:
    def test_valid_minimal(self) -> None:
        c = CompanyCreate(name="Acme Corp")
        assert c.name == "Acme Corp"
        assert c.website is None

    def test_strips_whitespace(self) -> None:
        c = CompanyCreate(name="  Acme Corp  ")
        assert c.name == "Acme Corp"

    def test_blank_name_raises(self) -> None:
        with pytest.raises(ValidationError):
            CompanyCreate(name="   ")

    def test_missing_name_raises(self) -> None:
        with pytest.raises(ValidationError):
            CompanyCreate()  # type: ignore[call-arg]


class TestCompanyUpdate:
    def test_all_optional(self) -> None:
        u = CompanyUpdate()
        assert u.name is None

    def test_exclude_unset(self) -> None:
        u = CompanyUpdate(name="New Name")
        dumped = u.model_dump(exclude_unset=True)
        assert dumped == {"name": "New Name"}
        assert "website" not in dumped


# ── Offer schemas ────────────────────────────────────────────────────


class TestOfferCreate:
    def test_valid(self) -> None:
        o = OfferCreate(mpn="LM317T", vendor_name="Arrow")
        assert o.condition == "new"
        assert o.status == "active"

    def test_blank_mpn_raises(self) -> None:
        with pytest.raises(ValidationError):
            OfferCreate(mpn="  ", vendor_name="Arrow")

    def test_blank_vendor_raises(self) -> None:
        with pytest.raises(ValidationError):
            OfferCreate(mpn="LM317T", vendor_name="  ")


# ── Quote schemas ────────────────────────────────────────────────────


class TestQuoteResult:
    def test_won(self) -> None:
        r = QuoteResult(result="won")
        assert r.result == "won"
        assert r.reason is None

    def test_lost_with_reason(self) -> None:
        r = QuoteResult(result="lost", reason="Price too high")
        assert r.reason == "Price too high"

    def test_invalid_result_raises(self) -> None:
        with pytest.raises(ValidationError):
            QuoteResult(result="pending")  # type: ignore[arg-type]


class TestQuoteReopen:
    def test_defaults_to_no_revise(self) -> None:
        r = QuoteReopen()
        assert r.revise is False

    def test_revise_true(self) -> None:
        r = QuoteReopen(revise=True)
        assert r.revise is True


# ── Enrichment schemas ───────────────────────────────────────────────


class TestEnrichDomainRequest:
    def test_empty(self) -> None:
        e = EnrichDomainRequest()
        assert e.domain is None

    def test_with_domain(self) -> None:
        e = EnrichDomainRequest(domain="acme.com")
        assert e.domain == "acme.com"


# ── Suggested Contacts schemas ───────────────────────────────────────


class TestSuggestedContactItem:
    def test_valid(self) -> None:
        c = SuggestedContactItem(email="john@acme.com", full_name="John Doe")
        assert c.email == "john@acme.com"
        assert c.source == "enrichment"
        assert c.label == "Sales"

    def test_email_lowercased_and_stripped(self) -> None:
        c = SuggestedContactItem(email="  JOHN@ACME.COM  ")
        assert c.email == "john@acme.com"

    def test_blank_email_raises(self) -> None:
        with pytest.raises(ValidationError):
            SuggestedContactItem(email="  ")


class TestAddContactsToVendor:
    def test_valid(self) -> None:
        payload = AddContactsToVendor(
            vendor_card_id=5,
            contacts=[SuggestedContactItem(email="a@b.com")],
        )
        assert len(payload.contacts) == 1

    def test_missing_vendor_card_id_raises(self) -> None:
        with pytest.raises(ValidationError):
            AddContactsToVendor(contacts=[])  # type: ignore[call-arg]


class TestAddContactToSite:
    def test_valid(self) -> None:
        payload = AddContactToSite(
            site_id=3,
            contact=SuggestedSiteContact(full_name="Jane", email="j@co.com"),
        )
        assert payload.contact.full_name == "Jane"


# ── CompanyCreate phone normalization ───────────────────────────────


class TestCompanyCreatePhone:
    def test_phone_none_passes(self) -> None:
        c = CompanyCreate(name="Acme")
        assert c.phone is None

    def test_phone_normalized(self) -> None:
        c = CompanyCreate(name="Acme", phone="(555) 123-4567")
        assert c.phone is not None
        assert c.phone.startswith("+")

    def test_phone_local_number_preserved_not_rejected(self) -> None:
        """Regression: a 7-digit local number can't be parsed to E.164, but the write must
        NOT 422 — the raw input is preserved (mirrors hq_state/hq_country fallback)."""
        c = CompanyCreate(name="Acme", phone="555-1234")
        assert c.phone == "555-1234"

    def test_phone_blank_collapses_to_none(self) -> None:
        c = CompanyCreate(name="Acme", phone="   ")
        assert c.phone is None


# ── CompanyCreate hq normalization (parity with CompanyUpdate) ──────


class TestCompanyCreateHqNormalization:
    @pytest.mark.parametrize(
        "field,value,expected",
        [
            pytest.param("hq_country", None, None, id="hq_country_none"),
            pytest.param("hq_country", "United States", "US", id="hq_country_value"),
            pytest.param("hq_state", None, None, id="hq_state_none"),
            pytest.param("hq_state", "California", "CA", id="hq_state_value"),
        ],
    )
    def test_normalize(self, field, value, expected) -> None:
        c = CompanyCreate(name="Acme", **{field: value})
        assert getattr(c, field) == expected

    def test_create_update_parity(self) -> None:
        created = CompanyCreate(name="Acme", hq_country="United States", hq_state="California")
        updated = CompanyUpdate(hq_country="United States", hq_state="California")
        assert created.hq_country == updated.hq_country == "US"
        assert created.hq_state == updated.hq_state == "CA"


# ── CompanyUpdate validators ────────────────────────────────────────


class TestCompanyUpdateValidators:
    @pytest.mark.parametrize(
        "field,value,expected",
        [
            pytest.param("hq_country", None, None, id="hq_country_none"),
            pytest.param("hq_country", "United States", "US", id="hq_country_value"),
            pytest.param("hq_state", None, None, id="hq_state_none"),
            pytest.param("hq_state", "California", "CA", id="hq_state_value"),
            pytest.param("phone", None, None, id="phone_none"),
        ],
    )
    def test_normalize(self, field, value, expected) -> None:
        u = CompanyUpdate(**{field: value})
        assert getattr(u, field) == expected

    def test_normalize_phone_value(self) -> None:
        u = CompanyUpdate(phone="(555) 123-4567")
        assert u.phone is not None
        assert u.phone.startswith("+")

    def test_phone_local_number_preserved_not_rejected(self) -> None:
        """Regression: an un-parseable local number is preserved, not 422'd."""
        u = CompanyUpdate(phone="555-1234")
        assert u.phone == "555-1234"


# ── OfferCreate validators ─────────────────────────────────────────


class TestOfferCreateValidators:
    @pytest.mark.parametrize(
        "overrides,field,expected",
        [
            pytest.param({}, "packaging", None, id="packaging_none"),
            pytest.param({"packaging": "Tape & Reel"}, "packaging", "reel", id="packaging_normalized"),
            pytest.param({}, "date_code", None, id="date_code_none"),
            pytest.param({"date_code": "DC 2024"}, "date_code", "2024", id="date_code_normalized"),
            pytest.param({"condition": "used"}, "condition", "pulls", id="condition_normalized"),
            pytest.param({"mpn": "lm317t"}, "mpn", "LM317T", id="mpn_normalized"),
        ],
    )
    def test_field_normalized(self, overrides, field, expected) -> None:
        o = OfferCreate(**{"mpn": "LM317T", "vendor_name": "Arrow", **overrides})
        assert getattr(o, field) == expected


# ── OfferUpdate validators ─────────────────────────────────────────


class TestOfferUpdate:
    def test_all_none(self) -> None:
        u = OfferUpdate()
        assert u.mpn is None

    @pytest.mark.parametrize(
        "field,value,expected",
        [
            pytest.param("mpn", None, None, id="mpn_none"),
            pytest.param("mpn", "lm317t", "LM317T", id="mpn_normalized"),
            pytest.param("condition", None, None, id="condition_none"),
            pytest.param("condition", "refurbished", "refurb", id="condition_normalized"),
            pytest.param("packaging", None, None, id="packaging_none"),
            pytest.param("packaging", "Tube", "tube", id="packaging_normalized"),
        ],
    )
    def test_field_normalized(self, field, value, expected) -> None:
        u = OfferUpdate(**{field: value})
        assert getattr(u, field) == expected


# ── QuoteLineItem ──────────────────────────────────────────────────


class TestQuoteLineItem:
    def test_defaults(self) -> None:
        li = QuoteLineItem()
        assert li.qty == 0
        assert li.unit_cost == 0
        assert li.margin == 0

    def test_extra_fields_allowed(self) -> None:
        li = QuoteLineItem(custom_field="value")
        assert li.custom_field == "value"


# ── QuoteCreate / QuoteUpdate ──────────────────────────────────────


class TestQuoteCreate:
    def test_defaults(self) -> None:
        q = QuoteCreate()
        assert q.offer_ids == []
        assert q.line_items == []


class TestQuoteUpdate:
    def test_all_none(self) -> None:
        q = QuoteUpdate()
        assert q.line_items is None
        assert q.payment_terms is None


# ── CustomerImportRow ──────────────────────────────────────────────


class TestCustomerImportRow:
    def test_valid(self) -> None:
        r = CustomerImportRow(company_name="Acme Corp")
        assert r.company_name == "Acme Corp"
        assert r.site_name == "HQ"

    def test_blank_company_name_raises(self) -> None:
        with pytest.raises(ValidationError):
            CustomerImportRow(company_name="   ")


# ── Misc schemas ───────────────────────────────────────────────────


class TestMiscSchemas:
    def test_onedrive_attach(self) -> None:
        o = OneDriveAttach(item_id="abc123")
        assert o.item_id == "abc123"

    def test_quote_send_override(self) -> None:
        q = QuoteSendOverride()
        assert q.to_email is None
        assert q.to_name is None
