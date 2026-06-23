"""test_unified_phone_matcher.py — Tests for the unified E.164 phone matcher.

Covers match_phone_to_entity() in activity_service.py:
- SiteContact match → resolves to Company
- Company match
- CustomerSite match
- VendorContact match → resolves to VendorCard
- VendorCard phones JSON match
- Unknown number → None
- Ambiguous: two distinct entities → ambiguous=True + candidates
- Formatted/international/+1 number formats all resolve via normalized index

Called by: pytest
Depends on: app/services/activity_service.match_phone_to_entity, conftest fixtures
"""

from app.services.activity_service import match_phone_to_entity


class TestUnifiedMatcherSiteContact:
    def test_site_contact_match_resolves_to_company(self, db_session):
        from app.models import Company, CustomerSite, SiteContact

        co = Company(name="SC Test Corp", is_active=True)
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="HQ", is_active=True)
        db_session.add(site)
        db_session.flush()
        contact = SiteContact(customer_site_id=site.id, full_name="Alice", phone="4155550001", is_active=True)
        db_session.add(contact)
        db_session.flush()

        result = match_phone_to_entity("(415) 555-0001", db_session)
        assert result is not None
        assert result["type"] == "company"
        assert result["company_id"] == co.id
        assert result["site_contact_id"] == contact.id

    def test_international_number_resolves(self, db_session):
        from app.models import Company, CustomerSite, SiteContact

        co = Company(name="Intl Corp", is_active=True)
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="HQ", is_active=True)
        db_session.add(site)
        db_session.flush()
        contact = SiteContact(customer_site_id=site.id, full_name="Bob", phone="+14155550002", is_active=True)
        db_session.add(contact)
        db_session.flush()

        # Different format — should still match via E.164 index
        result = match_phone_to_entity("415-555-0002", db_session)
        assert result is not None
        assert result["company_id"] == co.id


class TestUnifiedMatcherCompany:
    def test_company_direct_match(self, db_session):
        from app.models import Company

        co = Company(name="Direct Match Corp", phone="8005550099", is_active=True)
        db_session.add(co)
        db_session.flush()

        result = match_phone_to_entity("+18005550099", db_session)
        assert result is not None
        assert result["type"] == "company"
        assert result["company_id"] == co.id


class TestUnifiedMatcherCustomerSite:
    def test_customer_site_contact_phone_matches(self, db_session):
        from app.models import Company, CustomerSite

        co = Company(name="Site Phone Corp", is_active=True)
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="Branch", contact_phone="3055550101", is_active=True)
        db_session.add(site)
        db_session.flush()

        result = match_phone_to_entity("305-555-0101", db_session)
        assert result is not None
        assert result["type"] == "company"
        assert result["company_id"] == co.id


class TestUnifiedMatcherVendorContact:
    def test_vendor_contact_resolves_to_card(self, db_session):
        from app.models.vendors import VendorCard, VendorContact

        card = VendorCard(normalized_name="acme-vc", display_name="Acme VC", source="test", is_blacklisted=False)
        db_session.add(card)
        db_session.flush()
        vc = VendorContact(vendor_card_id=card.id, source="test", phone="8005550201")
        db_session.add(vc)
        db_session.flush()

        result = match_phone_to_entity("(800) 555-0201", db_session)
        assert result is not None
        assert result["type"] == "vendor"
        assert result["vendor_card_id"] == card.id
        assert result["vendor_contact_id"] == vc.id


class TestUnifiedMatcherVendorCard:
    def test_vendor_card_phones_json_match(self, db_session):
        from app.models.vendors import VendorCard

        card = VendorCard(
            normalized_name="digikey",
            display_name="DigiKey",
            source="test",
            phones=["(800) 344-4539"],
            is_blacklisted=False,
        )
        db_session.add(card)
        db_session.flush()

        result = match_phone_to_entity("8003444539", db_session)
        assert result is not None
        assert result["type"] == "vendor"
        assert result["vendor_card_id"] == card.id

    def test_blacklisted_vendor_not_returned(self, db_session):
        from app.models.vendors import VendorCard

        card = VendorCard(
            normalized_name="blacklisted-co",
            display_name="Blacklisted",
            source="test",
            phones=["5555550301"],
            is_blacklisted=True,
        )
        db_session.add(card)
        db_session.flush()

        result = match_phone_to_entity("5555550301", db_session)
        assert result is None


class TestUnifiedMatcherUnknown:
    def test_unknown_number_returns_none(self, db_session):
        result = match_phone_to_entity("+19999990000", db_session)
        assert result is None

    def test_garbage_returns_none(self, db_session):
        result = match_phone_to_entity("not-a-phone", db_session)
        assert result is None

    def test_empty_returns_none(self, db_session):
        result = match_phone_to_entity("", db_session)
        assert result is None


class TestUnifiedMatcherAmbiguous:
    def test_two_distinct_contacts_ambiguous(self, db_session):
        from app.models import Company, CustomerSite, SiteContact

        co1 = Company(name="Corp Alpha", is_active=True)
        co2 = Company(name="Corp Beta", is_active=True)
        db_session.add_all([co1, co2])
        db_session.flush()
        site1 = CustomerSite(company_id=co1.id, site_name="HQ1", is_active=True)
        site2 = CustomerSite(company_id=co2.id, site_name="HQ2", is_active=True)
        db_session.add_all([site1, site2])
        db_session.flush()
        # Same phone number on two different contacts at different companies
        sc1 = SiteContact(customer_site_id=site1.id, full_name="Amy", phone="2025550100", is_active=True)
        sc2 = SiteContact(customer_site_id=site2.id, full_name="Ben", phone="2025550100", is_active=True)
        db_session.add_all([sc1, sc2])
        db_session.flush()

        result = match_phone_to_entity("202-555-0100", db_session)
        assert result is not None
        assert result["ambiguous"] is True
        assert len(result["candidates"]) >= 2
