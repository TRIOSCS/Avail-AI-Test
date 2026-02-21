"""
Tests for app/services/deep_enrichment_service.py

Covers:
- link_contact_to_entities: domain matching, alias matching, update existing,
  company matching, no-match, empty/invalid email, field creation
- deep_enrich_vendor: not found, skip recent, force bypass, no domain,
  company enrichment, email verification, contact discovery, specialty detection
- deep_enrich_company: not found, skip recent, domain extraction,
  clearbit enrichment, contact discovery, commit failure
- _apply_field_update: JSON brand_tags, company field, vendor_contact field,
  unknown entity type
- apply_queue_item: vendor routing, company routing, contact routing,
  missing entity IDs

Called by: pytest tests/test_deep_enrichment_service.py -v
"""

import os

os.environ["TESTING"] = "1"

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import (
    Company,
    CustomerSite,
    EnrichmentQueue,
    SiteContact,
    VendorCard,
    VendorContact,
)
from app.services.deep_enrichment_service import (
    _apply_field_update,
    apply_queue_item,
    deep_enrich_company,
    deep_enrich_vendor,
    link_contact_to_entities,
)


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def vendor_with_domain(db_session: Session) -> VendorCard:
    """Vendor card with a known domain for contact linking."""
    card = VendorCard(
        normalized_name="acme parts",
        display_name="Acme Parts",
        domain="acmeparts.com",
        domain_aliases=json.dumps(["acme-parts.com", "acme-legacy.com"]),
        emails=["sales@acmeparts.com"],
        sighting_count=10,
        website="https://acmeparts.com",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)
    return card


@pytest.fixture()
def company_with_domain(db_session: Session) -> Company:
    """Company with domain and a child site for contact linking."""
    co = Company(
        name="Widget Corp",
        domain="widgetcorp.com",
        website="https://widgetcorp.com",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(co)
    db_session.flush()

    site = CustomerSite(
        company_id=co.id,
        site_name="Widget HQ",
        contact_name="Admin",
        contact_email="admin@widgetcorp.com",
    )
    db_session.add(site)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def enrichable_vendor(db_session: Session) -> VendorCard:
    """Vendor card ready for deep enrichment (no recent enrichment timestamp)."""
    card = VendorCard(
        normalized_name="deep enrich vendor",
        display_name="Deep Enrich Vendor",
        domain="deepvendor.com",
        emails=["info@deepvendor.com"],
        sighting_count=5,
        website="https://deepvendor.com",
        deep_enrichment_at=None,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)
    return card


@pytest.fixture()
def enrichable_company(db_session: Session) -> Company:
    """Company ready for deep enrichment (no recent enrichment timestamp)."""
    co = Company(
        name="Deep Enrich Co",
        domain="deepco.com",
        website="https://www.deepco.com",
        is_active=True,
        deep_enrichment_at=None,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(co)
    db_session.flush()

    site = CustomerSite(
        company_id=co.id,
        site_name="DeepCo HQ",
        contact_name="Main Contact",
        contact_email="main@deepco.com",
    )
    db_session.add(site)
    db_session.commit()
    db_session.refresh(co)
    return co


# ── link_contact_to_entities Tests ────────────────────────────────────


class TestLinkContactToEntities:
    """Tests for matching sender email to VendorCard/Company entities."""

    def test_link_contact_domain_match(self, db_session, vendor_with_domain):
        """Email domain matching vendor card domain creates VendorContact."""
        link_contact_to_entities(
            db_session,
            "john.doe@acmeparts.com",
            {"full_name": "John Doe", "title": "Sales Rep", "confidence": 0.8},
        )
        db_session.commit()

        contact = (
            db_session.query(VendorContact)
            .filter(VendorContact.vendor_card_id == vendor_with_domain.id)
            .first()
        )
        assert contact is not None
        assert contact.email == "john.doe@acmeparts.com"
        assert contact.full_name == "John Doe"
        assert contact.title == "Sales Rep"
        assert contact.source == "email_signature"

    def test_link_contact_alias_match(self, db_session, vendor_with_domain):
        """Email domain matching domain_aliases links to vendor."""
        link_contact_to_entities(
            db_session,
            "jane@acme-legacy.com",
            {"full_name": "Jane Alias", "title": "Manager", "confidence": 0.7},
        )
        db_session.commit()

        contact = (
            db_session.query(VendorContact)
            .filter(
                VendorContact.vendor_card_id == vendor_with_domain.id,
                VendorContact.email == "jane@acme-legacy.com",
            )
            .first()
        )
        assert contact is not None
        assert contact.full_name == "Jane Alias"

    def test_link_contact_updates_existing(self, db_session, vendor_with_domain):
        """Existing VendorContact with same email increments interaction_count."""
        # Create existing contact
        existing = VendorContact(
            vendor_card_id=vendor_with_domain.id,
            full_name="Existing Person",
            email="existing@acmeparts.com",
            source="manual",
            confidence=80,
            interaction_count=3,
        )
        db_session.add(existing)
        db_session.commit()
        db_session.refresh(existing)

        link_contact_to_entities(
            db_session,
            "existing@acmeparts.com",
            {"full_name": "Existing Person", "title": "New Title", "confidence": 0.9},
        )
        db_session.commit()

        db_session.refresh(existing)
        assert existing.interaction_count == 4
        assert existing.title == "New Title"
        assert existing.last_seen_at is not None

    def test_link_contact_company_match(self, db_session, company_with_domain):
        """Email domain matching Company domain creates SiteContact."""
        link_contact_to_entities(
            db_session,
            "bob@widgetcorp.com",
            {"full_name": "Bob Company", "title": "Buyer", "confidence": 0.85},
        )
        db_session.commit()

        site = (
            db_session.query(CustomerSite)
            .filter(CustomerSite.company_id == company_with_domain.id)
            .first()
        )
        sc = (
            db_session.query(SiteContact)
            .filter(
                SiteContact.customer_site_id == site.id,
                SiteContact.email == "bob@widgetcorp.com",
            )
            .first()
        )
        assert sc is not None
        assert sc.full_name == "Bob Company"
        assert sc.title == "Buyer"

    def test_link_contact_no_match(self, db_session, vendor_with_domain):
        """Email domain matching nothing returns None with no records created."""
        link_contact_to_entities(
            db_session,
            "nobody@unknowndomain.com",
            {"full_name": "No Match", "confidence": 0.9},
        )
        db_session.commit()

        contacts = db_session.query(VendorContact).filter(
            VendorContact.email == "nobody@unknowndomain.com"
        ).all()
        assert len(contacts) == 0

    def test_link_contact_empty_email(self, db_session, vendor_with_domain):
        """Empty string email returns None early without error."""
        result = link_contact_to_entities(db_session, "", {"full_name": "Ghost"})
        assert result is None

    def test_link_contact_no_at_sign(self, db_session, vendor_with_domain):
        """Email without @ returns None early without error."""
        result = link_contact_to_entities(db_session, "not-an-email", {"full_name": "Bad"})
        assert result is None

    def test_link_contact_creates_vendor_contact_fields(self, db_session, vendor_with_domain):
        """Verify created VendorContact has correct fields from signature_data."""
        link_contact_to_entities(
            db_session,
            "detailed@acmeparts.com",
            {
                "full_name": "Detailed Person",
                "title": "VP of Sales",
                "phone": "+1-555-9999",
                "confidence": 0.95,
            },
        )
        db_session.commit()

        contact = (
            db_session.query(VendorContact)
            .filter(VendorContact.email == "detailed@acmeparts.com")
            .first()
        )
        assert contact is not None
        assert contact.full_name == "Detailed Person"
        assert contact.title == "VP of Sales"
        assert contact.phone == "+1-555-9999"
        assert contact.source == "email_signature"
        assert contact.confidence == 95  # 0.95 * 100
        assert contact.vendor_card_id == vendor_with_domain.id


# ── deep_enrich_vendor Tests ──────────────────────────────────────────


class TestDeepEnrichVendor:
    """Tests for the async deep_enrich_vendor orchestrator."""

    @pytest.mark.asyncio
    async def test_deep_enrich_vendor_not_found(self, db_session):
        """Invalid vendor ID returns error dict."""
        result = await deep_enrich_vendor(99999, db_session)
        assert result["status"] == "not_found"

    @pytest.mark.asyncio
    async def test_deep_enrich_vendor_recently_enriched_skip(self, db_session, enrichable_vendor):
        """Vendor with recent deep_enrichment_at is skipped."""
        enrichable_vendor.deep_enrichment_at = datetime.now(timezone.utc) - timedelta(days=1)
        db_session.commit()

        result = await deep_enrich_vendor(enrichable_vendor.id, db_session)
        assert result["status"] == "skipped"
        assert result["reason"] == "recently_enriched"

    @pytest.mark.asyncio
    async def test_deep_enrich_vendor_force_bypass(self, db_session, enrichable_vendor):
        """Recently enriched but force=True proceeds with enrichment."""
        enrichable_vendor.deep_enrichment_at = datetime.now(timezone.utc) - timedelta(days=1)
        db_session.commit()

        with (
            patch(
                "app.enrichment_service.enrich_entity",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.connectors.hunter_client.verify_email",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.enrichment_service.find_suggested_contacts",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "app.services.specialty_detector.analyze_vendor_specialties",
                return_value={},
            ),
            patch(
                "app.services.vendor_analysis_service._analyze_vendor_materials",
                new_callable=AsyncMock,
            ),
        ):
            result = await deep_enrich_vendor(enrichable_vendor.id, db_session, force=True)
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_deep_enrich_vendor_no_domain(self, db_session):
        """Vendor without domain gets limited enrichment (skips domain-dependent steps)."""
        card = VendorCard(
            normalized_name="no domain vendor",
            display_name="No Domain Vendor",
            domain=None,
            emails=[],
            sighting_count=1,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()
        db_session.refresh(card)

        with (
            patch(
                "app.connectors.hunter_client.verify_email",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.services.specialty_detector.analyze_vendor_specialties",
                return_value={},
            ),
            patch(
                "app.services.vendor_analysis_service._analyze_vendor_materials",
                new_callable=AsyncMock,
            ),
        ):
            result = await deep_enrich_vendor(card.id, db_session)

        assert result["status"] == "completed"
        # Enrichment should complete without company enrichment or contact discovery
        # since there is no domain
        assert isinstance(result["enriched_fields"], list)

    @pytest.mark.asyncio
    async def test_deep_enrich_vendor_company_enrichment(self, db_session, enrichable_vendor):
        """Mock enrich_entity routes company fields via confidence system."""
        mock_data = {
            "industry": "Semiconductors",
            "employee_size": "500-1000",
            "hq_city": "Dallas",
            "source": "clearbit",
        }
        with (
            patch(
                "app.enrichment_service.enrich_entity",
                new_callable=AsyncMock,
                return_value=mock_data,
            ),
            patch(
                "app.connectors.hunter_client.verify_email",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.enrichment_service.find_suggested_contacts",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "app.services.specialty_detector.analyze_vendor_specialties",
                return_value={},
            ),
            patch(
                "app.services.vendor_analysis_service._analyze_vendor_materials",
                new_callable=AsyncMock,
            ),
        ):
            result = await deep_enrich_vendor(enrichable_vendor.id, db_session)

        assert result["status"] == "completed"
        # Confidence is 0.85 which is >= auto_apply_threshold (0.8), so fields should be auto-applied
        db_session.refresh(enrichable_vendor)
        assert enrichable_vendor.industry == "Semiconductors"
        assert "industry" in result["enriched_fields"]

    @pytest.mark.asyncio
    async def test_deep_enrich_vendor_email_verification(self, db_session, enrichable_vendor):
        """Mock verify_email marks contacts as verified."""
        # Create a contact with an unverified email
        contact = VendorContact(
            vendor_card_id=enrichable_vendor.id,
            full_name="Verify Me",
            email="verify@deepvendor.com",
            source="manual",
            is_verified=False,
            confidence=50,
        )
        db_session.add(contact)
        db_session.commit()

        mock_verify_result = {"status": "valid", "score": 90}
        with (
            patch(
                "app.enrichment_service.enrich_entity",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.connectors.hunter_client.verify_email",
                new_callable=AsyncMock,
                return_value=mock_verify_result,
            ),
            patch(
                "app.enrichment_service.find_suggested_contacts",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "app.services.specialty_detector.analyze_vendor_specialties",
                return_value={},
            ),
            patch(
                "app.services.vendor_analysis_service._analyze_vendor_materials",
                new_callable=AsyncMock,
            ),
        ):
            result = await deep_enrich_vendor(enrichable_vendor.id, db_session)

        assert result["status"] == "completed"
        db_session.refresh(contact)
        assert contact.is_verified is True
        assert any("verified:" in f for f in result["enriched_fields"])

    @pytest.mark.asyncio
    async def test_deep_enrich_vendor_contact_discovery(self, db_session, enrichable_vendor):
        """Mock find_suggested_contacts creates routed contact entries."""
        mock_contacts = [
            {
                "full_name": "New Contact",
                "email": "new@deepvendor.com",
                "title": "Engineer",
                "source": "apollo",
            },
        ]
        with (
            patch(
                "app.enrichment_service.enrich_entity",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.connectors.hunter_client.verify_email",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.enrichment_service.find_suggested_contacts",
                new_callable=AsyncMock,
                return_value=mock_contacts,
            ),
            patch(
                "app.services.specialty_detector.analyze_vendor_specialties",
                return_value={},
            ),
            patch(
                "app.services.vendor_analysis_service._analyze_vendor_materials",
                new_callable=AsyncMock,
            ),
        ):
            result = await deep_enrich_vendor(enrichable_vendor.id, db_session)

        assert result["status"] == "completed"
        # Check that an enrichment queue entry was created for the new contact
        eq = (
            db_session.query(EnrichmentQueue)
            .filter(
                EnrichmentQueue.vendor_card_id == enrichable_vendor.id,
                EnrichmentQueue.enrichment_type == "contact_info",
            )
            .first()
        )
        assert eq is not None
        assert "new@deepvendor.com" in eq.field_name

    @pytest.mark.asyncio
    async def test_deep_enrich_vendor_specialty_detection(self, db_session, enrichable_vendor):
        """Mock analyze_vendor_specialties routes brand_tags via confidence system."""
        mock_specialties = {
            "brand_tags": ["Intel", "AMD"],
            "commodity_tags": ["processors", "memory"],
            "confidence": 0.9,
        }
        with (
            patch(
                "app.enrichment_service.enrich_entity",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.connectors.hunter_client.verify_email",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.enrichment_service.find_suggested_contacts",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "app.services.specialty_detector.analyze_vendor_specialties",
                return_value=mock_specialties,
            ),
            patch(
                "app.services.vendor_analysis_service._analyze_vendor_materials",
                new_callable=AsyncMock,
            ),
        ):
            result = await deep_enrich_vendor(enrichable_vendor.id, db_session)

        assert result["status"] == "completed"
        # Brand tags should have been routed (confidence 0.9 >= 0.8 auto_apply)
        eq_brands = (
            db_session.query(EnrichmentQueue)
            .filter(
                EnrichmentQueue.vendor_card_id == enrichable_vendor.id,
                EnrichmentQueue.field_name == "brand_tags",
            )
            .first()
        )
        assert eq_brands is not None
        assert eq_brands.source == "specialty_analysis"

        # Commodity tags should also have been routed
        eq_commodities = (
            db_session.query(EnrichmentQueue)
            .filter(
                EnrichmentQueue.vendor_card_id == enrichable_vendor.id,
                EnrichmentQueue.field_name == "commodity_tags",
            )
            .first()
        )
        assert eq_commodities is not None

        # specialty_confidence should be updated on the card
        db_session.refresh(enrichable_vendor)
        assert enrichable_vendor.specialty_confidence == 0.9


# ── deep_enrich_company Tests ─────────────────────────────────────────


class TestDeepEnrichCompany:
    """Tests for the async deep_enrich_company orchestrator."""

    @pytest.mark.asyncio
    async def test_deep_enrich_company_not_found(self, db_session):
        """Invalid company ID returns error dict."""
        result = await deep_enrich_company(99999, db_session)
        assert result["status"] == "not_found"

    @pytest.mark.asyncio
    async def test_deep_enrich_company_skip_recent(self, db_session, enrichable_company):
        """Company with recent deep_enrichment_at is skipped."""
        enrichable_company.deep_enrichment_at = datetime.now(timezone.utc) - timedelta(days=1)
        db_session.commit()

        result = await deep_enrich_company(enrichable_company.id, db_session)
        assert result["status"] == "skipped"
        assert result["reason"] == "recently_enriched"

    @pytest.mark.asyncio
    async def test_deep_enrich_company_domain_extraction(self, db_session):
        """Company with website but no domain extracts domain from website URL."""
        co = Company(
            name="No Domain Co",
            domain=None,
            website="https://www.nodomain-co.com/about",
            is_active=True,
            deep_enrichment_at=None,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(co)
        db_session.commit()
        db_session.refresh(co)

        mock_data = {
            "industry": "Manufacturing",
            "source": "enrichment",
        }
        with (
            patch(
                "app.enrichment_service.enrich_entity",
                new_callable=AsyncMock,
                return_value=mock_data,
            ),
            patch(
                "app.connectors.clearbit_client.enrich_company",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.enrichment_service.find_suggested_contacts",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = await deep_enrich_company(co.id, db_session)

        assert result["status"] == "completed"
        # The domain should have been extracted from the website and used for enrichment
        db_session.refresh(co)
        assert co.deep_enrichment_at is not None

    @pytest.mark.asyncio
    async def test_deep_enrich_company_clearbit(self, db_session, enrichable_company):
        """Mock clearbit enrichment applies firmographic fields."""
        mock_clearbit = {
            "industry": "Technology",
            "employee_size": "201-500",
            "hq_city": "San Francisco",
            "hq_state": "CA",
            "hq_country": "US",
        }
        with (
            patch(
                "app.enrichment_service.enrich_entity",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.connectors.clearbit_client.enrich_company",
                new_callable=AsyncMock,
                return_value=mock_clearbit,
            ),
            patch(
                "app.enrichment_service.find_suggested_contacts",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = await deep_enrich_company(enrichable_company.id, db_session)

        assert result["status"] == "completed"
        # Clearbit data should have been routed (confidence 0.8 = auto_apply threshold)
        eq = (
            db_session.query(EnrichmentQueue)
            .filter(
                EnrichmentQueue.company_id == enrichable_company.id,
                EnrichmentQueue.source == "clearbit",
            )
            .all()
        )
        assert len(eq) > 0

    @pytest.mark.asyncio
    async def test_deep_enrich_company_contact_discovery(self, db_session, enrichable_company):
        """Mock contact finder creates SiteContacts for the company."""
        mock_contacts = [
            {
                "full_name": "New Company Contact",
                "email": "newcontact@deepco.com",
                "title": "Procurement",
                "phone": "+1-555-0001",
            },
        ]
        with (
            patch(
                "app.enrichment_service.enrich_entity",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.connectors.clearbit_client.enrich_company",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.enrichment_service.find_suggested_contacts",
                new_callable=AsyncMock,
                return_value=mock_contacts,
            ),
        ):
            result = await deep_enrich_company(enrichable_company.id, db_session)

        assert result["status"] == "completed"
        assert any("contact:newcontact@deepco.com" in f for f in result["enriched_fields"])

        # Verify the SiteContact was created
        site = (
            db_session.query(CustomerSite)
            .filter(CustomerSite.company_id == enrichable_company.id)
            .first()
        )
        sc = (
            db_session.query(SiteContact)
            .filter(
                SiteContact.customer_site_id == site.id,
                SiteContact.email == "newcontact@deepco.com",
            )
            .first()
        )
        assert sc is not None
        assert sc.full_name == "New Company Contact"

    @pytest.mark.asyncio
    async def test_deep_enrich_company_commit_failure(self, db_session, enrichable_company):
        """Mock DB commit failure results in graceful error handling."""
        with (
            patch(
                "app.enrichment_service.enrich_entity",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.connectors.clearbit_client.enrich_company",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.enrichment_service.find_suggested_contacts",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch.object(db_session, "commit", side_effect=Exception("DB write error")),
            patch.object(db_session, "rollback"),
        ):
            result = await deep_enrich_company(enrichable_company.id, db_session)

        # The function should still return a result dict even when commit fails
        assert result["status"] == "completed"


# ── _apply_field_update Tests ─────────────────────────────────────────


class TestApplyFieldUpdate:
    """Tests for _apply_field_update helper."""

    def test_apply_field_update_json_brand_tags(self, db_session, enrichable_vendor):
        """field_name=brand_tags on vendor_card sets JSON array."""
        _apply_field_update(
            db_session,
            "vendor_card",
            enrichable_vendor.id,
            "brand_tags",
            ["Intel", "AMD", "Nvidia"],
        )
        db_session.commit()
        db_session.refresh(enrichable_vendor)
        assert enrichable_vendor.brand_tags == ["Intel", "AMD", "Nvidia"]

    def test_apply_field_update_brand_tags_from_json_string(self, db_session, enrichable_vendor):
        """brand_tags passed as JSON string are parsed and set."""
        _apply_field_update(
            db_session,
            "vendor_card",
            enrichable_vendor.id,
            "brand_tags",
            '["Texas Instruments", "NXP"]',
        )
        db_session.commit()
        db_session.refresh(enrichable_vendor)
        assert enrichable_vendor.brand_tags == ["Texas Instruments", "NXP"]

    def test_apply_field_update_company_field(self, db_session, enrichable_company):
        """entity_type=company, field_name=industry sets field on Company."""
        _apply_field_update(
            db_session,
            "company",
            enrichable_company.id,
            "industry",
            "Automotive Electronics",
        )
        db_session.commit()
        db_session.refresh(enrichable_company)
        assert enrichable_company.industry == "Automotive Electronics"

    def test_apply_field_update_vendor_contact_field(self, db_session, enrichable_vendor):
        """entity_type=vendor_contact sets field on VendorContact."""
        contact = VendorContact(
            vendor_card_id=enrichable_vendor.id,
            full_name="Test Contact",
            email="test@deepvendor.com",
            source="manual",
            confidence=50,
        )
        db_session.add(contact)
        db_session.commit()
        db_session.refresh(contact)

        _apply_field_update(
            db_session,
            "vendor_contact",
            contact.id,
            "title",
            "Senior Engineer",
        )
        db_session.commit()
        db_session.refresh(contact)
        assert contact.title == "Senior Engineer"

    def test_apply_field_update_unknown_entity(self, db_session):
        """entity_type=unknown does nothing (no error raised)."""
        # Should not raise any exception
        _apply_field_update(db_session, "unknown_type", 1, "field", "value")

    def test_apply_field_update_nonexistent_entity(self, db_session):
        """Updating a nonexistent entity ID does not crash."""
        _apply_field_update(db_session, "vendor_card", 99999, "industry", "Test")
        # No exception means success


# ── apply_queue_item Tests ────────────────────────────────────────────


class TestApplyQueueItem:
    """Tests for the apply_queue_item function."""

    def test_apply_queue_item_vendor(self, db_session, enrichable_vendor, test_user):
        """Queue item with vendor_card_id routes to vendor update."""
        item = EnrichmentQueue(
            vendor_card_id=enrichable_vendor.id,
            enrichment_type="company_info",
            field_name="industry",
            current_value=None,
            proposed_value="Semiconductors",
            confidence=0.75,
            source="clearbit",
            status="pending",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.commit()

        ok = apply_queue_item(db_session, item, user_id=test_user.id)
        db_session.commit()

        assert ok is True
        assert item.status == "approved"
        assert item.reviewed_by_id == test_user.id
        assert item.reviewed_at is not None

        db_session.refresh(enrichable_vendor)
        assert enrichable_vendor.industry == "Semiconductors"

    def test_apply_queue_item_company(self, db_session, enrichable_company, test_user):
        """Queue item with company_id routes to company update."""
        item = EnrichmentQueue(
            company_id=enrichable_company.id,
            enrichment_type="company_info",
            field_name="employee_size",
            current_value=None,
            proposed_value="1001-5000",
            confidence=0.7,
            source="clearbit",
            status="pending",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.commit()

        ok = apply_queue_item(db_session, item, user_id=test_user.id)
        db_session.commit()

        assert ok is True
        assert item.status == "approved"

        db_session.refresh(enrichable_company)
        assert enrichable_company.employee_size == "1001-5000"

    def test_apply_queue_item_contact(self, db_session, enrichable_vendor, test_user):
        """Queue item with vendor_contact_id routes to contact update."""
        contact = VendorContact(
            vendor_card_id=enrichable_vendor.id,
            full_name="Queue Contact",
            email="queue@deepvendor.com",
            source="manual",
            confidence=50,
        )
        db_session.add(contact)
        db_session.commit()
        db_session.refresh(contact)

        item = EnrichmentQueue(
            vendor_contact_id=contact.id,
            enrichment_type="contact_info",
            field_name="title",
            current_value=None,
            proposed_value="Director of Sales",
            confidence=0.8,
            source="apollo",
            status="pending",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.commit()

        ok = apply_queue_item(db_session, item, user_id=test_user.id)
        db_session.commit()

        assert ok is True
        assert item.status == "approved"

        db_session.refresh(contact)
        assert contact.title == "Director of Sales"

    def test_apply_queue_item_missing_entity(self, db_session, test_user):
        """Queue item with no entity IDs still marks as approved but does not update anything."""
        item = EnrichmentQueue(
            vendor_card_id=None,
            company_id=None,
            vendor_contact_id=None,
            enrichment_type="company_info",
            field_name="industry",
            current_value=None,
            proposed_value="Unknown",
            confidence=0.5,
            source="test",
            status="pending",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.commit()

        ok = apply_queue_item(db_session, item, user_id=test_user.id)
        # The function marks as approved even without entity, since the status update is unconditional
        assert ok is True
        assert item.status == "approved"

    def test_apply_queue_item_already_approved_returns_false(self, db_session, enrichable_vendor):
        """Cannot apply an already-approved item."""
        item = EnrichmentQueue(
            vendor_card_id=enrichable_vendor.id,
            enrichment_type="company_info",
            field_name="industry",
            current_value=None,
            proposed_value="Test",
            confidence=0.7,
            source="test",
            status="approved",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.commit()

        ok = apply_queue_item(db_session, item)
        assert ok is False

    def test_apply_queue_item_low_confidence_can_be_applied(self, db_session, enrichable_vendor, test_user):
        """Low-confidence items can still be manually applied."""
        item = EnrichmentQueue(
            vendor_card_id=enrichable_vendor.id,
            enrichment_type="company_info",
            field_name="hq_city",
            current_value=None,
            proposed_value="Austin",
            confidence=0.3,
            source="email_signature",
            status="low_confidence",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.commit()

        ok = apply_queue_item(db_session, item, user_id=test_user.id)
        db_session.commit()

        assert ok is True
        assert item.status == "approved"
        db_session.refresh(enrichable_vendor)
        assert enrichable_vendor.hq_city == "Austin"
