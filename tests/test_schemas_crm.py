"""Tests for app/schemas/crm.py — CRM Pydantic validation models."""

import pytest
from pydantic import ValidationError

from app.schemas.crm import (
    AddContactsToVendor,
    AddContactToSite,
    CompanyCreate,
    CompanyUpdate,
    EnrichDomainRequest,
    OfferCreate,
    QuoteReopen,
    QuoteResult,
    SiteCreate,
    SiteUpdate,
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


# ── Site schemas ─────────────────────────────────────────────────────


class TestSiteCreate:
    def test_valid(self) -> None:
        s = SiteCreate(site_name="HQ")
        assert s.site_name == "HQ"
        assert s.country == "US"

    def test_blank_name_raises(self) -> None:
        with pytest.raises(ValidationError):
            SiteCreate(site_name="")


class TestSiteUpdate:
    def test_all_optional(self) -> None:
        u = SiteUpdate()
        assert u.site_name is None
        assert u.is_active is None

    def test_exclude_unset(self) -> None:
        u = SiteUpdate(city="Austin", is_active=False)
        dumped = u.model_dump(exclude_unset=True)
        assert dumped == {"city": "Austin", "is_active": False}


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


# ── Additional coverage for missing lines ───────────────────────────

from app.schemas.crm import (
    CustomerImportRow,
    OfferUpdate,
    OneDriveAttach,
    QuoteCreate,
    QuoteLineItem,
    QuoteSendOverride,
    QuoteUpdate,
    SiteContactCreate,
    SiteContactUpdate,
)

# ── CompanyCreate phone normalization ───────────────────────────────


class TestCompanyCreatePhone:
    def test_phone_none_passes(self) -> None:
        c = CompanyCreate(name="Acme")
        assert c.phone is None

    def test_phone_normalized(self) -> None:
        c = CompanyCreate(name="Acme", phone="(555) 123-4567")
        assert c.phone is not None
        assert c.phone.startswith("+")


# ── CompanyUpdate validators ────────────────────────────────────────


class TestCompanyUpdateValidators:
    def test_normalize_hq_country_none(self) -> None:
        u = CompanyUpdate(hq_country=None)
        assert u.hq_country is None

    def test_normalize_hq_country_value(self) -> None:
        u = CompanyUpdate(hq_country="United States")
        assert u.hq_country == "US"

    def test_normalize_hq_state_none(self) -> None:
        u = CompanyUpdate(hq_state=None)
        assert u.hq_state is None

    def test_normalize_hq_state_value(self) -> None:
        u = CompanyUpdate(hq_state="California")
        assert u.hq_state == "CA"

    def test_normalize_phone_none(self) -> None:
        u = CompanyUpdate(phone=None)
        assert u.phone is None

    def test_normalize_phone_value(self) -> None:
        u = CompanyUpdate(phone="(555) 123-4567")
        assert u.phone is not None
        assert u.phone.startswith("+")


# ── SiteCreate validators ──────────────────────────────────────────


class TestSiteCreateValidators:
    def test_country_normalized(self) -> None:
        s = SiteCreate(site_name="HQ", country="United States")
        assert s.country == "US"

    def test_state_none_passes(self) -> None:
        s = SiteCreate(site_name="HQ")
        assert s.state is None

    def test_state_normalized(self) -> None:
        s = SiteCreate(site_name="HQ", state="California")
        assert s.state == "CA"

    def test_contact_phone_none(self) -> None:
        s = SiteCreate(site_name="HQ")
        assert s.contact_phone is None

    def test_contact_phone_normalized(self) -> None:
        s = SiteCreate(site_name="HQ", contact_phone="(555) 123-4567")
        assert s.contact_phone.startswith("+")


# ── SiteUpdate validators ──────────────────────────────────────────


class TestSiteUpdateValidators:
    def test_country_none(self) -> None:
        u = SiteUpdate(country=None)
        assert u.country is None

    def test_country_normalized(self) -> None:
        u = SiteUpdate(country="Germany")
        assert u.country == "DE"

    def test_state_none(self) -> None:
        u = SiteUpdate(state=None)
        assert u.state is None

    def test_state_normalized(self) -> None:
        u = SiteUpdate(state="Texas")
        assert u.state == "TX"

    def test_contact_phone_none(self) -> None:
        u = SiteUpdate(contact_phone=None)
        assert u.contact_phone is None

    def test_contact_phone_normalized(self) -> None:
        u = SiteUpdate(contact_phone="(555) 123-4567")
        assert u.contact_phone.startswith("+")


# ── SiteContactCreate ──────────────────────────────────────────────


class TestSiteContactCreate:
    def test_valid(self) -> None:
        c = SiteContactCreate(full_name="John Doe")
        assert c.full_name == "John Doe"
        assert c.is_primary is False

    def test_blank_name_raises(self) -> None:
        with pytest.raises(ValidationError):
            SiteContactCreate(full_name="   ")

    def test_phone_none(self) -> None:
        c = SiteContactCreate(full_name="John")
        assert c.phone is None

    def test_phone_normalized(self) -> None:
        c = SiteContactCreate(full_name="John", phone="(555) 123-4567")
        assert c.phone.startswith("+")

    def test_email_none(self) -> None:
        c = SiteContactCreate(full_name="John")
        assert c.email is None

    def test_email_lowered_stripped(self) -> None:
        c = SiteContactCreate(full_name="John", email="  JOHN@ACME.COM  ")
        assert c.email == "john@acme.com"


# ── SiteContactUpdate ─────────────────────────────────────────────


class TestSiteContactUpdate:
    def test_all_none(self) -> None:
        u = SiteContactUpdate()
        assert u.full_name is None

    def test_phone_none(self) -> None:
        u = SiteContactUpdate(phone=None)
        assert u.phone is None

    def test_phone_normalized(self) -> None:
        u = SiteContactUpdate(phone="(555) 123-4567")
        assert u.phone.startswith("+")

    def test_email_none(self) -> None:
        u = SiteContactUpdate(email=None)
        assert u.email is None

    def test_email_lowered_stripped(self) -> None:
        u = SiteContactUpdate(email="  TEST@EXAMPLE.COM  ")
        assert u.email == "test@example.com"


# ── OfferCreate validators ─────────────────────────────────────────


class TestOfferCreateValidators:
    def test_packaging_none(self) -> None:
        o = OfferCreate(mpn="LM317T", vendor_name="Arrow")
        assert o.packaging is None

    def test_packaging_normalized(self) -> None:
        o = OfferCreate(mpn="LM317T", vendor_name="Arrow", packaging="Tape & Reel")
        assert o.packaging == "reel"

    def test_date_code_none(self) -> None:
        o = OfferCreate(mpn="LM317T", vendor_name="Arrow")
        assert o.date_code is None

    def test_date_code_normalized(self) -> None:
        o = OfferCreate(mpn="LM317T", vendor_name="Arrow", date_code="DC 2024")
        assert o.date_code == "2024"

    def test_condition_normalized(self) -> None:
        o = OfferCreate(mpn="LM317T", vendor_name="Arrow", condition="Factory New")
        assert o.condition == "new"

    def test_mpn_normalized(self) -> None:
        o = OfferCreate(mpn="lm317t", vendor_name="Arrow")
        assert o.mpn == "LM317T"


# ── OfferUpdate validators ─────────────────────────────────────────


class TestOfferUpdate:
    def test_all_none(self) -> None:
        u = OfferUpdate()
        assert u.mpn is None

    def test_mpn_none(self) -> None:
        u = OfferUpdate(mpn=None)
        assert u.mpn is None

    def test_mpn_normalized(self) -> None:
        u = OfferUpdate(mpn="lm317t")
        assert u.mpn == "LM317T"

    def test_condition_none(self) -> None:
        u = OfferUpdate(condition=None)
        assert u.condition is None

    def test_condition_normalized(self) -> None:
        u = OfferUpdate(condition="refurbished")
        assert u.condition == "refurb"

    def test_packaging_none(self) -> None:
        u = OfferUpdate(packaging=None)
        assert u.packaging is None

    def test_packaging_normalized(self) -> None:
        u = OfferUpdate(packaging="Tube")
        assert u.packaging == "tube"


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
