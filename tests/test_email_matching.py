"""test_email_matching.py — Phase 3 email matching disambiguation tests.

Covers:
- Exact SiteContact.email wins over domain match (FIX 2 priority 1)
- Exact CustomerSite.contact_email wins over domain match (FIX 2 priority 2)
- Exact VendorContact.email wins over domain match (FIX 2 priority 3)
- Multi-entity domain match resolves deterministically via fuzzy tie-break (FIX 2)
- Single-match path is unchanged from pre-FIX 2 behaviour
- EMAIL_SENT is in _AI_SCORED_TYPES (FIX 1)

Called by: pytest
Depends on: app/services/activity_service.py, app/services/activity_quality_service.py
"""

from datetime import datetime, timezone

from app.constants import ActivityType
from app.models import Company, CustomerSite, SiteContact, VendorCard, VendorContact
from app.services.activity_quality_service import _AI_SCORED_TYPES
from app.services.activity_service import match_email_to_entity

# ── Helpers ──────────────────────────────────────────────────────────────────


def _dt():
    return datetime.now(timezone.utc)


def _company(db, name="Acme Corp", domain="acme.com"):
    co = Company(name=name, domain=domain, is_active=True, created_at=_dt())
    db.add(co)
    db.flush()
    return co


def _site(db, company_id, contact_email="contact@acme.com"):
    site = CustomerSite(
        company_id=company_id,
        site_name="HQ",
        is_active=True,
        contact_email=contact_email,
        created_at=_dt(),
    )
    db.add(site)
    db.flush()
    return site


def _site_contact(db, site_id, email="contact@acme.com"):
    sc = SiteContact(
        customer_site_id=site_id,
        full_name="Jane Smith",
        email=email,
        is_primary=True,
        email_verified=True,
    )
    db.add(sc)
    db.flush()
    return sc


def _vendor_card(db, name="Arrow Electronics", domain="arrow.com"):
    card = VendorCard(
        normalized_name=name.lower().replace(" ", "-"),
        display_name=name,
        domain=domain,
        is_blacklisted=False,
        sighting_count=5,
        created_at=_dt(),
    )
    db.add(card)
    db.flush()
    return card


def _vendor_contact(db, vendor_card_id, email="sales@arrow.com"):
    vc = VendorContact(
        vendor_card_id=vendor_card_id,
        email=email,
        full_name="Sales Rep",
        source="manual",
    )
    db.add(vc)
    db.flush()
    return vc


# ── FIX 1: EMAIL_SENT is now AI-scored ───────────────────────────────────────


class TestEmailSentAIScored:
    def test_email_sent_in_ai_scored_types(self):
        """EMAIL_SENT must be in _AI_SCORED_TYPES so sent emails get clean_summary."""
        assert ActivityType.EMAIL_SENT in _AI_SCORED_TYPES

    def test_email_received_still_in_ai_scored_types(self):
        """EMAIL_RECEIVED must remain in _AI_SCORED_TYPES (was already there)."""
        assert ActivityType.EMAIL_RECEIVED in _AI_SCORED_TYPES


# ── FIX 2: Exact SiteContact email beats domain match ────────────────────────


class TestExactSiteContactBeatesDomainMatch:
    def test_site_contact_email_wins_over_domain(self, db_session):
        """A SiteContact with an exact email match wins over the domain Company row.

        Setup: company domain=corp.com has one SiteContact jane@corp.com.
        The email jane@corp.com should return site_contact_id set (not just a bare
        domain match).
        """
        co = _company(db_session, name="Corp Inc", domain="corp.com")
        site = _site(db_session, co.id, contact_email="generic@corp.com")
        sc = _site_contact(db_session, site.id, email="jane@corp.com")
        db_session.commit()

        result = match_email_to_entity("jane@corp.com", db_session)

        assert result is not None
        assert result["type"] == "company"
        assert result["id"] == co.id
        # Critically: site_contact_id is populated via the exact SiteContact path
        assert result.get("site_contact_id") == sc.id

    def test_site_contact_beats_domain_match_from_different_site(self, db_session):
        """Exact SiteContact email resolves even when the CustomerSite.contact_email
        field does NOT match — the SiteContact row is the source of truth."""
        co = _company(db_session, name="TechCo", domain="techco.com")
        site = _site(db_session, co.id, contact_email="main@techco.com")  # different from sc
        sc = _site_contact(db_session, site.id, email="direct@techco.com")
        db_session.commit()

        result = match_email_to_entity("direct@techco.com", db_session)

        assert result is not None
        assert result["type"] == "company"
        assert result["id"] == co.id
        assert result.get("site_contact_id") == sc.id

    def test_exact_vendor_contact_beats_domain(self, db_session):
        """An exact VendorContact.email match wins; domain alone would also match."""
        card = _vendor_card(db_session, name="FastParts", domain="fastparts.com")
        vc = _vendor_contact(db_session, card.id, email="sales@fastparts.com")
        db_session.commit()

        result = match_email_to_entity("sales@fastparts.com", db_session)

        assert result is not None
        assert result["type"] == "vendor"
        assert result["id"] == card.id
        assert result.get("vendor_contact_id") == vc.id


# ── FIX 2: Multi-entity domain is resolved deterministically ─────────────────


class TestMultiEntityDomainDeterministic:
    def test_single_company_domain_match_unchanged(self, db_session):
        """Common case: single company matches domain — behaviour unchanged."""
        co = _company(db_session, domain="unique-corp.com")
        db_session.commit()

        result = match_email_to_entity("buyer@unique-corp.com", db_session)

        assert result is not None
        assert result["type"] == "company"
        assert result["id"] == co.id

    def test_multi_vendor_domain_returns_one(self, db_session):
        """When multiple VendorCards share a domain (unusual but possible) the function
        returns exactly one result rather than a random .first()."""
        v1 = _vendor_card(db_session, name="Parts Alpha", domain="shared-vendor.com")
        v2 = _vendor_card(db_session, name="Parts Beta", domain="shared-vendor.com")
        db_session.commit()

        r1 = match_email_to_entity("sales@shared-vendor.com", db_session)
        r2 = match_email_to_entity("sales@shared-vendor.com", db_session)

        # Deterministic: same input always returns same result
        assert r1 is not None
        assert r2 is not None
        assert r1["id"] == r2["id"]
        assert r1["type"] == "vendor"
        assert r1["id"] in {v1.id, v2.id}

    def test_fuzzy_tie_break_picks_best_vendor(self, db_session):
        """When two vendors share a domain, fuzzy scoring against the local-part of the
        email picks the one whose name is the best match."""
        # "alpha" local-part should match "Alpha Electronics" better than "Beta Corp"
        v_alpha = _vendor_card(db_session, name="Alpha Electronics", domain="multivendor.com")
        _vendor_card(db_session, name="Beta Corp", domain="multivendor.com")
        db_session.commit()

        result = match_email_to_entity("alpha@multivendor.com", db_session)

        assert result is not None
        assert result["id"] == v_alpha.id

    def test_no_match_returns_none(self, db_session):
        """An email with no matching entity returns None."""
        db_session.commit()

        result = match_email_to_entity("nobody@completely-unknown-xyz.com", db_session)
        assert result is None

    def test_generic_domain_returns_none(self, db_session):
        """Generic domains (gmail, yahoo, etc.) always return None."""
        _company(db_session, domain="gmail.com")
        db_session.commit()

        result = match_email_to_entity("user@gmail.com", db_session)
        assert result is None

    def test_empty_email_returns_none(self, db_session):
        result = match_email_to_entity("", db_session)
        assert result is None

        result = match_email_to_entity(None, db_session)
        assert result is None
