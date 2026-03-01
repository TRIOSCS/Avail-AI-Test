"""Tests for customer_enrichment_service — waterfall orchestrator.

Covers: enrich_customer_account, get_enrichment_gaps, helper functions,
cooldown, force bypass, Apollo primary discovery, Lusha phone enrichment.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.crm import Company, CustomerSite, SiteContact
from tests.conftest import engine  # noqa: F401


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
        db_session.add(
            SiteContact(
                customer_site_id=site.id,
                full_name=f"Contact {i}",
                email=f"c{i}@testcorp.com",
            )
        )
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

    with (
        patch("app.services.customer_enrichment_service._step_apollo", new_callable=AsyncMock) as mock_apollo,
        patch("app.services.customer_enrichment_service._step_hunter_verify", new_callable=AsyncMock) as mock_hunter,
        patch(
            "app.services.customer_enrichment_service._step_lusha_phones", new_callable=AsyncMock
        ) as mock_lusha_phones,
    ):
        mock_apollo.return_value = [
            {
                "full_name": "Test Person",
                "email": "test@testcorp.com",
                "title": "Buyer",
                "phone": None,
                "phone_type": None,
                "source": "apollo",
                "confidence": "medium",
                "enrichment_field_sources": {"email": "apollo", "name": "apollo", "phone": None},
            },
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
    with (
        patch("app.services.customer_enrichment_service._step_apollo", new_callable=AsyncMock) as mock_apollo,
        patch("app.services.customer_enrichment_service._step_hunter_verify", new_callable=AsyncMock) as mock_hunter,
        patch(
            "app.services.customer_enrichment_service._step_lusha_phones", new_callable=AsyncMock
        ) as mock_lusha_phones,
    ):
        mock_apollo.return_value = [
            {
                "full_name": "Alice Buyer",
                "email": "alice@testcorp.com",
                "title": "Procurement Manager",
                "phone": None,
                "phone_type": None,
                "source": "apollo",
                "confidence": "medium",
                "enrichment_field_sources": {"email": "apollo", "name": "apollo", "phone": None},
            },
            {
                "full_name": "Bob Tech",
                "email": "bob@testcorp.com",
                "title": "Sr Engineer",
                "phone": None,
                "phone_type": None,
                "source": "apollo",
                "confidence": "medium",
                "enrichment_field_sources": {"email": "apollo", "name": "apollo", "phone": None},
            },
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
    from app.models.auth import User
    from app.services.customer_enrichment_service import get_enrichment_gaps

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
    with (
        patch("app.services.customer_enrichment_service._step_apollo", new_callable=AsyncMock) as mock_apollo,
        patch("app.services.customer_enrichment_service._step_hunter_verify", new_callable=AsyncMock) as mock_hunter,
        patch(
            "app.services.customer_enrichment_service._step_lusha_phones", new_callable=AsyncMock
        ) as mock_lusha_phones,
    ):
        mock_apollo.return_value = [
            {
                "full_name": f"Apollo Person {i}",
                "email": f"apollo{i}@testcorp.com",
                "title": "Buyer",
                "source": "apollo",
                "confidence": "medium",
                "enrichment_field_sources": {"email": "apollo", "name": "apollo", "phone": None},
            }
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
        {
            "full_name": "Has Phone",
            "email": "hasphone@testcorp.com",
            "title": "Buyer",
            "phone": "+1-555-1111",
            "phone_type": "direct_dial",
            "source": "apollo",
            "enrichment_field_sources": {"email": "apollo", "name": "apollo", "phone": "apollo"},
        },
        {
            "full_name": "No Phone",
            "email": "nophone@testcorp.com",
            "title": "Engineer",
            "phone": None,
            "phone_type": None,
            "source": "apollo",
            "enrichment_field_sources": {"email": "apollo", "name": "apollo", "phone": None},
        },
    ]

    with (
        patch("app.services.customer_enrichment_service.can_use_credits", return_value=True),
        patch("app.services.customer_enrichment_service.record_credit_usage"),
        patch("app.connectors.lusha_client.find_person", new_callable=AsyncMock) as mock_find,
    ):
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
    with (
        patch("app.services.customer_enrichment_service._step_apollo", new_callable=AsyncMock) as mock_apollo,
        patch(
            "app.services.customer_enrichment_service._step_lusha_discovery", new_callable=AsyncMock
        ) as mock_lusha_disc,
        patch("app.services.customer_enrichment_service._step_hunter_verify", new_callable=AsyncMock) as mock_hunter,
        patch(
            "app.services.customer_enrichment_service._step_lusha_phones", new_callable=AsyncMock
        ) as mock_lusha_phones,
    ):
        mock_apollo.side_effect = Exception("Apollo API timeout")
        mock_lusha_disc.return_value = [
            {
                "full_name": "Lusha Person",
                "email": "lusha@testcorp.com",
                "title": "Buyer",
                "source": "lusha",
                "confidence": 85,
            },
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
    with (
        patch("app.services.customer_enrichment_service._step_apollo", new_callable=AsyncMock) as mock_apollo,
        patch("app.services.customer_enrichment_service._step_hunter_verify", new_callable=AsyncMock) as mock_hunter,
        patch(
            "app.services.customer_enrichment_service._step_lusha_phones", new_callable=AsyncMock
        ) as mock_lusha_phones,
    ):
        mock_apollo.return_value = [
            {
                "full_name": "Jane Doe",
                "email": "jane@testcorp.com",
                "title": "Buyer",
                "phone": None,
                "phone_type": None,
                "source": "apollo",
                "confidence": "medium",
                "enrichment_field_sources": {"email": "apollo", "name": "apollo", "phone": None},
            },
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
    with (
        patch("app.services.customer_enrichment_service.can_use_credits") as mock_can,
        patch("app.services.customer_enrichment_service.record_credit_usage"),
        patch("app.connectors.lusha_client.find_person", new_callable=AsyncMock) as mock_find,
    ):
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
    with (
        patch("app.services.customer_enrichment_service._step_apollo", new_callable=AsyncMock) as mock_apollo,
        patch(
            "app.services.customer_enrichment_service._step_lusha_discovery", new_callable=AsyncMock
        ) as mock_lusha_disc,
        patch("app.services.customer_enrichment_service._step_hunter_verify", new_callable=AsyncMock) as mock_hunter,
        patch(
            "app.services.customer_enrichment_service._step_lusha_phones", new_callable=AsyncMock
        ) as mock_lusha_phones,
    ):
        mock_apollo.return_value = []
        mock_lusha_disc.return_value = [
            {
                "full_name": "Lusha Buyer",
                "email": "buyer@testcorp.com",
                "title": "Purchasing Manager",
                "source": "lusha",
                "confidence": 85,
            },
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
        {"full_name": "Has DD", "email": "dd@test.com", "phone": "+1-555-0001", "phone_type": "direct_dial"},
        {"full_name": "Has Mobile", "email": "mob@test.com", "phone": "+1-555-0002", "phone_type": "mobile"},
        {"full_name": "No Phone", "email": "nophone@test.com", "phone": None, "phone_type": None},
        {"full_name": "Work Phone", "email": "work@test.com", "phone": "+1-555-0003", "phone_type": "work"},
    ]

    with (
        patch("app.services.customer_enrichment_service.can_use_credits", return_value=True),
        patch("app.services.customer_enrichment_service.record_credit_usage"),
        patch("app.connectors.lusha_client.find_person", new_callable=AsyncMock) as mock_find,
    ):
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


# ── _save_contact unit tests ─────────────────────────────────────


def test_save_contact_no_email(db_session, company_with_domain):
    """_save_contact returns None when email is empty."""
    from app.services.customer_enrichment_service import _ensure_site, _save_contact

    company = db_session.get(Company, company_with_domain.id)
    site = _ensure_site(db_session, company)

    result = _save_contact(db_session, site, {"full_name": "No Email"}, "apollo")
    assert result is None

    result2 = _save_contact(db_session, site, {"full_name": "Empty Email", "email": ""}, "apollo")
    assert result2 is None


def test_save_contact_updates_existing(db_session, company_with_domain):
    """_save_contact updates phone, linkedin_url, title on existing contact."""
    from app.services.customer_enrichment_service import _ensure_site, _save_contact

    company = db_session.get(Company, company_with_domain.id)
    site = _ensure_site(db_session, company)

    # Save initial contact without phone/linkedin/title
    contact1 = {
        "full_name": "Jane Doe",
        "email": "jane@testcorp.com",
        "source": "apollo",
    }
    sc = _save_contact(db_session, site, contact1, "apollo")
    db_session.flush()
    assert sc is not None
    assert sc.phone is None
    assert sc.linkedin_url is None
    assert sc.title is None

    # Save again with phone, linkedin, title → should update existing
    contact2 = {
        "full_name": "Jane Doe",
        "email": "jane@testcorp.com",
        "phone": "+1-555-0001",
        "phone_type": "direct_dial",
        "linkedin_url": "https://linkedin.com/in/janedoe",
        "title": "VP of Procurement",
        "source": "lusha",
        "enrichment_field_sources": {"phone": "lusha"},
    }
    sc2 = _save_contact(db_session, site, contact2, "lusha")
    db_session.flush()

    assert sc2 is sc  # Same ORM object
    assert sc.phone == "+1-555-0001"
    assert sc.phone_verified is True  # direct_dial → True
    assert sc.linkedin_url == "https://linkedin.com/in/janedoe"
    assert sc.title == "VP of Procurement"
    assert sc.contact_role == "decision_maker"
    efs = sc.enrichment_field_sources or {}
    assert efs.get("phone") == "lusha"


def test_save_contact_new_contact(db_session, company_with_domain):
    """_save_contact creates new contact with all fields + enrichment_field_sources."""
    from app.services.customer_enrichment_service import _ensure_site, _save_contact

    company = db_session.get(Company, company_with_domain.id)
    site = _ensure_site(db_session, company)

    contact = {
        "full_name": "Bob Builder",
        "email": "bob@testcorp.com",
        "phone": "+1-555-9999",
        "phone_type": "mobile",
        "title": "Senior Engineer",
        "linkedin_url": "https://linkedin.com/in/bob",
        "source": "apollo",
        "enrichment_field_sources": {"email": "apollo", "name": "apollo", "phone": "apollo"},
    }
    sc = _save_contact(db_session, site, contact, "apollo")
    db_session.flush()

    assert sc is not None
    assert sc.full_name == "Bob Builder"
    assert sc.email == "bob@testcorp.com"
    assert sc.phone == "+1-555-9999"
    assert sc.phone_verified is True  # mobile → True
    assert sc.title == "Senior Engineer"
    assert sc.contact_role == "technical"
    assert sc.linkedin_url == "https://linkedin.com/in/bob"
    assert sc.enrichment_source == "apollo"
    assert sc.enrichment_field_sources["email"] == "apollo"
    assert sc.enrichment_field_sources["phone"] == "apollo"


# ── _step_lusha_phones exception test ────────────────────────────


@pytest.mark.asyncio
async def test_step_lusha_phones_exception_handled(db_session, _mock_settings):
    """find_person raises → contact kept without phone, exception swallowed."""
    contacts = [
        {"full_name": "Crash Test", "email": "crash@test.com", "phone": None, "phone_type": None},
    ]
    with (
        patch("app.services.customer_enrichment_service.can_use_credits", return_value=True),
        patch("app.services.customer_enrichment_service.record_credit_usage"),
        patch("app.connectors.lusha_client.find_person", new_callable=AsyncMock) as mock_find,
    ):
        mock_find.side_effect = Exception("Lusha API error")

        from app.services.customer_enrichment_service import _step_lusha_phones

        result = await _step_lusha_phones(db_session, contacts, "test.com")

        assert len(result) == 1
        assert result[0]["phone"] is None  # Phone not set due to exception


# ── _step_lusha_discovery tests ──────────────────────────────────


@pytest.mark.asyncio
async def test_step_lusha_discovery_skipped_not_needed(db_session, _mock_settings):
    """needed=0 → returns [] immediately."""
    from app.services.customer_enrichment_service import _step_lusha_discovery

    result = await _step_lusha_discovery(db_session, "test.com", "Test Corp", needed=0)
    assert result == []


@pytest.mark.asyncio
async def test_step_lusha_discovery_credits_exhausted(db_session, _mock_settings):
    """can_use_credits returns False → returns []."""
    with patch("app.services.customer_enrichment_service.can_use_credits", return_value=False):
        from app.services.customer_enrichment_service import _step_lusha_discovery

        result = await _step_lusha_discovery(db_session, "test.com", "Test Corp", needed=5)
        assert result == []


@pytest.mark.asyncio
async def test_step_lusha_discovery_success(db_session, _mock_settings):
    """Successful search returns contacts and records credit usage."""
    with (
        patch("app.services.customer_enrichment_service.can_use_credits", return_value=True),
        patch("app.services.customer_enrichment_service.record_credit_usage") as mock_record,
        patch("app.connectors.lusha_client.search_contacts", new_callable=AsyncMock) as mock_search,
    ):
        mock_search.return_value = [
            {"full_name": "Lusha Person", "email": "lusha@test.com", "title": "Buyer"},
        ]

        from app.services.customer_enrichment_service import _step_lusha_discovery

        result = await _step_lusha_discovery(db_session, "test.com", "Test Corp", needed=3)

        assert len(result) == 1
        assert result[0]["email"] == "lusha@test.com"
        mock_record.assert_called_once_with(db_session, "lusha", 1)


@pytest.mark.asyncio
async def test_step_lusha_discovery_exception(db_session, _mock_settings):
    """search_contacts raises → returns []."""
    with (
        patch("app.services.customer_enrichment_service.can_use_credits", return_value=True),
        patch("app.connectors.lusha_client.search_contacts", new_callable=AsyncMock) as mock_search,
    ):
        mock_search.side_effect = Exception("Lusha search blew up")

        from app.services.customer_enrichment_service import _step_lusha_discovery

        result = await _step_lusha_discovery(db_session, "test.com", "Test Corp", needed=3)
        assert result == []


# ── _step_hunter_verify tests ────────────────────────────────────


@pytest.mark.asyncio
async def test_step_hunter_verify_no_email(db_session, _mock_settings):
    """Contact without email → skipped entirely (not in verified list)."""
    contacts = [
        {"full_name": "No Email", "email": None},
    ]
    with patch("app.connectors.hunter_client.verify_email", new_callable=AsyncMock) as mock_verify:
        from app.services.customer_enrichment_service import _step_hunter_verify

        result = await _step_hunter_verify(db_session, contacts)

        assert len(result) == 0
        mock_verify.assert_not_awaited()


@pytest.mark.asyncio
async def test_step_hunter_verify_credits_exhausted(db_session, _mock_settings):
    """Credits run out → contact kept with email_verified=False, unverified status."""
    contacts = [
        {"full_name": "Person 1", "email": "p1@test.com"},
    ]
    with (
        patch("app.services.customer_enrichment_service.can_use_credits", return_value=False),
        patch("app.connectors.hunter_client.verify_email", new_callable=AsyncMock) as mock_verify,
    ):
        from app.services.customer_enrichment_service import _step_hunter_verify

        result = await _step_hunter_verify(db_session, contacts)

        assert len(result) == 1
        assert result[0]["email_verified"] is False
        assert result[0]["email_verification_status"] == "unverified"
        mock_verify.assert_not_awaited()


@pytest.mark.asyncio
async def test_step_hunter_verify_valid(db_session, _mock_settings):
    """Valid email → email_verified=True, kept in result."""
    contacts = [
        {"full_name": "Valid Person", "email": "valid@test.com"},
    ]
    with (
        patch("app.services.customer_enrichment_service.can_use_credits", return_value=True),
        patch("app.services.customer_enrichment_service.record_credit_usage"),
        patch("app.connectors.hunter_client.verify_email", new_callable=AsyncMock) as mock_verify,
    ):
        mock_verify.return_value = {"status": "valid", "score": 95}

        from app.services.customer_enrichment_service import _step_hunter_verify

        result = await _step_hunter_verify(db_session, contacts)

        assert len(result) == 1
        assert result[0]["email_verified"] is True
        assert result[0]["email_verification_status"] == "valid"


@pytest.mark.asyncio
async def test_step_hunter_verify_accept_all(db_session, _mock_settings):
    """accept_all status → email_verified=True, kept in result."""
    contacts = [
        {"full_name": "Accept All", "email": "catch@test.com"},
    ]
    with (
        patch("app.services.customer_enrichment_service.can_use_credits", return_value=True),
        patch("app.services.customer_enrichment_service.record_credit_usage"),
        patch("app.connectors.hunter_client.verify_email", new_callable=AsyncMock) as mock_verify,
    ):
        mock_verify.return_value = {"status": "accept_all", "score": 50}

        from app.services.customer_enrichment_service import _step_hunter_verify

        result = await _step_hunter_verify(db_session, contacts)

        assert len(result) == 1
        assert result[0]["email_verified"] is True
        assert result[0]["email_verification_status"] == "accept_all"


@pytest.mark.asyncio
async def test_step_hunter_verify_rejected(db_session, _mock_settings):
    """Invalid status → contact dropped from result."""
    contacts = [
        {"full_name": "Bad Email", "email": "bad@test.com"},
    ]
    with (
        patch("app.services.customer_enrichment_service.can_use_credits", return_value=True),
        patch("app.services.customer_enrichment_service.record_credit_usage"),
        patch("app.connectors.hunter_client.verify_email", new_callable=AsyncMock) as mock_verify,
    ):
        mock_verify.return_value = {"status": "invalid", "score": 10}

        from app.services.customer_enrichment_service import _step_hunter_verify

        result = await _step_hunter_verify(db_session, contacts)

        assert len(result) == 0  # Rejected — not in verified list


@pytest.mark.asyncio
async def test_step_hunter_verify_null_result(db_session, _mock_settings):
    """verify_email returns None → kept with email_verified=False, unknown status."""
    contacts = [
        {"full_name": "Unknown Result", "email": "unknown@test.com"},
    ]
    with (
        patch("app.services.customer_enrichment_service.can_use_credits", return_value=True),
        patch("app.services.customer_enrichment_service.record_credit_usage"),
        patch("app.connectors.hunter_client.verify_email", new_callable=AsyncMock) as mock_verify,
    ):
        mock_verify.return_value = None

        from app.services.customer_enrichment_service import _step_hunter_verify

        result = await _step_hunter_verify(db_session, contacts)

        assert len(result) == 1
        assert result[0]["email_verified"] is False
        assert result[0]["email_verification_status"] == "unknown"


# ── _step_apollo tests ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_step_apollo_credits_exhausted(db_session, _mock_settings):
    """can_use_credits returns False → returns []."""
    with patch("app.services.customer_enrichment_service.can_use_credits", return_value=False):
        from app.services.customer_enrichment_service import _step_apollo

        result = await _step_apollo(db_session, "test.com", "Test Corp", needed=5)
        assert result == []


@pytest.mark.asyncio
async def test_step_apollo_success(db_session, _mock_settings):
    """Successful search → maps results with enrichment_field_sources."""
    with (
        patch("app.services.customer_enrichment_service.can_use_credits", return_value=True),
        patch("app.services.customer_enrichment_service.record_credit_usage") as mock_record,
        patch("app.connectors.apollo_client.search_contacts", new_callable=AsyncMock) as mock_search,
    ):
        mock_search.return_value = [
            {
                "full_name": "Apollo Person",
                "title": "Buyer",
                "email": "apollo@test.com",
                "phone": "+1-555-1234",
                "phone_type": "direct_dial",
                "linkedin_url": "https://linkedin.com/in/apollo",
                "confidence": "high",
            },
            {
                "full_name": "Apollo NoPhone",
                "title": "Engineer",
                "email": "nophone@test.com",
                "phone": None,
                "phone_type": None,
                "linkedin_url": None,
            },
        ]

        from app.services.customer_enrichment_service import _step_apollo

        result = await _step_apollo(db_session, "test.com", "Test Corp", needed=5)

        assert len(result) == 2
        mock_record.assert_called_once_with(db_session, "apollo", 1)

        # First contact: has phone
        assert result[0]["full_name"] == "Apollo Person"
        assert result[0]["source"] == "apollo"
        assert result[0]["enrichment_field_sources"]["email"] == "apollo"
        assert result[0]["enrichment_field_sources"]["phone"] == "apollo"

        # Second contact: no phone
        assert result[1]["enrichment_field_sources"]["phone"] is None


@pytest.mark.asyncio
async def test_step_apollo_exception(db_session, _mock_settings):
    """apollo_search raises → returns []."""
    with (
        patch("app.services.customer_enrichment_service.can_use_credits", return_value=True),
        patch("app.connectors.apollo_client.search_contacts", new_callable=AsyncMock) as mock_search,
    ):
        mock_search.side_effect = Exception("Apollo timeout")

        from app.services.customer_enrichment_service import _step_apollo

        result = await _step_apollo(db_session, "test.com", "Test Corp", needed=5)
        assert result == []


# ── enrich_customer_account integration-level tests ──────────────


@pytest.mark.asyncio
async def test_enrich_already_complete(db_session, company_with_domain, _mock_settings):
    """needed <= 0 and not force → status set to complete, 0 contacts added."""
    # Fill up contacts so needed = 0
    site = db_session.query(CustomerSite).filter_by(company_id=company_with_domain.id).first()
    for i in range(5):
        db_session.add(
            SiteContact(
                customer_site_id=site.id,
                full_name=f"Existing {i}",
                email=f"existing{i}@testcorp.com",
                is_active=True,
            )
        )
    db_session.flush()

    from app.services.customer_enrichment_service import enrich_customer_account

    result = await enrich_customer_account(company_with_domain.id, db_session)

    assert result["ok"] is True
    assert result["contacts_added"] == 0
    assert result["status"] == "already_complete"
    # Verify company status was set
    db_session.refresh(company_with_domain)
    assert company_with_domain.customer_enrichment_status == "complete"


@pytest.mark.asyncio
async def test_enrich_lusha_discovery_exception(db_session, company_with_domain, _mock_settings):
    """Lusha discovery raises at outer try/except → gracefully handled, continues."""
    with (
        patch("app.services.customer_enrichment_service._step_apollo", new_callable=AsyncMock) as mock_apollo,
        patch(
            "app.services.customer_enrichment_service._step_lusha_discovery", new_callable=AsyncMock
        ) as mock_lusha_disc,
        patch("app.services.customer_enrichment_service._step_hunter_verify", new_callable=AsyncMock) as mock_hunter,
        patch(
            "app.services.customer_enrichment_service._step_lusha_phones", new_callable=AsyncMock
        ) as mock_lusha_phones,
    ):
        # Apollo returns nothing → falls through to Lusha
        mock_apollo.return_value = []
        # Lusha discovery raises (outer exception path, lines 383-385)
        mock_lusha_disc.side_effect = Exception("Lusha total failure")
        # Hunter/Lusha phones won't be called meaningfully (no contacts)
        mock_hunter.return_value = []
        mock_lusha_phones.return_value = []

        from app.services.customer_enrichment_service import enrich_customer_account

        result = await enrich_customer_account(company_with_domain.id, db_session, force=True)

        # Should not crash — gracefully handled
        assert result["ok"] is True
        assert result["contacts_added"] == 0


@pytest.mark.asyncio
async def test_enrich_invalid_contacts_filtered(db_session, company_with_domain, _mock_settings):
    """validate_contact rejects contacts with bad data, only valid ones are saved."""
    with (
        patch("app.services.customer_enrichment_service._step_apollo", new_callable=AsyncMock) as mock_apollo,
        patch("app.services.customer_enrichment_service._step_hunter_verify", new_callable=AsyncMock) as mock_hunter,
        patch(
            "app.services.customer_enrichment_service._step_lusha_phones", new_callable=AsyncMock
        ) as mock_lusha_phones,
    ):
        mock_apollo.return_value = [
            # Valid contact
            {
                "full_name": "Good Person",
                "email": "good@testcorp.com",
                "title": "Buyer",
                "phone": None,
                "phone_type": None,
                "source": "apollo",
                "confidence": "medium",
                "enrichment_field_sources": {"email": "apollo", "name": "apollo", "phone": None},
            },
            # Invalid contact — no name (will fail validate_contact)
            {
                "full_name": "",
                "email": "badname@testcorp.com",
                "title": "Buyer",
                "phone": None,
                "phone_type": None,
                "source": "apollo",
                "confidence": "medium",
                "enrichment_field_sources": {"email": "apollo", "name": "apollo", "phone": None},
            },
            # Invalid contact — bad email format
            {
                "full_name": "Bad Email",
                "email": "not-an-email",
                "title": "Buyer",
                "phone": None,
                "phone_type": None,
                "source": "apollo",
                "confidence": "medium",
                "enrichment_field_sources": {"email": "apollo", "name": "apollo", "phone": None},
            },
        ]

        async def _verify(db, contacts):
            for c in contacts:
                c["email_verified"] = True
                c["email_verification_status"] = "valid"
            return contacts

        mock_hunter.side_effect = _verify
        mock_lusha_phones.side_effect = lambda db, contacts, domain: contacts

        from app.services.customer_enrichment_service import enrich_customer_account

        result = await enrich_customer_account(company_with_domain.id, db_session, force=True)

        assert result["ok"] is True
        # Only the "Good Person" should be saved
        assert result["contacts_added"] == 1


@pytest.mark.asyncio
async def test_enrich_final_status_complete(db_session, company_with_domain, _mock_settings):
    """After saving contacts, final_needed=0 → status set to 'complete'."""
    # Target is 5 contacts. Pre-fill 4, Apollo adds 1 → total 5 → complete.
    # Test session uses autoflush=False, so the newly-added contact isn't visible
    # to the final _contacts_needed SQL query. We mock _contacts_needed so its
    # second call (the final check) returns 0 to exercise the "complete" branch.
    site = db_session.query(CustomerSite).filter_by(company_id=company_with_domain.id).first()
    for i in range(4):
        db_session.add(
            SiteContact(
                customer_site_id=site.id,
                full_name=f"Pre-existing {i}",
                email=f"pre{i}@testcorp.com",
                is_active=True,
            )
        )
    db_session.flush()

    # First call returns 1 (need 1 more), second call returns 0 (target met)
    needed_calls = iter([1, 0])

    with (
        patch("app.services.customer_enrichment_service._step_apollo", new_callable=AsyncMock) as mock_apollo,
        patch("app.services.customer_enrichment_service._step_hunter_verify", new_callable=AsyncMock) as mock_hunter,
        patch(
            "app.services.customer_enrichment_service._step_lusha_phones", new_callable=AsyncMock
        ) as mock_lusha_phones,
        patch("app.services.customer_enrichment_service._contacts_needed", side_effect=needed_calls),
    ):
        mock_apollo.return_value = [
            {
                "full_name": "Fifth Contact",
                "email": "fifth@testcorp.com",
                "title": "Buyer",
                "phone": None,
                "phone_type": None,
                "source": "apollo",
                "confidence": "medium",
                "enrichment_field_sources": {"email": "apollo", "name": "apollo", "phone": None},
            },
        ]

        async def _verify(db, contacts):
            for c in contacts:
                c["email_verified"] = True
                c["email_verification_status"] = "valid"
            return contacts

        mock_hunter.side_effect = _verify
        mock_lusha_phones.side_effect = lambda db, contacts, domain: contacts

        from app.services.customer_enrichment_service import enrich_customer_account

        result = await enrich_customer_account(company_with_domain.id, db_session, force=True)

        assert result["ok"] is True
        assert result["contacts_added"] == 1
        assert result["status"] == "complete"
        db_session.refresh(company_with_domain)
        assert company_with_domain.customer_enrichment_status == "complete"


# ── Edge-case coverage for _contacts_needed and _ensure_site ─────


def test_contacts_needed_no_sites(db_session, _mock_settings):
    """_contacts_needed returns target when company has no sites."""
    from app.services.customer_enrichment_service import _contacts_needed

    co = Company(name="No Sites Corp", domain="nosites.com", is_active=True)
    db_session.add(co)
    db_session.flush()

    needed = _contacts_needed(db_session, co.id, 5)
    assert needed == 5


def test_ensure_site_creates_when_missing(db_session, _mock_settings):
    """_ensure_site creates a new site when company has none."""
    from app.services.customer_enrichment_service import _ensure_site

    co = Company(name="Empty Corp", domain="empty.com", is_active=True)
    db_session.add(co)
    db_session.flush()

    # No sites exist yet
    assert db_session.query(CustomerSite).filter_by(company_id=co.id).count() == 0

    site = _ensure_site(db_session, co)
    assert site is not None
    assert site.site_name == "Empty Corp - HQ"
    assert site.company_id == co.id
    # Should be persisted (flushed)
    assert db_session.query(CustomerSite).filter_by(company_id=co.id).count() == 1
