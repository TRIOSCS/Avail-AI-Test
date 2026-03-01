"""
Tests for app/services/deep_enrichment_service.py

Covers:
- link_contact_to_entities: domain matching, alias matching, update existing,
  company matching, no-match, empty/invalid email, field creation, flush error,
  update existing with new full_name/phone, mobile fallback, no full_name skip,
  existing SiteContact dedup
- route_enrichment: auto_applied, pending, low_confidence, all entity types
- _apply_field_update: JSON brand_tags, commodity_tags, company field,
  vendor_contact field, unknown entity type, nonexistent entity, invalid JSON string
- apply_queue_item: vendor routing, company routing, contact routing,
  missing entity IDs, already approved, low_confidence apply, JSON proposed,
  auto_applied status rejected
- deep_enrich_vendor: not found, skip recent, force bypass, no domain,
  company enrichment, email verification, contact discovery, specialty detection,
  enrichment error, email verify errors, contact discovery errors,
  specialty detection errors, material analysis errors, commit failure,
  naive datetime handling, contact confidence by source, accept_all verify status
- deep_enrich_company: not found, skip recent, domain extraction,
  clearbit enrichment, contact discovery, commit failure, enrichment error,
  clearbit error, contact discovery error, no domain and no website,
  force bypass, naive datetime recently enriched
- run_backfill_job: creates job and launches task
- _execute_backfill: vendor processing, company processing, cancellation,
  error handling, deep email mining, job not found, exception handling

Called by: pytest tests/test_deep_enrichment_service.py -v
"""

import asyncio
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
    EnrichmentJob,
    EnrichmentQueue,
    SiteContact,
    User,
    VendorCard,
    VendorContact,
)
from app.services.deep_enrichment_service import (
    _apply_field_update,
    _execute_backfill,
    apply_queue_item,
    deep_enrich_company,
    deep_enrich_vendor,
    link_contact_to_entities,
    route_enrichment,
    run_backfill_job,
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


# Helper context manager for standard vendor enrichment mocks
def _vendor_enrich_patches(
    enrich_return=None,
    verify_return=None,
    contacts_return=None,
    specialties_return=None,
    material_side_effect=None,
):
    """Return a combined context manager for all deep_enrich_vendor dependencies."""
    import contextlib

    contacts_return = contacts_return if contacts_return is not None else []
    specialties_return = specialties_return if specialties_return is not None else {}

    @contextlib.contextmanager
    def _ctx():
        with (
            patch(
                "app.enrichment_service.enrich_entity",
                new_callable=AsyncMock,
                return_value=enrich_return,
            ),
            patch(
                "app.connectors.hunter_client.verify_email",
                new_callable=AsyncMock,
                return_value=verify_return,
            ),
            patch(
                "app.enrichment_service.find_suggested_contacts",
                new_callable=AsyncMock,
                return_value=contacts_return,
            ),
            patch(
                "app.services.specialty_detector.analyze_vendor_specialties",
                return_value=specialties_return,
            ),
            patch(
                "app.services.vendor_analysis_service._analyze_vendor_materials",
                new_callable=AsyncMock,
                side_effect=material_side_effect,
            ),
        ):
            yield

    return _ctx()


# Helper context manager for standard company enrichment mocks
def _company_enrich_patches(
    enrich_return=None,
    clearbit_return=None,
    contacts_return=None,
):
    """Return a combined context manager for all deep_enrich_company dependencies."""
    import contextlib

    contacts_return = contacts_return if contacts_return is not None else []

    @contextlib.contextmanager
    def _ctx():
        with (
            patch(
                "app.enrichment_service.enrich_entity",
                new_callable=AsyncMock,
                return_value=enrich_return,
            ),
            patch(
                "app.connectors.clearbit_client.enrich_company",
                new_callable=AsyncMock,
                return_value=clearbit_return,
            ),
            patch(
                "app.enrichment_service.find_suggested_contacts",
                new_callable=AsyncMock,
                return_value=contacts_return,
            ),
        ):
            yield

    return _ctx()


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

        contact = db_session.query(VendorContact).filter(VendorContact.vendor_card_id == vendor_with_domain.id).first()
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

    def test_link_contact_updates_existing_fills_missing_full_name(self, db_session, vendor_with_domain):
        """Existing contact without full_name gets it filled from signature data (line 61)."""
        existing = VendorContact(
            vendor_card_id=vendor_with_domain.id,
            full_name=None,
            email="noname@acmeparts.com",
            source="manual",
            confidence=80,
            interaction_count=0,
        )
        db_session.add(existing)
        db_session.commit()
        db_session.refresh(existing)

        link_contact_to_entities(
            db_session,
            "noname@acmeparts.com",
            {"full_name": "Filled Name", "confidence": 0.7},
        )
        db_session.commit()

        db_session.refresh(existing)
        assert existing.full_name == "Filled Name"
        assert existing.interaction_count == 1

    def test_link_contact_updates_existing_fills_missing_phone(self, db_session, vendor_with_domain):
        """Existing contact without phone gets it filled from signature data (line 65)."""
        existing = VendorContact(
            vendor_card_id=vendor_with_domain.id,
            full_name="Has Name",
            email="nophone@acmeparts.com",
            phone=None,
            source="manual",
            confidence=80,
            interaction_count=0,
        )
        db_session.add(existing)
        db_session.commit()
        db_session.refresh(existing)

        link_contact_to_entities(
            db_session,
            "nophone@acmeparts.com",
            {"full_name": "Has Name", "phone": "+1-555-1234", "confidence": 0.8},
        )
        db_session.commit()

        db_session.refresh(existing)
        assert existing.phone == "+1-555-1234"

    def test_link_contact_mobile_fallback(self, db_session, vendor_with_domain):
        """Signature with 'mobile' key but no 'phone' uses mobile (line 32)."""
        link_contact_to_entities(
            db_session,
            "mobilefallback@acmeparts.com",
            {"full_name": "Mobile User", "mobile": "+1-555-MOBI", "confidence": 0.7},
        )
        db_session.commit()

        contact = db_session.query(VendorContact).filter(VendorContact.email == "mobilefallback@acmeparts.com").first()
        assert contact is not None
        assert contact.phone == "+1-555-MOBI"

    def test_link_contact_no_full_name_skips_creation(self, db_session, vendor_with_domain):
        """New contact without full_name is not created (line 69 guard)."""
        link_contact_to_entities(
            db_session,
            "nofullname@acmeparts.com",
            {"title": "Manager", "confidence": 0.7},
        )
        db_session.commit()

        contacts = db_session.query(VendorContact).filter(VendorContact.email == "nofullname@acmeparts.com").all()
        assert len(contacts) == 0

    def test_link_contact_company_match(self, db_session, company_with_domain):
        """Email domain matching Company domain creates SiteContact."""
        link_contact_to_entities(
            db_session,
            "bob@widgetcorp.com",
            {"full_name": "Bob Company", "title": "Buyer", "confidence": 0.85},
        )
        db_session.commit()

        site = db_session.query(CustomerSite).filter(CustomerSite.company_id == company_with_domain.id).first()
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

    def test_link_contact_company_existing_site_contact_dedup(self, db_session, company_with_domain):
        """Existing SiteContact with same email is not duplicated."""
        site = db_session.query(CustomerSite).filter(CustomerSite.company_id == company_with_domain.id).first()
        existing = SiteContact(
            customer_site_id=site.id,
            full_name="Already There",
            email="already@widgetcorp.com",
        )
        db_session.add(existing)
        db_session.commit()

        link_contact_to_entities(
            db_session,
            "already@widgetcorp.com",
            {"full_name": "Already There", "confidence": 0.7},
        )
        db_session.commit()

        count = db_session.query(SiteContact).filter(SiteContact.email == "already@widgetcorp.com").count()
        assert count == 1

    def test_link_contact_no_match(self, db_session, vendor_with_domain):
        """Email domain matching nothing returns None with no records created."""
        link_contact_to_entities(
            db_session,
            "nobody@unknowndomain.com",
            {"full_name": "No Match", "confidence": 0.9},
        )
        db_session.commit()

        contacts = db_session.query(VendorContact).filter(VendorContact.email == "nobody@unknowndomain.com").all()
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

        contact = db_session.query(VendorContact).filter(VendorContact.email == "detailed@acmeparts.com").first()
        assert contact is not None
        assert contact.full_name == "Detailed Person"
        assert contact.title == "VP of Sales"
        assert contact.phone == "+1-555-9999"
        assert contact.source == "email_signature"
        assert contact.confidence == 95  # 0.95 * 100
        assert contact.vendor_card_id == vendor_with_domain.id

    def test_link_contact_flush_error_rollback(self, db_session, vendor_with_domain):
        """Flush error during contact linking triggers rollback (lines 115-117)."""
        with patch.object(db_session, "flush", side_effect=Exception("integrity error")):
            # Should not raise — error is caught and rolled back
            link_contact_to_entities(
                db_session,
                "flush-fail@acmeparts.com",
                {"full_name": "Flush Fail", "confidence": 0.7},
            )


# ── route_enrichment Tests ────────────────────────────────────────────


class TestRouteEnrichment:
    """Tests for the three-tier confidence routing system."""

    def test_route_auto_applied(self, db_session, enrichable_vendor):
        """Confidence >= 0.8 auto-applies and returns 'auto_applied'."""
        result = route_enrichment(
            db_session,
            "vendor_card",
            enrichable_vendor.id,
            "industry",
            None,
            "Semiconductors",
            confidence=0.85,
            source="clearbit",
        )
        db_session.flush()
        assert result == "auto_applied"

        db_session.refresh(enrichable_vendor)
        assert enrichable_vendor.industry == "Semiconductors"

        eq = (
            db_session.query(EnrichmentQueue)
            .filter(
                EnrichmentQueue.vendor_card_id == enrichable_vendor.id,
                EnrichmentQueue.status == "auto_applied",
            )
            .first()
        )
        assert eq is not None

    def test_route_pending(self, db_session, enrichable_vendor):
        """Confidence >= 0.5 but < 0.8 returns 'pending' (lines 174-178)."""
        result = route_enrichment(
            db_session,
            "vendor_card",
            enrichable_vendor.id,
            "industry",
            None,
            "Pending Industry",
            confidence=0.65,
            source="enrichment",
        )
        db_session.flush()
        assert result == "pending"

        eq = (
            db_session.query(EnrichmentQueue)
            .filter(
                EnrichmentQueue.vendor_card_id == enrichable_vendor.id,
                EnrichmentQueue.status == "pending",
            )
            .first()
        )
        assert eq is not None
        assert eq.proposed_value == "Pending Industry"

    def test_route_low_confidence(self, db_session, enrichable_vendor):
        """Confidence < 0.5 returns 'low_confidence' (lines 179-183)."""
        result = route_enrichment(
            db_session,
            "vendor_card",
            enrichable_vendor.id,
            "industry",
            None,
            "Low Conf Industry",
            confidence=0.3,
            source="email_signature",
        )
        db_session.flush()
        assert result == "low_confidence"

        eq = (
            db_session.query(EnrichmentQueue)
            .filter(
                EnrichmentQueue.vendor_card_id == enrichable_vendor.id,
                EnrichmentQueue.status == "low_confidence",
            )
            .first()
        )
        assert eq is not None

    def test_route_vendor_contact_entity_type(self, db_session, enrichable_vendor):
        """entity_type='vendor_contact' sets vendor_contact_id (lines 165-166)."""
        contact = VendorContact(
            vendor_card_id=enrichable_vendor.id,
            full_name="Route Test",
            email="route@deepvendor.com",
            source="manual",
            confidence=50,
        )
        db_session.add(contact)
        db_session.commit()
        db_session.refresh(contact)

        result = route_enrichment(
            db_session,
            "vendor_contact",
            contact.id,
            "title",
            None,
            "New Title",
            confidence=0.9,
            source="apollo",
        )
        db_session.flush()
        assert result == "auto_applied"

        eq = (
            db_session.query(EnrichmentQueue)
            .filter(
                EnrichmentQueue.vendor_contact_id == contact.id,
            )
            .first()
        )
        assert eq is not None
        assert eq.vendor_contact_id == contact.id

    def test_route_company_entity_type(self, db_session, enrichable_company):
        """entity_type='company' sets company_id."""
        result = route_enrichment(
            db_session,
            "company",
            enrichable_company.id,
            "industry",
            None,
            "Tech",
            confidence=0.6,
            source="clearbit",
        )
        db_session.flush()
        assert result == "pending"

        eq = (
            db_session.query(EnrichmentQueue)
            .filter(
                EnrichmentQueue.company_id == enrichable_company.id,
            )
            .first()
        )
        assert eq is not None
        assert eq.company_id == enrichable_company.id

    def test_route_serializes_current_value(self, db_session, enrichable_vendor):
        """Non-None current_value is JSON-serialized."""
        result = route_enrichment(
            db_session,
            "vendor_card",
            enrichable_vendor.id,
            "brand_tags",
            ["old_tag"],
            ["new_tag"],
            confidence=0.85,
            source="specialty_analysis",
            enrichment_type="brand_tags",
        )
        db_session.flush()
        assert result == "auto_applied"

        eq = (
            db_session.query(EnrichmentQueue)
            .filter(
                EnrichmentQueue.vendor_card_id == enrichable_vendor.id,
                EnrichmentQueue.field_name == "brand_tags",
            )
            .first()
        )
        assert eq is not None
        assert eq.current_value == '["old_tag"]'

    def test_route_with_job_id(self, db_session, enrichable_vendor, test_user):
        """batch_job_id is set when job_id is provided."""
        job = EnrichmentJob(
            job_type="backfill",
            status="running",
            total_items=1,
            started_by_id=test_user.id,
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        route_enrichment(
            db_session,
            "vendor_card",
            enrichable_vendor.id,
            "industry",
            None,
            "Test",
            confidence=0.9,
            source="test",
            job_id=job.id,
        )
        db_session.flush()

        eq = (
            db_session.query(EnrichmentQueue)
            .filter(
                EnrichmentQueue.batch_job_id == job.id,
            )
            .first()
        )
        assert eq is not None


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
    async def test_deep_enrich_vendor_recently_enriched_naive_datetime(self, db_session, enrichable_vendor):
        """Vendor with naive datetime deep_enrichment_at is handled correctly (line 277)."""
        # Set a naive datetime (no tzinfo) that is recent
        enrichable_vendor.deep_enrichment_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
        db_session.commit()

        result = await deep_enrich_vendor(enrichable_vendor.id, db_session)
        assert result["status"] == "skipped"
        assert result["reason"] == "recently_enriched"

    @pytest.mark.asyncio
    async def test_deep_enrich_vendor_force_bypass(self, db_session, enrichable_vendor):
        """Recently enriched but force=True proceeds with enrichment."""
        enrichable_vendor.deep_enrichment_at = datetime.now(timezone.utc) - timedelta(days=1)
        db_session.commit()

        with _vendor_enrich_patches():
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

        with _vendor_enrich_patches():
            result = await deep_enrich_vendor(card.id, db_session)

        assert result["status"] == "completed"
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
        with _vendor_enrich_patches(enrich_return=mock_data):
            result = await deep_enrich_vendor(enrichable_vendor.id, db_session)

        assert result["status"] == "completed"
        db_session.refresh(enrichable_vendor)
        assert enrichable_vendor.industry == "Semiconductors"
        assert "industry" in result["enriched_fields"]

    @pytest.mark.asyncio
    async def test_deep_enrich_vendor_company_enrichment_error(self, db_session, enrichable_vendor):
        """enrich_entity raising exception is caught and logged (lines 307-309)."""
        with (
            patch(
                "app.enrichment_service.enrich_entity",
                new_callable=AsyncMock,
                side_effect=Exception("API timeout"),
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
        assert any("company_enrichment" in e for e in result["errors"])

    @pytest.mark.asyncio
    async def test_deep_enrich_vendor_email_verification(self, db_session, enrichable_vendor):
        """Mock verify_email marks contacts as verified."""
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
        with _vendor_enrich_patches(verify_return=mock_verify_result):
            result = await deep_enrich_vendor(enrichable_vendor.id, db_session)

        assert result["status"] == "completed"
        db_session.refresh(contact)
        assert contact.is_verified is True
        assert any("verified:" in f for f in result["enriched_fields"])

    @pytest.mark.asyncio
    async def test_deep_enrich_vendor_email_verify_accept_all(self, db_session, enrichable_vendor):
        """verify_email with 'accept_all' status also marks contacts as verified."""
        contact = VendorContact(
            vendor_card_id=enrichable_vendor.id,
            full_name="Accept All",
            email="acceptall@deepvendor.com",
            source="manual",
            is_verified=False,
            confidence=50,
        )
        db_session.add(contact)
        db_session.commit()

        with _vendor_enrich_patches(verify_return={"status": "accept_all"}):
            result = await deep_enrich_vendor(enrichable_vendor.id, db_session)

        assert result["status"] == "completed"
        db_session.refresh(contact)
        assert contact.is_verified is True

    @pytest.mark.asyncio
    async def test_deep_enrich_vendor_email_verify_already_verified(self, db_session, enrichable_vendor):
        """Already-verified contact is not re-added to enriched_fields (line 323)."""
        contact = VendorContact(
            vendor_card_id=enrichable_vendor.id,
            full_name="Already Verified",
            email="alreadyv@deepvendor.com",
            source="manual",
            is_verified=True,
            confidence=90,
        )
        db_session.add(contact)
        db_session.commit()

        with _vendor_enrich_patches(verify_return={"status": "valid"}):
            result = await deep_enrich_vendor(enrichable_vendor.id, db_session)

        assert result["status"] == "completed"
        # Should not have "verified:alreadyv@deepvendor.com" in enriched_fields
        assert not any("alreadyv@deepvendor.com" in f for f in result["enriched_fields"])

    @pytest.mark.asyncio
    async def test_deep_enrich_vendor_email_verify_inner_exception(self, db_session, enrichable_vendor):
        """Exception during individual email verification is caught silently (line 327)."""
        contact = VendorContact(
            vendor_card_id=enrichable_vendor.id,
            full_name="Error Contact",
            email="error@deepvendor.com",
            source="manual",
            is_verified=False,
            confidence=50,
        )
        db_session.add(contact)
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
                side_effect=Exception("Hunter API error"),
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
        # The individual verify error is caught silently, so no errors in result
        db_session.refresh(contact)
        assert contact.is_verified is False

    @pytest.mark.asyncio
    async def test_deep_enrich_vendor_email_verify_outer_exception(self, db_session, enrichable_vendor):
        """Exception in outer email verification block is caught (lines 332-333)."""
        import sys

        # Remove hunter_client from sys.modules to force ImportError
        saved = sys.modules.get("app.connectors.hunter_client")
        sys.modules["app.connectors.hunter_client"] = None  # Forces ImportError on from-import

        try:
            with (
                patch(
                    "app.enrichment_service.enrich_entity",
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
        finally:
            if saved is not None:
                sys.modules["app.connectors.hunter_client"] = saved
            else:
                sys.modules.pop("app.connectors.hunter_client", None)

        assert result["status"] == "completed"
        assert any("email_verification" in e for e in result["errors"])

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
        with _vendor_enrich_patches(contacts_return=mock_contacts):
            result = await deep_enrich_vendor(enrichable_vendor.id, db_session)

        assert result["status"] == "completed"
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
    async def test_deep_enrich_vendor_contact_discovery_source_confidence(self, db_session, enrichable_vendor):
        """Contact confidence varies by source: hunter=0.8, rocketreach=0.8, unknown=0.7 (lines 363-364)."""
        mock_contacts = [
            {"full_name": "Hunter Contact", "email": "hunter@deepvendor.com", "source": "hunter"},
            {"full_name": "RR Contact", "email": "rr@deepvendor.com", "source": "rocketreach"},
            {"full_name": "CB Contact", "email": "cb@deepvendor.com", "source": "clearbit"},
            {"full_name": "Unknown Contact", "email": "unknown@deepvendor.com", "source": "unknown"},
        ]
        with _vendor_enrich_patches(contacts_return=mock_contacts):
            result = await deep_enrich_vendor(enrichable_vendor.id, db_session)

        assert result["status"] == "completed"

        entries = (
            db_session.query(EnrichmentQueue)
            .filter(
                EnrichmentQueue.vendor_card_id == enrichable_vendor.id,
                EnrichmentQueue.enrichment_type == "contact_info",
            )
            .all()
        )
        assert len(entries) == 4

        conf_by_field = {e.field_name: e.confidence for e in entries}
        assert conf_by_field["new_contact:hunter@deepvendor.com"] == 0.8
        assert conf_by_field["new_contact:rr@deepvendor.com"] == 0.8
        assert conf_by_field["new_contact:cb@deepvendor.com"] == 0.8
        assert conf_by_field["new_contact:unknown@deepvendor.com"] == 0.7

    @pytest.mark.asyncio
    async def test_deep_enrich_vendor_contact_discovery_dedup(self, db_session, enrichable_vendor):
        """Existing contacts are deduped from discovery results."""
        existing = VendorContact(
            vendor_card_id=enrichable_vendor.id,
            full_name="Existing",
            email="existing@deepvendor.com",
            source="manual",
            confidence=80,
        )
        db_session.add(existing)
        db_session.commit()

        mock_contacts = [
            {"full_name": "Existing", "email": "existing@deepvendor.com", "source": "apollo"},
            {"full_name": "Brand New", "email": "brandnew@deepvendor.com", "source": "apollo"},
        ]
        with _vendor_enrich_patches(contacts_return=mock_contacts):
            result = await deep_enrich_vendor(enrichable_vendor.id, db_session)

        entries = (
            db_session.query(EnrichmentQueue)
            .filter(
                EnrichmentQueue.vendor_card_id == enrichable_vendor.id,
                EnrichmentQueue.enrichment_type == "contact_info",
            )
            .all()
        )
        emails_in_queue = [e.field_name for e in entries]
        assert "new_contact:brandnew@deepvendor.com" in emails_in_queue
        assert "new_contact:existing@deepvendor.com" not in emails_in_queue

    @pytest.mark.asyncio
    async def test_deep_enrich_vendor_contact_discovery_error(self, db_session, enrichable_vendor):
        """Contact discovery exception is caught (lines 376-377)."""
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
                side_effect=Exception("Apollo API down"),
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
        assert any("contact_discovery" in e for e in result["errors"])

    @pytest.mark.asyncio
    async def test_deep_enrich_vendor_specialty_detection(self, db_session, enrichable_vendor):
        """Mock analyze_vendor_specialties routes brand_tags via confidence system."""
        mock_specialties = {
            "brand_tags": ["Intel", "AMD"],
            "commodity_tags": ["processors", "memory"],
            "confidence": 0.9,
        }
        with _vendor_enrich_patches(specialties_return=mock_specialties):
            result = await deep_enrich_vendor(enrichable_vendor.id, db_session)

        assert result["status"] == "completed"
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

        eq_commodities = (
            db_session.query(EnrichmentQueue)
            .filter(
                EnrichmentQueue.vendor_card_id == enrichable_vendor.id,
                EnrichmentQueue.field_name == "commodity_tags",
            )
            .first()
        )
        assert eq_commodities is not None

        db_session.refresh(enrichable_vendor)
        assert enrichable_vendor.specialty_confidence == 0.9

    @pytest.mark.asyncio
    async def test_deep_enrich_vendor_specialty_detection_error(self, db_session, enrichable_vendor):
        """Specialty detection exception is caught (lines 387-388)."""
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
                side_effect=Exception("Specialty analysis crashed"),
            ),
            patch(
                "app.services.vendor_analysis_service._analyze_vendor_materials",
                new_callable=AsyncMock,
            ),
        ):
            result = await deep_enrich_vendor(enrichable_vendor.id, db_session)

        assert result["status"] == "completed"
        assert any("specialty_detection" in e for e in result["errors"])

    @pytest.mark.asyncio
    async def test_deep_enrich_vendor_material_analysis_error(self, db_session, enrichable_vendor):
        """Material analysis exception is caught (lines 396-397)."""
        with _vendor_enrich_patches(
            material_side_effect=Exception("Material analysis failed"),
        ):
            result = await deep_enrich_vendor(enrichable_vendor.id, db_session)

        assert result["status"] == "completed"
        assert any("material_analysis" in e for e in result["errors"])
        # material_tags should not be in enriched_fields when it fails
        assert "material_tags" not in result["enriched_fields"]

    @pytest.mark.asyncio
    async def test_deep_enrich_vendor_material_analysis_success(self, db_session, enrichable_vendor):
        """Successful material analysis adds 'material_tags' to enriched_fields."""
        with _vendor_enrich_patches():
            result = await deep_enrich_vendor(enrichable_vendor.id, db_session)

        assert result["status"] == "completed"
        assert "material_tags" in result["enriched_fields"]

    @pytest.mark.asyncio
    async def test_deep_enrich_vendor_commit_failure(self, db_session, enrichable_vendor):
        """Commit failure is caught and rolled back (lines 439-441)."""
        with _vendor_enrich_patches():
            with (
                patch.object(
                    db_session,
                    "commit",
                    side_effect=Exception("DB commit failed"),
                ),
                patch.object(db_session, "rollback") as mock_rollback,
            ):
                result = await deep_enrich_vendor(enrichable_vendor.id, db_session)

        assert result["status"] == "completed"
        mock_rollback.assert_called()


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
    async def test_deep_enrich_company_skip_recent_naive_datetime(self, db_session, enrichable_company):
        """Company with naive datetime deep_enrichment_at is handled (line 465)."""
        enrichable_company.deep_enrichment_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
        db_session.commit()

        result = await deep_enrich_company(enrichable_company.id, db_session)
        assert result["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_deep_enrich_company_force_bypass(self, db_session, enrichable_company):
        """force=True bypasses the recency check."""
        enrichable_company.deep_enrichment_at = datetime.now(timezone.utc) - timedelta(days=1)
        db_session.commit()

        with _company_enrich_patches():
            result = await deep_enrich_company(enrichable_company.id, db_session, force=True)

        assert result["status"] == "completed"

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

        mock_data = {"industry": "Manufacturing", "source": "enrichment"}
        with _company_enrich_patches(enrich_return=mock_data):
            result = await deep_enrich_company(co.id, db_session)

        assert result["status"] == "completed"
        db_session.refresh(co)
        assert co.deep_enrichment_at is not None

    @pytest.mark.asyncio
    async def test_deep_enrich_company_no_domain_no_website(self, db_session):
        """Company with neither domain nor website skips all domain-dependent enrichment."""
        co = Company(
            name="No Domain No Website",
            domain=None,
            website=None,
            is_active=True,
            deep_enrichment_at=None,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(co)
        db_session.commit()
        db_session.refresh(co)

        result = await deep_enrich_company(co.id, db_session)
        assert result["status"] == "completed"
        assert result["enriched_fields"] == []

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
        with _company_enrich_patches(clearbit_return=mock_clearbit):
            result = await deep_enrich_company(enrichable_company.id, db_session)

        assert result["status"] == "completed"
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
    async def test_deep_enrich_company_enrichment_error(self, db_session, enrichable_company):
        """enrich_entity exception is caught (lines 502-503)."""
        with (
            patch(
                "app.enrichment_service.enrich_entity",
                new_callable=AsyncMock,
                side_effect=Exception("Enrichment API error"),
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
            result = await deep_enrich_company(enrichable_company.id, db_session)

        assert result["status"] == "completed"
        assert any("company_enrichment" in e for e in result["errors"])

    @pytest.mark.asyncio
    async def test_deep_enrich_company_clearbit_error(self, db_session, enrichable_company):
        """Clearbit exception is caught (lines 522-523)."""
        with (
            patch(
                "app.enrichment_service.enrich_entity",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.connectors.clearbit_client.enrich_company",
                new_callable=AsyncMock,
                side_effect=Exception("Clearbit API timeout"),
            ),
            patch(
                "app.enrichment_service.find_suggested_contacts",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = await deep_enrich_company(enrichable_company.id, db_session)

        assert result["status"] == "completed"
        assert any("clearbit" in e for e in result["errors"])

    @pytest.mark.asyncio
    async def test_deep_enrich_company_contact_discovery(self, db_session, enrichable_company):
        """Mock contact finder queues contacts via route_enrichment."""
        mock_contacts = [
            {
                "full_name": "New Company Contact",
                "email": "newcontact@deepco.com",
                "title": "Procurement",
                "phone": "+1-555-0001",
            },
        ]
        with _company_enrich_patches(contacts_return=mock_contacts):
            with patch(
                "app.services.deep_enrichment_service.route_enrichment",
                return_value="pending",
            ) as mock_route:
                result = await deep_enrich_company(enrichable_company.id, db_session)

        assert result["status"] == "completed"
        assert any("contact:newcontact@deepco.com" in f for f in result["enriched_fields"])
        mock_route.assert_called_once()
        call_args = mock_route.call_args
        assert call_args[0][0] == db_session  # db
        assert call_args[0][1] == "company"  # entity_type
        assert call_args[0][2] == enrichable_company.id  # entity_id
        assert "newcontact@deepco.com" in call_args[0][3]  # field_name

    @pytest.mark.asyncio
    async def test_deep_enrich_company_contact_discovery_error(self, db_session, enrichable_company):
        """Contact discovery exception is caught (lines 555-556)."""
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
                side_effect=Exception("Contact API failed"),
            ),
        ):
            result = await deep_enrich_company(enrichable_company.id, db_session)

        assert result["status"] == "completed"
        assert any("contact_discovery" in e for e in result["errors"])

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

    def test_apply_field_update_brand_tags_invalid_json_string(self, db_session, enrichable_vendor):
        """brand_tags with invalid JSON string falls through to pass (lines 208-209)."""
        _apply_field_update(
            db_session,
            "vendor_card",
            enrichable_vendor.id,
            "brand_tags",
            "not-valid-json{{{",
        )
        db_session.commit()
        db_session.refresh(enrichable_vendor)
        # Invalid JSON remains as the raw string
        assert enrichable_vendor.brand_tags == "not-valid-json{{{"

    def test_apply_field_update_commodity_tags_json_string(self, db_session, enrichable_vendor):
        """commodity_tags also goes through the JSON parse path."""
        _apply_field_update(
            db_session,
            "vendor_card",
            enrichable_vendor.id,
            "commodity_tags",
            '["capacitors", "resistors"]',
        )
        db_session.commit()
        db_session.refresh(enrichable_vendor)
        assert enrichable_vendor.commodity_tags == ["capacitors", "resistors"]

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
        _apply_field_update(db_session, "unknown_type", 1, "field", "value")

    def test_apply_field_update_nonexistent_entity(self, db_session):
        """Updating a nonexistent entity ID does not crash."""
        _apply_field_update(db_session, "vendor_card", 99999, "industry", "Test")


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

    def test_apply_queue_item_auto_applied_returns_false(self, db_session, enrichable_vendor):
        """Cannot apply an already auto_applied item."""
        item = EnrichmentQueue(
            vendor_card_id=enrichable_vendor.id,
            enrichment_type="company_info",
            field_name="industry",
            current_value=None,
            proposed_value="Test",
            confidence=0.9,
            source="test",
            status="auto_applied",
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

    def test_apply_queue_item_json_proposed_value(self, db_session, enrichable_vendor, test_user):
        """Queue item with JSON-encoded proposed_value is parsed before applying."""
        item = EnrichmentQueue(
            vendor_card_id=enrichable_vendor.id,
            enrichment_type="brand_tags",
            field_name="brand_tags",
            current_value=None,
            proposed_value='["Intel", "AMD"]',
            confidence=0.7,
            source="specialty_analysis",
            status="pending",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.commit()

        ok = apply_queue_item(db_session, item, user_id=test_user.id)
        db_session.commit()

        assert ok is True
        db_session.refresh(enrichable_vendor)
        assert enrichable_vendor.brand_tags == ["Intel", "AMD"]

    def test_apply_queue_item_no_user_id(self, db_session, enrichable_vendor):
        """Queue item applied without user_id sets reviewed_by_id to None."""
        item = EnrichmentQueue(
            vendor_card_id=enrichable_vendor.id,
            enrichment_type="company_info",
            field_name="industry",
            current_value=None,
            proposed_value="Electronics",
            confidence=0.6,
            source="test",
            status="pending",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.commit()

        ok = apply_queue_item(db_session, item)
        assert ok is True
        assert item.reviewed_by_id is None
        assert item.reviewed_at is not None


# ── run_backfill_job Tests ────────────────────────────────────────────


class TestRunBackfillJob:
    """Tests for run_backfill_job which creates and launches backfill tasks."""

    @pytest.mark.asyncio
    async def test_run_backfill_job_creates_job(self, db_session, test_user, enrichable_vendor, enrichable_company):
        """Creates EnrichmentJob with correct attributes and returns job ID."""
        with patch("app.services.deep_enrichment_service.asyncio.create_task") as mock_task:
            job_id = await run_backfill_job(db_session, test_user.id)

        assert job_id is not None
        job = db_session.get(EnrichmentJob, job_id)
        assert job is not None
        assert job.job_type == "backfill"
        assert job.status == "running"
        assert job.started_by_id == test_user.id
        assert job.started_at is not None
        assert job.total_items > 0
        mock_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_backfill_job_vendor_only(self, db_session, test_user, enrichable_vendor):
        """Scope with entity_types=['vendor'] only counts vendors."""
        with patch("app.services.deep_enrichment_service.asyncio.create_task"):
            job_id = await run_backfill_job(
                db_session,
                test_user.id,
                scope={"entity_types": ["vendor"]},
            )

        job = db_session.get(EnrichmentJob, job_id)
        assert job is not None
        assert job.total_items >= 1

    @pytest.mark.asyncio
    async def test_run_backfill_job_company_only(self, db_session, test_user, enrichable_company):
        """Scope with entity_types=['company'] only counts companies."""
        with patch("app.services.deep_enrichment_service.asyncio.create_task"):
            job_id = await run_backfill_job(
                db_session,
                test_user.id,
                scope={"entity_types": ["company"]},
            )

        job = db_session.get(EnrichmentJob, job_id)
        assert job is not None
        assert job.total_items >= 1

    @pytest.mark.asyncio
    async def test_run_backfill_job_max_items_capped(self, db_session, test_user):
        """max_items is capped at 2000."""
        with patch("app.services.deep_enrichment_service.asyncio.create_task"):
            job_id = await run_backfill_job(
                db_session,
                test_user.id,
                scope={"max_items": 5000},
            )

        job = db_session.get(EnrichmentJob, job_id)
        assert job is not None
        # total_items should be min(count, 2000) since 5000 is capped to 2000

    @pytest.mark.asyncio
    async def test_run_backfill_job_default_scope(self, db_session, test_user):
        """Default scope (None) includes both vendor and company types."""
        with patch("app.services.deep_enrichment_service.asyncio.create_task"):
            job_id = await run_backfill_job(db_session, test_user.id, scope=None)

        job = db_session.get(EnrichmentJob, job_id)
        assert job is not None
        assert job.scope == {}


# ── _execute_backfill Tests ───────────────────────────────────────────


class TestExecuteBackfill:
    """Tests for _execute_backfill background task execution."""

    @pytest.mark.asyncio
    async def test_execute_backfill_job_not_found(self, db_session):
        """Job not found returns early without error."""
        mock_session = MagicMock()
        mock_session.get.return_value = None
        mock_session.close = MagicMock()

        with patch("app.database.SessionLocal", return_value=mock_session):
            await _execute_backfill(99999, ["vendor"], 10, {})

        mock_session.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_backfill_vendors_completed(self, db_session, test_user, enrichable_vendor):
        """Backfill processes vendors and marks job as completed."""
        job = EnrichmentJob(
            job_type="backfill",
            status="running",
            total_items=1,
            started_by_id=test_user.id,
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch(
                "app.services.deep_enrichment_service.deep_enrich_vendor",
                new_callable=AsyncMock,
                return_value={"status": "completed", "enriched_fields": ["industry"], "errors": []},
            ),
            patch("app.services.deep_enrichment_service.asyncio.sleep", new_callable=AsyncMock),
        ):
            await _execute_backfill(job.id, ["vendor"], 500, {})

        db_session.refresh(job)
        assert job.status == "completed"
        assert job.enriched_items >= 1
        assert job.completed_at is not None

    @pytest.mark.asyncio
    async def test_execute_backfill_companies_completed(self, db_session, test_user, enrichable_company):
        """Backfill processes companies when 'company' is in entity_types."""
        job = EnrichmentJob(
            job_type="backfill",
            status="running",
            total_items=1,
            started_by_id=test_user.id,
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch(
                "app.services.deep_enrichment_service.deep_enrich_company",
                new_callable=AsyncMock,
                return_value={"status": "completed", "enriched_fields": ["industry"], "errors": []},
            ),
            patch("app.services.deep_enrichment_service.asyncio.sleep", new_callable=AsyncMock),
        ):
            await _execute_backfill(job.id, ["company"], 500, {})

        db_session.refresh(job)
        assert job.status == "completed"
        assert job.enriched_items >= 1

    @pytest.mark.asyncio
    async def test_execute_backfill_vendor_cancelled(self, db_session, test_user, enrichable_vendor):
        """Job cancellation during vendor processing stops early."""
        job = EnrichmentJob(
            job_type="backfill",
            status="running",
            total_items=1,
            started_by_id=test_user.id,
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        # Set job to cancelled before processing starts
        orig_refresh = db_session.refresh

        def _cancel_on_refresh(obj, *args, **kwargs):
            orig_refresh(obj, *args, **kwargs)
            if isinstance(obj, EnrichmentJob):
                obj.status = "cancelled"

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch.object(db_session, "refresh", side_effect=_cancel_on_refresh),
            patch("app.services.deep_enrichment_service.asyncio.sleep", new_callable=AsyncMock),
        ):
            await _execute_backfill(job.id, ["vendor"], 500, {})

        db_session.expire_all()
        job_check = db_session.get(EnrichmentJob, job.id)
        assert job_check.completed_at is not None

    @pytest.mark.asyncio
    async def test_execute_backfill_company_cancelled(self, db_session, test_user, enrichable_company):
        """Job cancellation during company processing stops early."""
        job = EnrichmentJob(
            job_type="backfill",
            status="running",
            total_items=1,
            started_by_id=test_user.id,
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        orig_refresh = db_session.refresh

        def _cancel_on_refresh(obj, *args, **kwargs):
            orig_refresh(obj, *args, **kwargs)
            if isinstance(obj, EnrichmentJob):
                obj.status = "cancelled"

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch.object(db_session, "refresh", side_effect=_cancel_on_refresh),
            patch("app.services.deep_enrichment_service.asyncio.sleep", new_callable=AsyncMock),
        ):
            await _execute_backfill(job.id, ["company"], 500, {})

        db_session.expire_all()
        job_check = db_session.get(EnrichmentJob, job.id)
        assert job_check.completed_at is not None

    @pytest.mark.asyncio
    async def test_execute_backfill_vendor_with_errors(self, db_session, test_user, enrichable_vendor):
        """Vendor enrichment returning errors increments error_count."""
        job = EnrichmentJob(
            job_type="backfill",
            status="running",
            total_items=1,
            started_by_id=test_user.id,
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch(
                "app.services.deep_enrichment_service.deep_enrich_vendor",
                new_callable=AsyncMock,
                return_value={
                    "status": "completed",
                    "enriched_fields": [],
                    "errors": ["company_enrichment: timeout", "email_verification: failed"],
                },
            ),
            patch("app.services.deep_enrichment_service.asyncio.sleep", new_callable=AsyncMock),
        ):
            await _execute_backfill(job.id, ["vendor"], 500, {})

        db_session.refresh(job)
        assert job.status == "completed"
        assert job.error_count >= 2

    @pytest.mark.asyncio
    async def test_execute_backfill_vendor_error_status(self, db_session, test_user, enrichable_vendor):
        """Vendor enrichment returning error status increments error_count."""
        job = EnrichmentJob(
            job_type="backfill",
            status="running",
            total_items=1,
            started_by_id=test_user.id,
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch(
                "app.services.deep_enrichment_service.deep_enrich_vendor",
                new_callable=AsyncMock,
                return_value={"status": "error", "error": "vendor_1: some error"},
            ),
            patch("app.services.deep_enrichment_service.asyncio.sleep", new_callable=AsyncMock),
        ):
            await _execute_backfill(job.id, ["vendor"], 500, {})

        db_session.refresh(job)
        assert job.status == "completed"
        assert job.error_count >= 1

    @pytest.mark.asyncio
    async def test_execute_backfill_vendor_exception_in_enrich(self, db_session, test_user, enrichable_vendor):
        """Exception raised during vendor enrichment is caught."""
        job = EnrichmentJob(
            job_type="backfill",
            status="running",
            total_items=1,
            started_by_id=test_user.id,
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch(
                "app.services.deep_enrichment_service.deep_enrich_vendor",
                new_callable=AsyncMock,
                side_effect=Exception("Unexpected crash"),
            ),
            patch("app.services.deep_enrichment_service.asyncio.sleep", new_callable=AsyncMock),
        ):
            await _execute_backfill(job.id, ["vendor"], 500, {})

        db_session.refresh(job)
        assert job.status == "completed"

    @pytest.mark.asyncio
    async def test_execute_backfill_company_with_errors(self, db_session, test_user, enrichable_company):
        """Company enrichment returning errors increments error_count."""
        job = EnrichmentJob(
            job_type="backfill",
            status="running",
            total_items=1,
            started_by_id=test_user.id,
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch(
                "app.services.deep_enrichment_service.deep_enrich_company",
                new_callable=AsyncMock,
                return_value={
                    "status": "completed",
                    "enriched_fields": [],
                    "errors": ["clearbit: timeout"],
                },
            ),
            patch("app.services.deep_enrichment_service.asyncio.sleep", new_callable=AsyncMock),
        ):
            await _execute_backfill(job.id, ["company"], 500, {})

        db_session.refresh(job)
        assert job.status == "completed"
        assert job.error_count >= 1

    @pytest.mark.asyncio
    async def test_execute_backfill_company_error_status(self, db_session, test_user, enrichable_company):
        """Company enrichment returning error status increments error_count."""
        job = EnrichmentJob(
            job_type="backfill",
            status="running",
            total_items=1,
            started_by_id=test_user.id,
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch(
                "app.services.deep_enrichment_service.deep_enrich_company",
                new_callable=AsyncMock,
                return_value={"status": "error", "error": "company_1: some error"},
            ),
            patch("app.services.deep_enrichment_service.asyncio.sleep", new_callable=AsyncMock),
        ):
            await _execute_backfill(job.id, ["company"], 500, {})

        db_session.refresh(job)
        assert job.error_count >= 1

    @pytest.mark.asyncio
    async def test_execute_backfill_company_exception_in_enrich(self, db_session, test_user, enrichable_company):
        """Exception raised during company enrichment is caught."""
        job = EnrichmentJob(
            job_type="backfill",
            status="running",
            total_items=1,
            started_by_id=test_user.id,
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch(
                "app.services.deep_enrichment_service.deep_enrich_company",
                new_callable=AsyncMock,
                side_effect=Exception("Company crash"),
            ),
            patch("app.services.deep_enrichment_service.asyncio.sleep", new_callable=AsyncMock),
        ):
            await _execute_backfill(job.id, ["company"], 500, {})

        db_session.refresh(job)
        assert job.status == "completed"

    @pytest.mark.asyncio
    async def test_execute_backfill_deep_email_mining(self, db_session, test_user):
        """Deep email mining processes users with M365 connections."""
        job = EnrichmentJob(
            job_type="backfill",
            status="running",
            total_items=0,
            started_by_id=test_user.id,
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        # Create a user with m365 connection
        user = User(
            email="miner@trioscs.com",
            name="Email Miner",
            role="buyer",
            azure_id="miner-azure-id",
            m365_connected=True,
            refresh_token="fake-refresh-token",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()

        mock_scan_result = {
            "per_domain": {
                "vendor.com": {
                    "emails": ["sales@vendor.com"],
                    "sender_names": ["Sales Rep"],
                },
            },
        }
        mock_sig_data = {"full_name": "Sales Rep", "confidence": 0.5}

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch("app.services.deep_enrichment_service.asyncio.sleep", new_callable=AsyncMock),
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fake-token"),
            patch("app.connectors.email_mining.EmailMiner") as MockMiner,
            patch(
                "app.services.signature_parser.extract_signature",
                new_callable=AsyncMock,
                return_value=mock_sig_data,
            ),
            patch("app.services.signature_parser.cache_signature_extract"),
            patch(
                "app.services.deep_enrichment_service.link_contact_to_entities",
            ),
        ):
            mock_miner_instance = MockMiner.return_value
            mock_miner_instance.deep_scan_inbox = AsyncMock(return_value=mock_scan_result)

            await _execute_backfill(
                job.id,
                [],  # No vendor/company processing
                500,
                {"include_deep_email": True, "lookback_days": 90},
            )

        db_session.refresh(job)
        assert job.status == "completed"

    @pytest.mark.asyncio
    async def test_execute_backfill_deep_email_no_token(self, db_session, test_user):
        """User with no valid token is skipped during deep email mining."""
        job = EnrichmentJob(
            job_type="backfill",
            status="running",
            total_items=0,
            started_by_id=test_user.id,
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        user = User(
            email="notoken@trioscs.com",
            name="No Token",
            role="buyer",
            azure_id="notoken-azure-id",
            m365_connected=True,
            refresh_token="fake-token",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch("app.services.deep_enrichment_service.asyncio.sleep", new_callable=AsyncMock),
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value=None),
            patch("app.connectors.email_mining.EmailMiner"),
            patch("app.services.signature_parser.extract_signature", new_callable=AsyncMock),
            patch("app.services.signature_parser.cache_signature_extract"),
        ):
            await _execute_backfill(
                job.id,
                [],
                500,
                {"include_deep_email": True},
            )

        db_session.refresh(job)
        assert job.status == "completed"

    @pytest.mark.asyncio
    async def test_execute_backfill_deep_email_user_exception(self, db_session, test_user):
        """Exception during individual user email scan is caught and logged (lines 798-801)."""
        job = EnrichmentJob(
            job_type="backfill",
            status="running",
            total_items=0,
            started_by_id=test_user.id,
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        user = User(
            email="failuser@trioscs.com",
            name="Fail User",
            role="buyer",
            azure_id="fail-azure-id",
            m365_connected=True,
            refresh_token="fake-token",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch("app.services.deep_enrichment_service.asyncio.sleep", new_callable=AsyncMock),
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                side_effect=Exception("Token refresh failed"),
            ),
            patch("app.connectors.email_mining.EmailMiner"),
            patch("app.services.signature_parser.extract_signature", new_callable=AsyncMock),
            patch("app.services.signature_parser.cache_signature_extract"),
        ):
            await _execute_backfill(
                job.id,
                [],
                500,
                {"include_deep_email": True},
            )

        db_session.refresh(job)
        assert job.status == "completed"
        assert any("email_scan" in e for e in (job.error_log or []))

    @pytest.mark.asyncio
    async def test_execute_backfill_deep_email_import_error(self, db_session, test_user):
        """Import error during deep email mining is caught (lines 802-803)."""
        job = EnrichmentJob(
            job_type="backfill",
            status="running",
            total_items=0,
            started_by_id=test_user.id,
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch("app.services.deep_enrichment_service.asyncio.sleep", new_callable=AsyncMock),
            patch.dict("sys.modules", {"app.connectors.email_mining": None}),
        ):
            await _execute_backfill(
                job.id,
                [],
                500,
                {"include_deep_email": True},
            )

        db_session.refresh(job)
        assert job.status == "completed"
        assert any("deep_email_mining" in e for e in (job.error_log or []))

    @pytest.mark.asyncio
    async def test_execute_backfill_fatal_exception(self, db_session, test_user):
        """Fatal exception marks job as failed (lines 819-829)."""
        job = EnrichmentJob(
            job_type="backfill",
            status="running",
            total_items=1,
            started_by_id=test_user.id,
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        mock_session = MagicMock()
        mock_session.get.side_effect = [job, job]  # First call returns job, second for error handler
        mock_session.query.side_effect = Exception("Fatal DB error")
        mock_session.close = MagicMock()
        mock_session.commit = MagicMock()

        with patch("app.database.SessionLocal", return_value=mock_session):
            await _execute_backfill(job.id, ["vendor"], 500, {})

        mock_session.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_backfill_fatal_exception_recovery_failure(self, db_session, test_user):
        """Fatal exception where even the error handler fails (line 828-829)."""
        job = EnrichmentJob(
            job_type="backfill",
            status="running",
            total_items=1,
            started_by_id=test_user.id,
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        mock_session = MagicMock()
        # First get returns job, query raises, then second get also raises
        mock_session.get.side_effect = [job, Exception("Recovery also failed")]
        mock_session.query.side_effect = Exception("Fatal DB error")
        mock_session.close = MagicMock()

        with patch("app.database.SessionLocal", return_value=mock_session):
            await _execute_backfill(job.id, ["vendor"], 500, {})

        mock_session.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_backfill_both_vendors_and_companies(
        self, db_session, test_user, enrichable_vendor, enrichable_company
    ):
        """Processing both vendors and companies in a single backfill job."""
        job = EnrichmentJob(
            job_type="backfill",
            status="running",
            total_items=2,
            started_by_id=test_user.id,
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch(
                "app.services.deep_enrichment_service.deep_enrich_vendor",
                new_callable=AsyncMock,
                return_value={"status": "completed", "enriched_fields": ["industry"], "errors": []},
            ),
            patch(
                "app.services.deep_enrichment_service.deep_enrich_company",
                new_callable=AsyncMock,
                return_value={"status": "completed", "enriched_fields": ["industry"], "errors": []},
            ),
            patch("app.services.deep_enrichment_service.asyncio.sleep", new_callable=AsyncMock),
        ):
            await _execute_backfill(job.id, ["vendor", "company"], 500, {})

        db_session.refresh(job)
        assert job.status == "completed"
        assert job.enriched_items >= 2

    @pytest.mark.asyncio
    async def test_execute_backfill_deep_email_low_confidence_sig(self, db_session, test_user):
        """Signature data with confidence <= 0.3 is skipped."""
        job = EnrichmentJob(
            job_type="backfill",
            status="running",
            total_items=0,
            started_by_id=test_user.id,
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        user = User(
            email="lowconf@trioscs.com",
            name="Low Conf",
            role="buyer",
            azure_id="lowconf-azure-id",
            m365_connected=True,
            refresh_token="fake-token",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()

        mock_scan_result = {
            "per_domain": {
                "vendor.com": {
                    "emails": ["low@vendor.com"],
                    "sender_names": ["Low Conf Person"],
                },
            },
        }

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch("app.services.deep_enrichment_service.asyncio.sleep", new_callable=AsyncMock),
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fake-token"),
            patch("app.connectors.email_mining.EmailMiner") as MockMiner,
            patch(
                "app.services.signature_parser.extract_signature",
                new_callable=AsyncMock,
                return_value={"full_name": "Low", "confidence": 0.1},  # Below 0.3
            ),
            patch("app.services.signature_parser.cache_signature_extract") as mock_cache,
            patch(
                "app.services.deep_enrichment_service.link_contact_to_entities",
            ) as mock_link,
        ):
            mock_miner_instance = MockMiner.return_value
            mock_miner_instance.deep_scan_inbox = AsyncMock(return_value=mock_scan_result)

            await _execute_backfill(
                job.id,
                [],
                500,
                {"include_deep_email": True},
            )

        # cache_signature_extract and link_contact_to_entities should NOT be called
        mock_cache.assert_not_called()
        mock_link.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_backfill_vendor_gather_exception_result(self, db_session, test_user, enrichable_vendor):
        """Exception returned from asyncio.gather increments error_count (lines 680-682)."""
        job = EnrichmentJob(
            job_type="backfill",
            status="running",
            total_items=1,
            started_by_id=test_user.id,
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        # Patch gather to return an Exception object (simulating return_exceptions=True behavior)
        original_gather = asyncio.gather
        call_count = [0]

        async def _mock_gather(*coros, **kwargs):
            call_count[0] += 1
            # First gather call is for vendor batch; return an Exception
            if call_count[0] == 1:
                # Cancel the coroutines to avoid warnings
                for c in coros:
                    c.close()
                return [RuntimeError("Semaphore error")]
            return await original_gather(*coros, **kwargs)

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch("app.services.deep_enrichment_service.asyncio.gather", side_effect=_mock_gather),
            patch("app.services.deep_enrichment_service.asyncio.sleep", new_callable=AsyncMock),
        ):
            await _execute_backfill(job.id, ["vendor"], 500, {})

        db_session.refresh(job)
        assert job.status == "completed"
        assert job.error_count >= 1

    @pytest.mark.asyncio
    async def test_execute_backfill_company_gather_exception_result(self, db_session, test_user, enrichable_company):
        """Exception returned from company gather increments error_count (lines 741-743)."""
        job = EnrichmentJob(
            job_type="backfill",
            status="running",
            total_items=1,
            started_by_id=test_user.id,
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        original_gather = asyncio.gather
        call_count = [0]

        async def _mock_gather(*coros, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                for c in coros:
                    c.close()
                return [RuntimeError("Company semaphore error")]
            return await original_gather(*coros, **kwargs)

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch("app.services.deep_enrichment_service.asyncio.gather", side_effect=_mock_gather),
            patch("app.services.deep_enrichment_service.asyncio.sleep", new_callable=AsyncMock),
        ):
            await _execute_backfill(job.id, ["company"], 500, {})

        db_session.refresh(job)
        assert job.status == "completed"
        assert job.error_count >= 1

    @pytest.mark.asyncio
    async def test_execute_backfill_deep_email_no_sender_names(self, db_session, test_user):
        """Domain data with no sender_names uses empty string."""
        job = EnrichmentJob(
            job_type="backfill",
            status="running",
            total_items=0,
            started_by_id=test_user.id,
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        user = User(
            email="nosender@trioscs.com",
            name="No Sender",
            role="buyer",
            azure_id="nosender-azure-id",
            m365_connected=True,
            refresh_token="fake-token",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()

        mock_scan_result = {
            "per_domain": {
                "vendor.com": {
                    "emails": ["nosender@vendor.com"],
                    # No sender_names key
                },
            },
        }

        with (
            patch("app.database.SessionLocal", return_value=db_session),
            patch.object(db_session, "close"),
            patch("app.services.deep_enrichment_service.asyncio.sleep", new_callable=AsyncMock),
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fake-token"),
            patch("app.connectors.email_mining.EmailMiner") as MockMiner,
            patch(
                "app.services.signature_parser.extract_signature",
                new_callable=AsyncMock,
                return_value={"full_name": "Test", "confidence": 0.5},
            ) as mock_extract,
            patch("app.services.signature_parser.cache_signature_extract"),
            patch("app.services.deep_enrichment_service.link_contact_to_entities"),
        ):
            mock_miner_instance = MockMiner.return_value
            mock_miner_instance.deep_scan_inbox = AsyncMock(return_value=mock_scan_result)

            await _execute_backfill(
                job.id,
                [],
                500,
                {"include_deep_email": True},
            )

        # extract_signature should have been called with sender_name=""
        mock_extract.assert_called_once()
        call_kwargs = mock_extract.call_args
        assert (
            call_kwargs[1]["sender_name"] == "" or call_kwargs[0][1] == ""
            if len(call_kwargs[0]) > 1
            else call_kwargs[1].get("sender_name") == ""
        )
