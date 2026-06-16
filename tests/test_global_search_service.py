"""tests/test_global_search_service.py — Tests for global search service.

Called by: pytest
Depends on: app.services.global_search_service, test fixtures from conftest.py
"""

import pytest

from app.models.crm import Company, CustomerSite, SiteContact
from app.models.offers import Offer
from app.models.sourcing import Requirement, Requisition
from app.models.vendors import VendorCard, VendorContact
from app.services.global_search_service import fast_search


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
    result = fast_search("LM358", search_db)
    req_names = [r["name"] for r in result["groups"]["requisitions"]]
    assert any("LM358" in n for n in req_names)


@pytest.mark.parametrize(
    ("query", "group", "field", "expected"),
    [
        ("Acme", "companies", "name", "Acme Electronics"),
        ("john@arrow", "vendor_contacts", "email", "john@arrow.com"),
        ("LM358N", "parts", "primary_mpn", "LM358N"),
        ("Arrow", "offers", "vendor_name", "Arrow"),
        # JSON array fields (emails/phones) are searchable.
        ("sales@arrow", "vendors", "display_name", "Arrow Electronics"),
    ],
    ids=["company_name", "vendor_contact_email", "part_mpn", "offer_vendor_name", "vendor_json_email"],
)
def test_fast_search_finds_entity_by_field(search_db, query, group, field, expected):
    result = fast_search(query, search_db)
    values = [r[field] for r in result["groups"][group]]
    assert expected in values


@pytest.mark.parametrize("query", ["", "a"], ids=["empty", "short"])
def test_fast_search_too_short_returns_empty(search_db, query):
    result = fast_search(query, search_db)
    assert result["total_count"] == 0


def test_fast_search_respects_limit(search_db):
    result = fast_search("LM358", search_db)
    for group in result["groups"].values():
        assert len(group) <= 5


def test_fast_search_best_match_present(search_db):
    result = fast_search("LM358N", search_db)
    assert result["best_match"] is not None
    assert "type" in result["best_match"]
    assert "id" in result["best_match"]


@pytest.mark.parametrize("query", ["100%", "test_underscore", "O'Reilly"])
def test_fast_search_special_chars_safe(search_db, query):
    """SQL injection / wildcard chars don't cause errors (no match, but no error)."""
    result = fast_search(query, search_db)
    assert result["total_count"] == 0
