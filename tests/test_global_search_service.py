"""tests/test_global_search_service.py — Tests for global search service.

Called by: pytest
Depends on: app.services.global_search_service, test fixtures from conftest.py
"""

import pytest

from app.models.crm import Company, CustomerSite, SiteContact
from app.models.offers import Offer
from app.models.sourcing import Requirement, Requisition
from app.models.vendors import VendorCard, VendorContact


@pytest.fixture
def search_db(db_session, test_user):
    """Seed test DB with searchable entities across all 7 types."""
    # Requisition
    req = Requisition(name="REQ-2024-LM358", customer_name="Raytheon", created_by=test_user.id)
    db_session.add(req)
    db_session.flush()

    # Company
    co = Company(name="Acme Electronics", domain="acme.com")
    db_session.add(co)
    db_session.flush()

    # Vendor (with JSON emails for JSON field search test)
    vendor = VendorCard(
        display_name="Arrow Electronics",
        normalized_name="arrow electronics",
        domain="arrow.com",
        emails=["sales@arrow.com", "support@arrow.com"],
        phones=["+1-555-0100"],
    )
    db_session.add(vendor)
    db_session.flush()

    # Vendor Contact
    vc = VendorContact(
        vendor_card_id=vendor.id,
        full_name="John Smith",
        email="john@arrow.com",
        phone="+1-555-0101",
        source="manual",
    )
    db_session.add(vc)

    # Customer Site + Site Contact
    site = CustomerSite(company_id=co.id, site_name="HQ")
    db_session.add(site)
    db_session.flush()
    sc = SiteContact(customer_site_id=site.id, full_name="Jane Doe", email="jane@acme.com")
    db_session.add(sc)

    # Requirement (part)
    part = Requirement(
        requisition_id=req.id,
        primary_mpn="LM358N",
        normalized_mpn="lm358n",
        brand="Texas Instruments",
    )
    db_session.add(part)
    db_session.flush()

    # Offer
    offer = Offer(
        requisition_id=req.id,
        requirement_id=part.id,
        vendor_name="Arrow",
        mpn="LM358N",
        qty_available=1000,
        unit_price=0.50,
    )
    db_session.add(offer)

    db_session.commit()
    return db_session


def test_fast_search_returns_structure(search_db):
    from app.services.global_search_service import fast_search

    result = fast_search("LM358", search_db)
    assert "best_match" in result
    assert "groups" in result
    assert "total_count" in result
    assert set(result["groups"].keys()) == {
        "requisitions",
        "companies",
        "vendors",
        "vendor_contacts",
        "site_contacts",
        "parts",
        "offers",
    }


def test_fast_search_finds_requisition_by_name(search_db):
    from app.services.global_search_service import fast_search

    result = fast_search("LM358", search_db)
    req_names = [r["name"] for r in result["groups"]["requisitions"]]
    assert any("LM358" in n for n in req_names)


def test_fast_search_finds_company_by_name(search_db):
    from app.services.global_search_service import fast_search

    result = fast_search("Acme", search_db)
    co_names = [r["name"] for r in result["groups"]["companies"]]
    assert "Acme Electronics" in co_names


def test_fast_search_finds_vendor_contact_by_email(search_db):
    from app.services.global_search_service import fast_search

    result = fast_search("john@arrow", search_db)
    vc_emails = [r["email"] for r in result["groups"]["vendor_contacts"]]
    assert "john@arrow.com" in vc_emails


def test_fast_search_finds_part_by_mpn(search_db):
    from app.services.global_search_service import fast_search

    result = fast_search("LM358N", search_db)
    mpns = [r["primary_mpn"] for r in result["groups"]["parts"]]
    assert "LM358N" in mpns


def test_fast_search_finds_offer_by_vendor_name(search_db):
    from app.services.global_search_service import fast_search

    result = fast_search("Arrow", search_db)
    vendor_names = [r["vendor_name"] for r in result["groups"]["offers"]]
    assert "Arrow" in vendor_names


def test_fast_search_finds_vendor_by_json_email(search_db):
    """Verify JSON array fields (emails/phones) are searchable."""
    from app.services.global_search_service import fast_search

    result = fast_search("sales@arrow", search_db)
    vendor_names = [r["display_name"] for r in result["groups"]["vendors"]]
    assert "Arrow Electronics" in vendor_names


def test_fast_search_empty_query_returns_empty(search_db):
    from app.services.global_search_service import fast_search

    result = fast_search("", search_db)
    assert result["total_count"] == 0


def test_fast_search_short_query_returns_empty(search_db):
    from app.services.global_search_service import fast_search

    result = fast_search("a", search_db)
    assert result["total_count"] == 0


def test_fast_search_respects_limit(search_db):
    from app.services.global_search_service import fast_search

    result = fast_search("LM358", search_db)
    for group in result["groups"].values():
        assert len(group) <= 5


def test_fast_search_best_match_present(search_db):
    from app.services.global_search_service import fast_search

    result = fast_search("LM358N", search_db)
    assert result["best_match"] is not None
    assert "type" in result["best_match"]
    assert "id" in result["best_match"]


def test_fast_search_special_chars_safe(search_db):
    """SQL injection / wildcard chars don't cause errors."""
    from app.services.global_search_service import fast_search

    result = fast_search("100%", search_db)
    assert result["total_count"] == 0  # no match, but no error

    result = fast_search("test_underscore", search_db)
    assert result["total_count"] == 0

    result = fast_search("O'Reilly", search_db)
    assert result["total_count"] == 0
