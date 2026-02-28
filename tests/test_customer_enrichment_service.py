"""Tests for customer_enrichment_service — waterfall orchestrator.

Covers: enrich_customer_account, get_enrichment_gaps, helper functions, cooldown, force bypass.
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
        {"email": "a@test.com", "full_name": "A", "source": "lusha"},
        {"email": "a@test.com", "full_name": "A duplicate", "source": "clay"},
        {"email": "b@test.com", "full_name": "B", "source": "apollo"},
    ]
    result = _dedup_contacts(contacts)
    assert len(result) == 2
    assert result[0]["source"] == "lusha"  # First one kept


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
    company_with_domain.customer_enrichment_at = datetime.now(timezone.utc) - timedelta(days=10)
    db_session.flush()

    with patch("app.services.customer_enrichment_service._step_lusha", new_callable=AsyncMock) as mock_lusha, \
         patch("app.services.customer_enrichment_service._step_clay", new_callable=AsyncMock) as mock_clay, \
         patch("app.services.customer_enrichment_service._step_hunter_verify", new_callable=AsyncMock) as mock_hunter, \
         patch("app.services.customer_enrichment_service._step_apollo", new_callable=AsyncMock) as mock_apollo:
        mock_lusha.return_value = [
            {"full_name": "Test Person", "email": "test@testcorp.com", "title": "Buyer",
             "phone": "+15550100", "phone_type": "direct_dial", "source": "lusha", "confidence": 90}
        ]
        mock_clay.return_value = []
        mock_apollo.return_value = []
        async def _verify(db, contacts):
            for c in contacts:
                c["email_verified"] = True
                c["email_verification_status"] = "valid"
            return contacts
        mock_hunter.side_effect = _verify
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
    """Test the full waterfall: Lusha returns contacts, Hunter verifies."""
    with patch("app.services.customer_enrichment_service._step_lusha", new_callable=AsyncMock) as mock_lusha, \
         patch("app.services.customer_enrichment_service._step_clay", new_callable=AsyncMock) as mock_clay, \
         patch("app.services.customer_enrichment_service._step_hunter_verify", new_callable=AsyncMock) as mock_hunter, \
         patch("app.services.customer_enrichment_service._step_apollo", new_callable=AsyncMock) as mock_apollo:

        mock_lusha.return_value = [
            {"full_name": "Alice Buyer", "email": "alice@testcorp.com", "title": "Procurement Manager",
             "phone": "+1-555-0100", "phone_type": "direct_dial", "source": "lusha", "confidence": 85},
            {"full_name": "Bob Tech", "email": "bob@testcorp.com", "title": "Sr Engineer",
             "phone": None, "phone_type": None, "source": "lusha", "confidence": 70},
        ]
        mock_clay.return_value = [
            {"full_name": "Carol Director", "email": "carol@testcorp.com", "title": "VP Operations",
             "phone": "+1-555-0300", "phone_type": "work", "source": "clay", "confidence": 60},
        ]
        # Hunter verifies all
        async def _verify(db, contacts):
            for c in contacts:
                c["email_verified"] = True
                c["email_verification_status"] = "valid"
            return contacts
        mock_hunter.side_effect = _verify
        mock_apollo.return_value = []

        from app.services.customer_enrichment_service import enrich_customer_account
        result = await enrich_customer_account(company_with_domain.id, db_session, force=True)

        assert result["ok"] is True
        assert result["contacts_added"] == 3
        assert "lusha" in result["sources_used"]
        assert "clay" in result["sources_used"]

        # Verify contacts were saved
        site = db_session.query(CustomerSite).filter_by(company_id=company_with_domain.id).first()
        contacts = db_session.query(SiteContact).filter_by(customer_site_id=site.id).all()
        assert len(contacts) == 3

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


# ── Clay co-primary tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_clay_runs_even_when_lusha_fills_target(db_session, company_with_domain, _mock_settings):
    """Clay should run concurrently with Lusha, even if Lusha returns enough contacts."""
    with patch("app.services.customer_enrichment_service._step_lusha", new_callable=AsyncMock) as mock_lusha, \
         patch("app.services.customer_enrichment_service._step_clay", new_callable=AsyncMock) as mock_clay, \
         patch("app.services.customer_enrichment_service._step_hunter_verify", new_callable=AsyncMock) as mock_hunter, \
         patch("app.services.customer_enrichment_service._step_apollo", new_callable=AsyncMock) as mock_apollo:
        # Lusha returns 5 contacts (full target)
        mock_lusha.return_value = [
            {"full_name": f"Lusha Person {i}", "email": f"lusha{i}@testcorp.com",
             "title": "Buyer", "source": "lusha", "confidence": 85}
            for i in range(5)
        ]
        # Clay should still be called
        mock_clay.return_value = [
            {"full_name": "Clay Person", "email": "clay@testcorp.com",
             "title": "VP Procurement", "source": "clay", "confidence": 70},
        ]
        async def _verify(db, contacts):
            for c in contacts:
                c["email_verified"] = True
                c["email_verification_status"] = "valid"
            return contacts
        mock_hunter.side_effect = _verify
        mock_apollo.return_value = []

        from app.services.customer_enrichment_service import enrich_customer_account
        result = await enrich_customer_account(company_with_domain.id, db_session, force=True)

        # Both sources were called
        mock_lusha.assert_awaited_once()
        mock_clay.assert_awaited_once()
        assert "lusha" in result["sources_used"]
        assert "clay" in result["sources_used"]


@pytest.mark.asyncio
async def test_clay_only_when_lusha_fails(db_session, company_with_domain, _mock_settings):
    """If Lusha returns nothing, Clay contacts should still be saved."""
    with patch("app.services.customer_enrichment_service._step_lusha", new_callable=AsyncMock) as mock_lusha, \
         patch("app.services.customer_enrichment_service._step_clay", new_callable=AsyncMock) as mock_clay, \
         patch("app.services.customer_enrichment_service._step_hunter_verify", new_callable=AsyncMock) as mock_hunter, \
         patch("app.services.customer_enrichment_service._step_apollo", new_callable=AsyncMock) as mock_apollo:
        mock_lusha.return_value = []
        mock_clay.return_value = [
            {"full_name": "Clay Buyer", "email": "buyer@testcorp.com",
             "title": "Purchasing Manager", "source": "clay", "confidence": 70},
        ]
        async def _verify(db, contacts):
            for c in contacts:
                c["email_verified"] = True
                c["email_verification_status"] = "valid"
            return contacts
        mock_hunter.side_effect = _verify
        mock_apollo.return_value = []

        from app.services.customer_enrichment_service import enrich_customer_account
        result = await enrich_customer_account(company_with_domain.id, db_session, force=True)

        assert result["ok"] is True
        assert result["contacts_added"] == 1
        assert "clay" in result["sources_used"]
        assert "lusha" not in result["sources_used"]


@pytest.mark.asyncio
async def test_clay_exception_doesnt_block_waterfall(db_session, company_with_domain, _mock_settings):
    """If Clay raises an exception, Lusha contacts should still be processed."""
    with patch("app.services.customer_enrichment_service._step_lusha", new_callable=AsyncMock) as mock_lusha, \
         patch("app.services.customer_enrichment_service._step_clay", new_callable=AsyncMock) as mock_clay, \
         patch("app.services.customer_enrichment_service._step_hunter_verify", new_callable=AsyncMock) as mock_hunter, \
         patch("app.services.customer_enrichment_service._step_apollo", new_callable=AsyncMock) as mock_apollo:
        mock_lusha.return_value = [
            {"full_name": "Lusha Person", "email": "lusha@testcorp.com",
             "title": "Buyer", "source": "lusha", "confidence": 85},
        ]
        mock_clay.side_effect = Exception("Clay API timeout")
        async def _verify(db, contacts):
            for c in contacts:
                c["email_verified"] = True
                c["email_verification_status"] = "valid"
            return contacts
        mock_hunter.side_effect = _verify
        mock_apollo.return_value = []

        from app.services.customer_enrichment_service import enrich_customer_account
        result = await enrich_customer_account(company_with_domain.id, db_session, force=True)

        assert result["ok"] is True
        assert result["contacts_added"] == 1
        assert "lusha" in result["sources_used"]
        assert "clay" not in result["sources_used"]


@pytest.mark.asyncio
async def test_clay_dedup_with_lusha(db_session, company_with_domain, _mock_settings):
    """If Lusha and Clay return the same email, only one contact is saved."""
    with patch("app.services.customer_enrichment_service._step_lusha", new_callable=AsyncMock) as mock_lusha, \
         patch("app.services.customer_enrichment_service._step_clay", new_callable=AsyncMock) as mock_clay, \
         patch("app.services.customer_enrichment_service._step_hunter_verify", new_callable=AsyncMock) as mock_hunter, \
         patch("app.services.customer_enrichment_service._step_apollo", new_callable=AsyncMock) as mock_apollo:
        # Same email from both sources
        mock_lusha.return_value = [
            {"full_name": "Jane Doe", "email": "jane@testcorp.com",
             "title": "Buyer", "phone": "+1-555-0100", "phone_type": "direct_dial",
             "source": "lusha", "confidence": 85},
        ]
        mock_clay.return_value = [
            {"full_name": "Jane Doe", "email": "jane@testcorp.com",
             "title": "Procurement Manager", "source": "clay", "confidence": 70},
        ]
        async def _verify(db, contacts):
            for c in contacts:
                c["email_verified"] = True
                c["email_verification_status"] = "valid"
            return contacts
        mock_hunter.side_effect = _verify
        mock_apollo.return_value = []

        from app.services.customer_enrichment_service import enrich_customer_account
        result = await enrich_customer_account(company_with_domain.id, db_session, force=True)

        assert result["ok"] is True
        assert result["contacts_added"] == 1  # Deduped — only one saved

        # Lusha record should be kept (higher priority, listed first)
        site = db_session.query(CustomerSite).filter_by(company_id=company_with_domain.id).first()
        contacts = db_session.query(SiteContact).filter_by(customer_site_id=site.id).all()
        assert len(contacts) == 1
        assert contacts[0].enrichment_source == "lusha"


@pytest.mark.asyncio
async def test_step_clay_credit_check(db_session, _mock_settings):
    """_step_clay should not call API if credits exhausted."""
    with patch("app.services.customer_enrichment_service.can_use_credits", return_value=False):
        from app.services.customer_enrichment_service import _step_clay
        result = await _step_clay(db_session, "test.com", "Test Corp", 5)
        assert result == []


@pytest.mark.asyncio
async def test_step_clay_passes_title_keywords(db_session, _mock_settings):
    """_step_clay should pass role-based title keywords, not company name."""
    with patch("app.services.customer_enrichment_service.can_use_credits", return_value=True), \
         patch("app.services.customer_enrichment_service.record_credit_usage"), \
         patch("app.connectors.clay_client.find_contacts", new_callable=AsyncMock) as mock_clay:
        mock_clay.return_value = []

        from app.services.customer_enrichment_service import _step_clay
        await _step_clay(db_session, "acme.com", "Acme Electronics", 5)

        mock_clay.assert_awaited_once()
        call_args = mock_clay.call_args
        # First positional arg is domain, second is title filter
        title_filter = call_args[0][1]
        assert "purchasing" in title_filter
        assert "Acme Electronics" not in title_filter


@pytest.mark.asyncio
async def test_step_clay_confidence_is_70(db_session, _mock_settings):
    """Clay contacts should have confidence 70 (co-primary, not gap-filler 50)."""
    with patch("app.services.customer_enrichment_service.can_use_credits", return_value=True), \
         patch("app.services.customer_enrichment_service.record_credit_usage"), \
         patch("app.connectors.clay_client.find_contacts", new_callable=AsyncMock) as mock_clay:
        mock_clay.return_value = [
            {"full_name": "Test Person", "email": "test@acme.com", "title": "Buyer"},
        ]

        from app.services.customer_enrichment_service import _step_clay
        result = await _step_clay(db_session, "acme.com", "Acme", 5)

        assert len(result) == 1
        assert result[0]["confidence"] == 70
        assert result[0]["source"] == "clay"
