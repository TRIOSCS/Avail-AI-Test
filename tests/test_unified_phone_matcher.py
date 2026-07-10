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

    def test_priority5_skipped_when_higher_priority_matches(self, db_session, monkeypatch):
        """Priority-5 vendor-card scan must NOT run when a priority 1-4 match exists.

        A company (priority 2) and a vendor card (priority 5) share the same phone. The
        company must win AND the expensive vendor-card fallback must be short-circuited
        (never invoked), so the result is unambiguous.
        """
        from app.models import Company
        from app.models.vendors import VendorCard
        from app.services import activity_service

        co = Company(name="Shared Phone Co", phone="9165550700", is_active=True)
        db_session.add(co)
        card = VendorCard(
            normalized_name="shared-vendor",
            display_name="Shared Vendor",
            source="test",
            phones=["9165550700"],
            is_blacklisted=False,
        )
        db_session.add(card)
        db_session.flush()

        # Spy on the fallback helper: it must never be called on a 1-4 hit.
        calls = {"n": 0}
        real = activity_service._match_vendor_card_by_phone

        def _spy(db, e164):
            calls["n"] += 1
            return real(db, e164)

        monkeypatch.setattr(activity_service, "_match_vendor_card_by_phone", _spy)

        result = match_phone_to_entity("916-555-0700", db_session)

        assert result is not None
        assert result["type"] == "company"
        assert result["company_id"] == co.id
        assert result["ambiguous"] is False
        assert calls["n"] == 0, "priority-5 fallback ran despite a higher-priority match"

    def test_priority5_matches_via_db_containment(self, db_session):
        """Priority-5 links a phone to a vendor card when 1-4 do not match."""
        from app.models.vendors import VendorCard
        from app.services import activity_service

        card = VendorCard(
            normalized_name="containment-vendor",
            display_name="Containment Vendor",
            source="test",
            phones=["(800) 344-4539"],
            is_blacklisted=False,
        )
        db_session.add(card)
        db_session.flush()

        # e164 must live in the normalized_phones JSON list for the match.
        assert "+18003444539" in (card.normalized_phones or [])

        # The helper itself resolves the card via the in-DB membership test.
        # NOTE: on Postgres this compiles to a JSONB `@>` containment query;
        # SQLite (this test DB) has no JSONB/@>, so the helper degrades to a
        # Python membership test — the observable match result is identical.
        assert activity_service._match_vendor_card_by_phone(db_session, "+18003444539") is card

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


class TestUnifiedMatcherBatchedLookups:
    """P3.4 regression: batched CustomerSite/Company/VendorCard lookups (dict lookups
    keyed off the matched contact rows) must preserve match-priority order exactly even
    when several rows resolve to the SAME company/vendor within one call."""

    def test_two_site_contacts_same_company_same_site_resolve_together(self, db_session):
        """Two SiteContacts on the SAME site sharing a phone still collapse to one
        company candidate (dedup via `seen`, not affected by the batched dict
        lookup)."""
        from app.models import Company, CustomerSite, SiteContact

        co = Company(name="Batched Same Site Corp", is_active=True)
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="HQ", is_active=True)
        db_session.add(site)
        db_session.flush()
        c1 = SiteContact(customer_site_id=site.id, full_name="One", phone="2135550001", is_active=True)
        c2 = SiteContact(customer_site_id=site.id, full_name="Two", phone="2135550001", is_active=True)
        db_session.add_all([c1, c2])
        db_session.flush()

        result = match_phone_to_entity("213-555-0001", db_session)
        assert result is not None
        assert result["ambiguous"] is False
        assert result["company_id"] == co.id

    def test_multiple_customer_sites_different_companies_resolve_via_batched_dict(self, db_session):
        """Priority-3 CustomerSite match across two distinct companies must still report
        both as ambiguous candidates via the batched Company dict lookup."""
        from app.models import Company, CustomerSite

        co1 = Company(name="Batched Site Corp One", is_active=True)
        co2 = Company(name="Batched Site Corp Two", is_active=True)
        db_session.add_all([co1, co2])
        db_session.flush()
        site1 = CustomerSite(company_id=co1.id, site_name="Branch1", contact_phone="4085550050", is_active=True)
        site2 = CustomerSite(company_id=co2.id, site_name="Branch2", contact_phone="4085550050", is_active=True)
        db_session.add_all([site1, site2])
        db_session.flush()

        result = match_phone_to_entity("408-555-0050", db_session)
        assert result is not None
        assert result["ambiguous"] is True
        candidate_ids = {c["company_id"] for c in result["candidates"]}
        assert candidate_ids == {co1.id, co2.id}

    def test_multiple_vendor_contacts_same_card_resolve_via_batched_dict(self, db_session):
        """Two VendorContacts pointing at the SAME VendorCard sharing a phone still
        collapse to one vendor candidate via the batched VendorCard dict lookup."""
        from app.models.vendors import VendorCard, VendorContact

        card = VendorCard(normalized_name="batched-vc", display_name="Batched VC", source="test", is_blacklisted=False)
        db_session.add(card)
        db_session.flush()
        vc1 = VendorContact(vendor_card_id=card.id, source="test", phone="7145550099")
        vc2 = VendorContact(vendor_card_id=card.id, source="test", phone="7145550099")
        db_session.add_all([vc1, vc2])
        db_session.flush()

        result = match_phone_to_entity("(714) 555-0099", db_session)
        assert result is not None
        assert result["ambiguous"] is False
        assert result["vendor_card_id"] == card.id


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
