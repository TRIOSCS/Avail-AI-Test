"""tests/test_global_search_service.py — Tests for global search service.

Called by: pytest
Depends on: app.services.global_search_service, test fixtures from conftest.py
"""

import pytest

from app.models.crm import Company, CustomerSite, SiteContact
from app.models.intelligence import MaterialCard
from app.models.offers import Offer
from app.models.sourcing import Requirement, Requisition, Sighting
from app.models.vendors import VendorCard, VendorContact
from app.services.global_search_service import fast_search

EXPECTED_GROUPS = {
    "requisitions",
    "companies",
    "vendors",
    "vendor_contacts",
    "site_contacts",
    "parts",
    "offers",
    "material_cards",
    "sightings",
}


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

    # Material card (material hub) — keyed on normalized MPN
    mc = MaterialCard(
        normalized_mpn="lm358n",
        display_mpn="LM358N",
        manufacturer="Texas Instruments",
        description="Dual operational amplifier",
    )
    db_session.add(mc)
    db_session.flush()

    # Requirement (part)
    part = Requirement(
        requisition_id=req.id,
        material_card_id=mc.id,
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
        vendor_card_id=vendor.id,
        vendor_name="Arrow",
        mpn="LM358N",
        normalized_mpn="lm358n",
        qty_available=1000,
        unit_price=0.50,
    )
    db_session.add(offer)

    # Sighting — vendor seen offering the part on the requirement
    sighting = Sighting(
        requirement_id=part.id,
        material_card_id=mc.id,
        vendor_name="Arrow Electronics",
        vendor_name_normalized="arrow electronics",
        mpn_matched="LM358N",
        normalized_mpn="lm358n",
        manufacturer="Texas Instruments",
        qty_available=500,
        unit_price=0.45,
    )
    db_session.add(sighting)

    db_session.commit()
    return db_session


def test_fast_search_returns_structure(search_db):
    result = fast_search("LM358", search_db)
    assert "best_match" in result
    assert "groups" in result
    assert "total_count" in result
    assert set(result["groups"].keys()) == EXPECTED_GROUPS


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


# ── Universal cross-entity attachment ─────────────────────────────────


def test_part_number_returns_requirement_offer_and_material_card(search_db):
    """A part number surfaces every entity it is attached to: the requirement,
    the offer, and the material-hub card."""
    result = fast_search("LM358N", search_db)
    groups = result["groups"]
    assert any(p["primary_mpn"] == "LM358N" for p in groups["parts"]), "part missing"
    assert any(o["mpn"] == "LM358N" for o in groups["offers"]), "offer missing"
    assert any(m["display_mpn"] == "LM358N" for m in groups["material_cards"]), "material card missing"


def test_part_number_returns_its_sightings(search_db):
    """A part number surfaces the sightings it appears on."""
    result = fast_search("LM358N", search_db)
    assert any(s["mpn_matched"] == "LM358N" for s in result["groups"]["sightings"]), "sighting missing"


def test_material_card_matched_by_manufacturer(search_db):
    """Material cards are searchable by manufacturer, not just MPN."""
    result = fast_search("Texas Instruments", search_db)
    assert any(m["display_mpn"] == "LM358N" for m in result["groups"]["material_cards"])


def test_normalized_mpn_match_ignores_separators(search_db):
    """Typing the MPN with stray separators still hits the normalized key."""
    result = fast_search("lm-358-n", search_db)
    assert any(m["display_mpn"] == "LM358N" for m in result["groups"]["material_cards"])
    assert any(p["primary_mpn"] == "LM358N" for p in result["groups"]["parts"])


def test_vendor_returns_its_contacts_offers_and_sightings(search_db):
    """Searching a vendor surfaces the vendor card, its contacts, offers, and
    sightings."""
    result = fast_search("Arrow", search_db)
    groups = result["groups"]
    assert any(v["display_name"] == "Arrow Electronics" for v in groups["vendors"]), "vendor missing"
    assert any(c["full_name"] == "John Smith" for c in groups["vendor_contacts"]), "contact missing"
    assert any(o["vendor_name"] == "Arrow" for o in groups["offers"]), "offer missing"
    assert any("Arrow" in (s["vendor_name"] or "") for s in groups["sightings"]), "sighting missing"


def test_vendor_surfaced_via_contact_name(search_db):
    """Typing a vendor contact's name surfaces the parent vendor card too."""
    result = fast_search("John Smith", search_db)
    assert any(v["display_name"] == "Arrow Electronics" for v in result["groups"]["vendors"]), (
        "vendor not surfaced via its contact"
    )


# ── Read-gating / authz scoping ───────────────────────────────────────


def test_restricted_role_does_not_see_others_requisition(search_db, sales_user):
    """A SALES user sees no requisition/part/offer/sighting they don't own."""
    result = fast_search("LM358", search_db, user=sales_user)
    groups = result["groups"]
    assert groups["requisitions"] == [], "restricted user saw foreign requisition"
    assert groups["parts"] == [], "restricted user saw foreign part"
    assert groups["offers"] == [], "restricted user saw foreign offer"
    assert groups["sightings"] == [], "restricted user saw foreign sighting"


def test_restricted_role_sees_own_requisition(search_db, db_session, sales_user):
    """A SALES user still sees requisitions they own."""
    own = Requisition(name="OWN-LM358", customer_name="MyCust", created_by=sales_user.id)
    db_session.add(own)
    db_session.commit()
    result = fast_search("LM358", search_db, user=sales_user)
    assert any(r["name"] == "OWN-LM358" for r in result["groups"]["requisitions"])


def test_unrestricted_role_sees_all_requisitions(search_db, test_user):
    """A BUYER (unrestricted) sees requisitions regardless of owner."""
    result = fast_search("LM358", search_db, user=test_user)
    assert any("LM358" in r["name"] for r in result["groups"]["requisitions"])


def test_restricted_role_still_sees_shared_reference_data(search_db, sales_user):
    """Vendors / material cards are shared reference data — visible to all roles."""
    result = fast_search("Arrow", search_db, user=sales_user)
    assert any(v["display_name"] == "Arrow Electronics" for v in result["groups"]["vendors"])


def test_restricted_role_does_not_surface_vendor_via_foreign_offer(db_session, test_user, sales_user):
    """A vendor whose ONLY match is an offer on a requisition the user can't see must
    not surface — that would leak the existence of a req-scoped offer."""
    # Foreign requisition owned by a DIFFERENT user (the buyer), not the sales user.
    foreign_req = Requisition(name="FOREIGN", customer_name="X", created_by=test_user.id)
    db_session.add(foreign_req)
    db_session.flush()
    part = Requirement(requisition_id=foreign_req.id, primary_mpn="SECRET99", normalized_mpn="secret99")
    db_session.add(part)
    db_session.flush()
    # Vendor whose name does NOT match the query — only its offer does.
    vendor = VendorCard(display_name="QuietVendor", normalized_name="quietvendor")
    db_session.add(vendor)
    db_session.flush()
    db_session.add(
        Offer(
            requisition_id=foreign_req.id,
            requirement_id=part.id,
            vendor_card_id=vendor.id,
            vendor_name="QuietVendor",
            mpn="SECRET99",
            normalized_mpn="secret99",
        )
    )
    db_session.commit()

    result = fast_search("SECRET99", db_session, user=sales_user)
    assert result["groups"]["offers"] == [], "restricted user saw the foreign offer"
    assert all(v["display_name"] != "QuietVendor" for v in result["groups"]["vendors"]), (
        "restricted user inferred a foreign offer via the vendor group"
    )
    # An unrestricted buyer-less call (user=None) DOES surface it (no restriction).
    open_result = fast_search("SECRET99", db_session, user=None)
    assert any(v["display_name"] == "QuietVendor" for v in open_result["groups"]["vendors"])
