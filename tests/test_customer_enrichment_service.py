"""Tests for customer_enrichment_service — waterfall orchestrator.

Covers: enrich_customer_account, get_enrichment_gaps, helper functions,
cooldown, force bypass, Apollo primary discovery, Lusha phone enrichment.
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch, MagicMock
from tests.conftest import engine  # noqa: F401

from app.models.crm import Company, CustomerSite, SiteContact


@pytest.fixture
def _mock_settings():
    with patch("app.services.customer_enrichment_service.settings") as mock_s:
        mock_s.customer_enrichment_enabled = True
        mock_s.customer_enrichment_cooldown_days = 90
        mock_s.customer_enrichment_contacts_per_account = 5
        yield mock_s


@pytest.fixture
def company_with_domain(db_session):
    co = Company(name="Test Corp", domain="testcorp.com", is_active=True)
    db_session.add(co)
    db_session.flush()
    site = CustomerSite(company_id=co.id, site_name="Test HQ")
    db_session.add(site)
    db_session.commit()
    db_session.refresh(co)
    return co


# ── Helper function tests ──────────────────────────────────────────


def test_classify_contact_role():
    from app.services.customer_enrichment_service import _classify_contact_role
    assert _classify_contact_role("VP of Procurement") == "decision_maker"
    assert _classify_contact_role("Buyer") == "buyer"
    assert _classify_contact_role("Senior Engineer") == "technical"
    assert _classify_contact_role("Office Manager") == "operations"
    assert _classify_contact_role(None) == "unknown"
    assert _classify_contact_role("") == "unknown"


def test_is_direct_dial():
    from app.services.customer_enrichment_service import _is_direct_dial
    assert _is_direct_dial("direct_dial") is True
    assert _is_direct_dial("mobile") is True
    assert _is_direct_dial("work") is False
    assert _is_direct_dial(None) is False


def test_contacts_needed(db_session, company_with_domain, _mock_settings):
    from app.services.customer_enrichment_service import _contacts_needed
    # No contacts yet — need all 5
    needed = _contacts_needed(db_session, company_with_domain.id, 5)
    assert needed == 5


def test_contacts_needed_with_existing(db_session, company_with_domain, _mock_settings):
    from app.services.customer_enrichment_service import _contacts_needed
    site = db_session.query(CustomerSite).filter_by(company_id=company_with_domain.id).first()
    for i in range(3):
        db_session.add(SiteContact(
            customer_site_id=site.id,
            full_name=f"Contact {i}",
            email=f"c{i}@testcorp.com",
        ))
    db_session.flush()
    needed = _contacts_needed(db_session, company_with_domain.id, 5)
    assert needed == 2


def test_dedup_contacts():
    from app.services.customer_enrichment_service import _dedup_contacts
    contacts = [
        {"email": "a@test.com", "full_name": "A", "source": "apollo"},
        {"email": "a@test.com", "full_name": "A duplicate", "source": "lusha"},
        {"email": "b@test.com", "full_name": "B", "source": "apollo"},
    ]
    result = _dedup_contacts(contacts)
    assert len(result) == 2
    assert result[0]["source"] == "apollo"  # First one kept (Apollo priority)


def test_get_company_domain():
    from app.services.customer_enrichment_service import _get_company_domain
    co = MagicMock()
    co.domain = "acme.com"
    co.website = None
    assert _get_company_domain(co) == "acme.com"

    co.domain = None
    co.website = "https://www.acme.com/about"
    assert _get_company_domain(co) == "acme.com"


# ── Waterfall tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enrich_disabled(db_session, company_with_domain):
    with patch("app.services.customer_enrichment_service.settings") as mock_s:
        mock_s.customer_enrichment_enabled = False
        from app.services.customer_enrichment_service import enrich_customer_account
        result = await enrich_customer_account(company_with_domain.id, db_session)
        assert result["contacts_added"] == 0
        assert "disabled" in result.get("error", "")


@pytest.mark.asyncio
async def test_enrich_cooldown(db_session, company_with_domain, _mock_settings):
    company_with_domain.customer_enrichment_at = datetime.now(timezone.utc) - timedelta(days=10)
    db_session.flush()
    from app.services.customer_enrichment_service import enrich_customer_account
    result = await enrich_customer_account(company_with_domain.id, db_session)
    assert "Cooldown" in result.get("error", "")


@pytest.mark.asyncio
async def test_enrich_force_bypass_cooldown(db_session, company_with_domain, _mock_settings):
    """Force bypasses cooldown. Apollo is primary, Lusha enriches phones."""
    company_with_domain.customer_enrichment_at = datetime.now(timezone.utc) - timedelta(days=10)
    db_session.flush()

    with patch("app.services.customer_enrichment_service._step_apollo", new_callable=AsyncMock) as mock_apollo, \
         patch("app.services.customer_enrichment_service._step_hunter_verify", new_callable=AsyncMock) as mock_hunter, \
         patch("app.services.customer_enrichment_service._step_lusha_phones", new_callable=AsyncMock) as mock_lusha_phones:
        mock_apollo.return_value = [
            {"full_name": "Test Person", "email": "test@testcorp.com", "title": "Buyer",
             "phone": None, "phone_type": None, "source": "apollo", "confidence": "medium",
             "enrichment_field_sources": {"email": "apollo", "name": "apollo", "phone": None}},
        ]

        async def _verify(db, contacts):
            for c in contacts:
                c["email_verified"] = True
                c["email_verification_status"] = "valid"
            return contacts
        mock_hunter.side_effect = _verify

        async def _lusha_phones(db, contacts, domain):
            for c in contacts:
                if not c.get("phone"):
                    c["phone"] = "+15550100"
                    c["phone_type"] = "direct_dial"
                    fs = c.get("enrichment_field_sources") or {}
                    fs["phone"] = "lusha"
                    c["enrichment_field_sources"] = fs
            return contacts
        mock_lusha_phones.side_effect = _lusha_phones

        from app.services.customer_enrichment_service import enrich_customer_account
        result = await enrich_customer_account(company_with_domain.id, db_session, force=True)
        assert result.get("ok") is True
        assert result["contacts_added"] >= 1


@pytest.mark.asyncio
async def test_enrich_no_domain(db_session, _mock_settings):
    co = Company(name="No Domain Corp", is_active=True)
    db_session.add(co)
    db_session.commit()
    from app.services.customer_enrichment_service import enrich_customer_account
    result = await enrich_customer_account(co.id, db_session)
    assert "No domain" in result.get("error", "")


@pytest.mark.asyncio
async def test_enrich_company_not_found(db_session, _mock_settings):
    from app.services.customer_enrichment_service import enrich_customer_account
    result = await enrich_customer_account(99999, db_session)
    assert "not found" in result.get("error", "")


@pytest.mark.asyncio
async def test_full_waterfall(db_session, company_with_domain, _mock_settings):
    """Test the full waterfall: Apollo discovers → Hunter verifies → Lusha adds phones."""
    with patch("app.services.customer_enrichment_service._step_apollo", new_callable=AsyncMock) as mock_apollo, \
         patch("app.services.customer_enrichment_service._step_hunter_verify", new_callable=AsyncMock) as mock_hunter, \
         patch("app.services.customer_enrichment_service._step_lusha_phones", new_callable=AsyncMock) as mock_lusha_phones:

        mock_apollo.return_value = [
            {"full_name": "Alice Buyer", "email": "alice@testcorp.com", "title": "Procurement Manager",
             "phone": None, "phone_type": None, "source": "apollo", "confidence": "medium",
             "enrichment_field_sources": {"email": "apollo", "name": "apollo", "phone": None}},
            {"full_name": "Bob Tech", "email": "bob@testcorp.com", "title": "Sr Engineer",
             "phone": None, "phone_type": None, "source": "apollo", "confidence": "medium",
             "enrichment_field_sources": {"email": "apollo", "name": "apollo", "phone": None}},
        ]

        async def _verify(db, contacts):
            for c in contacts:
                c["email_verified"] = True
                c["email_verification_status"] = "valid"
            return contacts
        mock_hunter.side_effect = _verify

        async def _lusha_phones(db, contacts, domain):
            for c in contacts:
                if not c.get("phone") or c.get("phone_type") not in ("direct_dial", "mobile"):
                    c["phone"] = "+1-555-0100"
                    c["phone_type"] = "direct_dial"
                    fs = c.get("enrichment_field_sources") or {}
                    fs["phone"] = "lusha"
                    c["enrichment_field_sources"] = fs
            return contacts
        mock_lusha_phones.side_effect = _lusha_phones

        from app.services.customer_enrichment_service import enrich_customer_account
        result = await enrich_customer_account(company_with_domain.id, db_session, force=True)

        assert result["ok"] is True
        assert result["contacts_added"] == 2
        assert "apollo" in result["sources_used"]
        assert "lusha_phones" in result["sources_used"]

        # Verify contacts were saved
        site = db_session.query(CustomerSite).filter_by(company_id=company_with_domain.id).first()
        contacts = db_session.query(SiteContact).filter_by(customer_site_id=site.id).all()
        assert len(contacts) == 2

        # Verify roles were classified
        roles = {c.contact_role for c in contacts}
        assert "buyer" in roles
        assert "technical" in roles


# ── Gap analysis tests ──────────────────────────────────────────────


def test_get_enrichment_gaps(db_session, _mock_settings):
    from app.services.customer_enrichment_service import get_enrichment_gaps
    from app.models.auth import User

    user = User(email="owner@test.com", name="Owner", azure_id="az-owner")
    db_session.add(user)
    db_session.flush()

    # Assigned company
    co1 = Company(name="Assigned Corp", domain="assigned.com", is_active=True, account_owner_id=user.id)
    db_session.add(co1)
    # Unassigned company
    co2 = Company(name="Unassigned Corp", domain="unassigned.com", is_active=True)
    db_session.add(co2)
    db_session.flush()

    # Add sites
    db_session.add(CustomerSite(company_id=co1.id, site_name="HQ"))
    db_session.add(CustomerSite(company_id=co2.id, site_name="HQ"))
    db_session.commit()

    gaps = get_enrichment_gaps(db_session, limit=10)
    assert len(gaps) >= 2
    # Assigned should be first
    assert gaps[0]["company_name"] == "Assigned Corp"
    assert gaps[0]["account_owner_id"] is not None


# ── Apollo primary discovery tests ─────────────────────────────────


@pytest.mark.asyncio
async def test_apollo_primary_discovery(db_session, company_with_domain, _mock_settings):
    """Apollo is the primary source; all contacts come from Apollo."""
    with patch("app.services.customer_enrichment_service._step_apollo", new_callable=AsyncMock) as mock_apollo, \
         patch("app.services.customer_enrichment_service._step_hunter_verify", new_callable=AsyncMock) as mock_hunter, \
         patch("app.services.customer_enrichment_service._step_lusha_phones", new_callable=AsyncMock) as mock_lusha_phones:

        mock_apollo.return_value = [
            {"full_name": f"Apollo Person {i}", "email": f"apollo{i}@testcorp.com",
             "title": "Buyer", "source": "apollo", "confidence": "medium",
             "enrichment_field_sources": {"email": "apollo", "name": "apollo", "phone": None}}
            for i in range(5)
        ]

        async def _verify(db, contacts):
            for c in contacts:
                c["email_verified"] = True
                c["email_verification_status"] = "valid"
            return contacts
        mock_hunter.side_effect = _verify

        async def _lusha_phones(db, contacts, domain):
            return contacts  # No phone enrichment
        mock_lusha_phones.side_effect = _lusha_phones

        from app.services.customer_enrichment_service import enrich_customer_account
        result = await enrich_customer_account(company_with_domain.id, db_session, force=True)

        mock_apollo.assert_awaited_once()
        assert "apollo" in result["sources_used"]
        assert result["contacts_added"] == 5


@pytest.mark.asyncio
async def test_lusha_phone_enrichment_skips_existing_direct_dials(db_session, _mock_settings):
    """Lusha phone enrichment skips contacts that already have direct dials."""
    contacts = [
        {"full_name": "Has Phone", "email": "hasphone@testcorp.com",
         "title": "Buyer", "phone": "+1-555-1111", "phone_type": "direct_dial",
         "source": "apollo", "enrichment_field_sources": {"email": "apollo", "name": "apollo", "phone": "apollo"}},
        {"full_name": "No Phone", "email": "nophone@testcorp.com",
         "title": "Engineer", "phone": None, "phone_type": None,
         "source": "apollo", "enrichment_field_sources": {"email": "apollo", "name": "apollo", "phone": None}},
    ]

    with patch("app.services.customer_enrichment_service.can_use_credits", return_value=True), \
         patch("app.services.customer_enrichment_service.record_credit_usage"), \
         patch("app.connectors.lusha_client.find_person", new_callable=AsyncMock) as mock_find:
        mock_find.return_value = {"phone": "+1-555-2222", "phone_type": "mobile"}

        from app.services.customer_enrichment_service import _step_lusha_phones
        result = await _step_lusha_phones(db_session, contacts, "testcorp.com")

        # find_person should only be called for "No Phone" (the one without direct dial)
        assert mock_find.await_count == 1
        assert len(result) == 2
        # "Has Phone" kept original
        assert result[0]["phone"] == "+1-555-1111"
        # "No Phone" got Lusha phone
        assert result[1]["phone"] == "+1-555-2222"
        assert result[1]["enrichment_field_sources"]["phone"] == "lusha"


@pytest.mark.asyncio
async def test_apollo_exception_handled_gracefully(db_session, company_with_domain, _mock_settings):
    """If Apollo raises an exception, Lusha discovery fills the gap."""
    with patch("app.services.customer_enrichment_service._step_apollo", new_callable=AsyncMock) as mock_apollo, \
         patch("app.services.customer_enrichment_service._step_lusha_discovery", new_callable=AsyncMock) as mock_lusha_disc, \
         patch("app.services.customer_enrichment_service._step_hunter_verify", new_callable=AsyncMock) as mock_hunter, \
         patch("app.services.customer_enrichment_service._step_lusha_phones", new_callable=AsyncMock) as mock_lusha_phones:
        mock_apollo.side_effect = Exception("Apollo API timeout")
        mock_lusha_disc.return_value = [
            {"full_name": "Lusha Person", "email": "lusha@testcorp.com", "title": "Buyer",
             "source": "lusha", "confidence": 85},
        ]
        async def _verify(db, contacts):
            for c in contacts:
                c["email_verified"] = True
                c["email_verification_status"] = "valid"
            return contacts
        mock_hunter.side_effect = _verify

        async def _lusha_phones(db, contacts, domain):
            return contacts
        mock_lusha_phones.side_effect = _lusha_phones

        from app.services.customer_enrichment_service import enrich_customer_account
        result = await enrich_customer_account(company_with_domain.id, db_session, force=True)

        assert result["ok"] is True
        assert result["contacts_added"] == 1
        assert "apollo" not in result["sources_used"]
        assert "lusha" in result["sources_used"]


@pytest.mark.asyncio
async def test_field_source_attribution(db_session, company_with_domain, _mock_settings):
    """Verify enrichment_field_sources tracks per-field attribution correctly."""
    with patch("app.services.customer_enrichment_service._step_apollo", new_callable=AsyncMock) as mock_apollo, \
         patch("app.services.customer_enrichment_service._step_hunter_verify", new_callable=AsyncMock) as mock_hunter, \
         patch("app.services.customer_enrichment_service._step_lusha_phones", new_callable=AsyncMock) as mock_lusha_phones:

        mock_apollo.return_value = [
            {"full_name": "Jane Doe", "email": "jane@testcorp.com",
             "title": "Buyer", "phone": None, "phone_type": None,
             "source": "apollo", "confidence": "medium",
             "enrichment_field_sources": {"email": "apollo", "name": "apollo", "phone": None}},
        ]

        async def _verify(db, contacts):
            for c in contacts:
                c["email_verified"] = True
                c["email_verification_status"] = "valid"
            return contacts
        mock_hunter.side_effect = _verify

        # Lusha adds phone
        async def _lusha_phones(db, contacts, domain):
            for c in contacts:
                if not c.get("phone"):
                    c["phone"] = "+1-555-0100"
                    c["phone_type"] = "direct_dial"
                    fs = c.get("enrichment_field_sources") or {}
                    fs["phone"] = "lusha"
                    c["enrichment_field_sources"] = fs
            return contacts
        mock_lusha_phones.side_effect = _lusha_phones

        from app.services.customer_enrichment_service import enrich_customer_account
        result = await enrich_customer_account(company_with_domain.id, db_session, force=True)

        assert result["ok"] is True
        assert result["contacts_added"] == 1

        # Verify field source attribution on saved contact
        site = db_session.query(CustomerSite).filter_by(company_id=company_with_domain.id).first()
        contacts = db_session.query(SiteContact).filter_by(customer_site_id=site.id).all()
        assert len(contacts) == 1
        efs = contacts[0].enrichment_field_sources
        assert efs["email"] == "apollo"
        assert efs["name"] == "apollo"
        assert efs["phone"] == "lusha"


# ── Lusha phone enrichment unit tests ─────────────────────────────


@pytest.mark.asyncio
async def test_step_lusha_phones_credits_exhausted(db_session, _mock_settings):
    """Lusha phone enrichment stops calling API when credits are exhausted."""
    contacts = [
        {"full_name": "Person 1", "email": "p1@test.com", "phone": None, "phone_type": None},
        {"full_name": "Person 2", "email": "p2@test.com", "phone": None, "phone_type": None},
    ]
    call_count = 0
    with patch("app.services.customer_enrichment_service.can_use_credits") as mock_can, \
         patch("app.services.customer_enrichment_service.record_credit_usage"), \
         patch("app.connectors.lusha_client.find_person", new_callable=AsyncMock) as mock_find:
        # Allow first call, deny second
        mock_can.side_effect = [True, False]
        mock_find.return_value = {"phone": "+1-555-0001", "phone_type": "direct_dial"}

        from app.services.customer_enrichment_service import _step_lusha_phones
        result = await _step_lusha_phones(db_session, contacts, "test.com")

        assert len(result) == 2
        # First contact got phone, second did not (credits exhausted)
        assert result[0]["phone"] == "+1-555-0001"
        assert result[1]["phone"] is None
        assert mock_find.await_count == 1


@pytest.mark.asyncio
async def test_apollo_zero_results_falls_through(db_session, company_with_domain, _mock_settings):
    """When Apollo returns 0 results, Lusha discovery fills the gap."""
    with patch("app.services.customer_enrichment_service._step_apollo", new_callable=AsyncMock) as mock_apollo, \
         patch("app.services.customer_enrichment_service._step_lusha_discovery", new_callable=AsyncMock) as mock_lusha_disc, \
         patch("app.services.customer_enrichment_service._step_hunter_verify", new_callable=AsyncMock) as mock_hunter, \
         patch("app.services.customer_enrichment_service._step_lusha_phones", new_callable=AsyncMock) as mock_lusha_phones:
        mock_apollo.return_value = []
        mock_lusha_disc.return_value = [
            {"full_name": "Lusha Buyer", "email": "buyer@testcorp.com", "title": "Purchasing Manager",
             "source": "lusha", "confidence": 85},
        ]
        async def _verify(db, contacts):
            for c in contacts:
                c["email_verified"] = True
                c["email_verification_status"] = "valid"
            return contacts
        mock_hunter.side_effect = _verify

        async def _lusha_phones(db, contacts, domain):
            return contacts
        mock_lusha_phones.side_effect = _lusha_phones

        from app.services.customer_enrichment_service import enrich_customer_account
        result = await enrich_customer_account(company_with_domain.id, db_session, force=True)

        assert result["ok"] is True
        assert result["contacts_added"] == 1
        assert "apollo" not in result["sources_used"]
        assert "lusha" in result["sources_used"]


@pytest.mark.asyncio
async def test_lusha_phone_only_contacts_missing_phones(db_session, _mock_settings):
    """Verify Lusha find_person is NOT called for contacts with direct dials."""
    contacts = [
        {"full_name": "Has DD", "email": "dd@test.com",
         "phone": "+1-555-0001", "phone_type": "direct_dial"},
        {"full_name": "Has Mobile", "email": "mob@test.com",
         "phone": "+1-555-0002", "phone_type": "mobile"},
        {"full_name": "No Phone", "email": "nophone@test.com",
         "phone": None, "phone_type": None},
        {"full_name": "Work Phone", "email": "work@test.com",
         "phone": "+1-555-0003", "phone_type": "work"},
    ]

    with patch("app.services.customer_enrichment_service.can_use_credits", return_value=True), \
         patch("app.services.customer_enrichment_service.record_credit_usage"), \
         patch("app.connectors.lusha_client.find_person", new_callable=AsyncMock) as mock_find:
        mock_find.return_value = {"phone": "+1-555-9999", "phone_type": "direct_dial"}

        from app.services.customer_enrichment_service import _step_lusha_phones
        result = await _step_lusha_phones(db_session, contacts, "test.com")

        # Only "No Phone" and "Work Phone" should trigger find_person (2 calls)
        assert mock_find.await_count == 2
        # All 4 contacts returned
        assert len(result) == 4
        # Direct dial and mobile kept original phones
        assert result[0]["phone"] == "+1-555-0001"
        assert result[1]["phone"] == "+1-555-0002"
        # No Phone and Work Phone got Lusha phone
        assert result[2]["phone"] == "+1-555-9999"
        assert result[3]["phone"] == "+1-555-9999"


