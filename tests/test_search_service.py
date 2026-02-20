"""
test_search_service.py — Tests for search_service helper functions

Covers: get_all_pns, sighting_to_dict, _history_to_result, _save_sightings,
_upsert_material_card, _propagate_vendor_emails.

All tests use the in-memory SQLite session from conftest.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy.orm import Session

from app.models import (
    MaterialCard,
    MaterialVendorHistory,
    Requirement,
    Requisition,
    Sighting,
    User,
    VendorCard,
    VendorContact,
)
from app.search_service import (
    _history_to_result,
    _propagate_vendor_emails,
    _save_sightings,
    _upsert_material_card,
    get_all_pns,
    sighting_to_dict,
)

# ── Helpers ────────────────────────────────────────────────────────────


def _make_requirement(db: Session, user: User, mpn="LM317T", subs=None, target_qty=1000):
    """Create a Requisition + Requirement and return the Requirement."""
    req = Requisition(
        name="TEST-REQ",
        status="active",
        created_by=user.id,
    )
    db.add(req)
    db.flush()
    item = Requirement(
        requisition_id=req.id,
        primary_mpn=mpn,
        substitutes=subs,
        target_qty=target_qty,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


# ── get_all_pns() ─────────────────────────────────────────────────────


def test_get_all_pns_primary_only(db_session, test_user):
    """Returns just the primary MPN when no substitutes."""
    req = _make_requirement(db_session, test_user, mpn="LM317T", subs=None)
    pns = get_all_pns(req)
    assert pns == ["LM317T"]


def test_get_all_pns_with_substitutes(db_session, test_user):
    """Returns primary + substitutes."""
    req = _make_requirement(db_session, test_user, mpn="LM317T", subs=["LM317LZ", "LM350T"])
    pns = get_all_pns(req)
    assert len(pns) == 3
    assert pns[0] == "LM317T"
    assert "LM317LZ" in pns
    assert "LM350T" in pns


def test_get_all_pns_dedup_by_key(db_session, test_user):
    """Substitutes that normalize to the same key as primary are deduplicated."""
    req = _make_requirement(db_session, test_user, mpn="LM317T", subs=["LM-317-T"])
    pns = get_all_pns(req)
    assert len(pns) == 1  # "LM317T" and "LM-317-T" have the same canonical key


def test_get_all_pns_empty(db_session, test_user):
    """Empty/blank primary MPN returns empty list."""
    req = _make_requirement(db_session, test_user, mpn="", subs=None)
    pns = get_all_pns(req)
    assert pns == []


def test_get_all_pns_none_subs(db_session, test_user):
    """None substitutes list is handled."""
    req = _make_requirement(db_session, test_user, mpn="TPS65988", subs=[None, "", "  "])
    pns = get_all_pns(req)
    assert pns == ["TPS65988"]


# ── sighting_to_dict() ────────────────────────────────────────────────


def test_sighting_to_dict_full(db_session, test_user):
    """Full sighting converts correctly."""
    req = _make_requirement(db_session, test_user)
    s = Sighting(
        requirement_id=req.id,
        vendor_name="Arrow Electronics",
        vendor_email="sales@arrow.com",
        vendor_phone="+1-555-1234",
        mpn_matched="LM317T",
        manufacturer="TI",
        qty_available=5000,
        unit_price=0.52,
        currency="USD",
        moq=100,
        source_type="nexar",
        is_authorized=True,
        confidence=0.95,
        score=82.5,
        raw_data={"octopart_url": "https://octopart.com/lm317t", "vendor_sku": "LM317T-ND"},
        condition="new",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(s)
    db_session.commit()

    d = sighting_to_dict(s)
    assert d["vendor_name"] == "Arrow Electronics"
    assert d["vendor_email"] == "sales@arrow.com"
    assert d["mpn_matched"] == "LM317T"
    assert d["score"] == 82.5
    assert d["octopart_url"] == "https://octopart.com/lm317t"
    assert d["vendor_sku"] == "LM317T-ND"
    assert d["condition"] == "new"


def test_sighting_to_dict_minimal(db_session, test_user):
    """Sighting with minimal fields (Nones) converts without error."""
    req = _make_requirement(db_session, test_user)
    s = Sighting(
        requirement_id=req.id,
        vendor_name="Unknown Vendor",
        mpn_matched="XYZ123",
        raw_data=None,
        created_at=None,
    )
    db_session.add(s)
    db_session.commit()

    d = sighting_to_dict(s)
    assert d["vendor_name"] == "Unknown Vendor"
    assert d["vendor_email"] is None
    assert d["octopart_url"] is None
    # created_at has a column default so it gets populated even with None input
    assert d["created_at"] is not None


# ── _history_to_result() ──────────────────────────────────────────────


def test_history_to_result_recent():
    """Recent history (< 7 days) gets high base score."""
    now = datetime.now(timezone.utc)
    h = {
        "vendor_name": "Acme",
        "mpn_matched": "LM317T",
        "manufacturer": "TI",
        "qty_available": 1000,
        "unit_price": 0.50,
        "currency": "USD",
        "source_type": "brokerbin",
        "is_authorized": False,
        "vendor_sku": "SK-001",
        "first_seen": now - timedelta(days=30),
        "last_seen": now - timedelta(days=2),
        "times_seen": 1,
        "material_card_id": 99,
    }
    result = _history_to_result(h, now)
    assert result["score"] >= 50  # base 55, recent
    assert result["is_material_history"] is True
    assert result["vendor_name"] == "Acme"


def test_history_to_result_old():
    """Old history (> 90 days) gets low base score."""
    now = datetime.now(timezone.utc)
    h = {
        "vendor_name": "Old Corp",
        "mpn_matched": "ANCIENT-PART",
        "manufacturer": None,
        "qty_available": None,
        "unit_price": None,
        "currency": "USD",
        "source_type": "manual",
        "is_authorized": False,
        "vendor_sku": None,
        "first_seen": now - timedelta(days=365),
        "last_seen": now - timedelta(days=120),
        "times_seen": 1,
        "material_card_id": 50,
    }
    result = _history_to_result(h, now)
    assert result["score"] <= 30  # base 30 for old, minus age penalty
    assert result["is_material_history"] is True


def test_history_to_result_times_seen_bonus():
    """times_seen > 1 adds bonus to score."""
    now = datetime.now(timezone.utc)
    base_h = {
        "vendor_name": "Reliable Corp",
        "mpn_matched": "LM317T",
        "manufacturer": "TI",
        "qty_available": 5000,
        "unit_price": 0.45,
        "currency": "USD",
        "source_type": "nexar",
        "is_authorized": True,
        "vendor_sku": None,
        "first_seen": now - timedelta(days=15),
        "last_seen": now - timedelta(days=10),
        "times_seen": 1,
        "material_card_id": 42,
    }
    once = _history_to_result(dict(base_h, times_seen=1), now)
    many = _history_to_result(dict(base_h, times_seen=6), now)
    assert many["score"] > once["score"]


# ── _save_sightings() ────────────────────────────────────────────────


def test_save_sightings_creates_scored(db_session, test_user):
    """_save_sightings creates Sighting records with scores."""
    req = _make_requirement(db_session, test_user)
    fresh = [
        {
            "vendor_name": "Arrow Electronics",
            "vendor_email": "sales@arrow.com",
            "mpn_matched": "LM317T",
            "manufacturer": "Texas Instruments",
            "qty_available": 5000,
            "unit_price": 0.52,
            "currency": "USD",
            "source_type": "nexar",
            "is_authorized": True,
            "confidence": 0.9,
        },
        {
            "vendor_name": "Digi-Key",
            "mpn_matched": "LM317T",
            "qty_available": 1000,
            "unit_price": 0.75,
            "currency": "USD",
            "source_type": "digikey",
            "is_authorized": True,
            "confidence": 0.95,
        },
    ]

    with patch("app.search_service._propagate_vendor_emails"):
        sightings = _save_sightings(fresh, req, db_session)

    assert len(sightings) == 2
    assert all(s.score > 0 for s in sightings)
    assert all(s.requirement_id == req.id for s in sightings)

    # Verify persisted in DB
    count = db_session.query(Sighting).filter_by(requirement_id=req.id).count()
    assert count == 2


def test_save_sightings_clears_previous(db_session, test_user):
    """_save_sightings deletes previous sightings on re-search."""
    req = _make_requirement(db_session, test_user)

    # Create an initial sighting
    old = Sighting(
        requirement_id=req.id,
        vendor_name="Old Vendor",
        mpn_matched="LM317T",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(old)
    db_session.commit()
    fresh = [
        {
            "vendor_name": "New Vendor",
            "mpn_matched": "LM317T",
            "source_type": "brokerbin",
        },
    ]

    with patch("app.search_service._propagate_vendor_emails"):
        sightings = _save_sightings(fresh, req, db_session)

    assert len(sightings) == 1
    assert sightings[0].vendor_name == "New Vendor"
    # Old sighting should be gone — query DB directly to bypass identity map
    remaining = db_session.query(Sighting).filter_by(requirement_id=req.id).all()
    assert len(remaining) == 1
    assert remaining[0].vendor_name == "New Vendor"


# ── _upsert_material_card() ──────────────────────────────────────────


def test_upsert_material_card_creates_new(db_session, test_user):
    """Creates a new MaterialCard when none exists."""
    req = _make_requirement(db_session, test_user)
    now = datetime.now(timezone.utc)

    s = Sighting(
        requirement_id=req.id,
        vendor_name="Arrow",
        mpn_matched="LM317T",
        manufacturer="TI",
        qty_available=5000,
        unit_price=0.50,
        source_type="nexar",
        is_authorized=True,
        raw_data={"vendor_sku": "LM317T-ND"},
        created_at=now,
    )
    db_session.add(s)
    db_session.commit()

    _upsert_material_card("LM317T", [s], db_session, now)

    card = db_session.query(MaterialCard).filter_by(normalized_mpn="lm317t").first()
    assert card is not None
    assert card.search_count == 1
    assert card.manufacturer == "TI"

    # Check vendor history
    vh = db_session.query(MaterialVendorHistory).filter_by(material_card_id=card.id).first()
    assert vh is not None
    assert vh.vendor_name == "Arrow"
    assert vh.times_seen == 1


def test_upsert_material_card_updates_existing(db_session, test_user):
    """Updates an existing MaterialCard and increments vendor history."""
    now = datetime.now(timezone.utc)
    req = _make_requirement(db_session, test_user)

    # Pre-create the card
    card = MaterialCard(normalized_mpn="lm317t", display_mpn="LM317T", search_count=3)
    db_session.add(card)
    db_session.flush()

    vh = MaterialVendorHistory(
        material_card_id=card.id,
        vendor_name="Arrow",
        source_type="nexar",
        times_seen=2,
        first_seen=now - timedelta(days=30),
        last_seen=now - timedelta(days=5),
    )
    db_session.add(vh)
    db_session.commit()

    s = Sighting(
        requirement_id=req.id,
        vendor_name="Arrow",
        mpn_matched="LM317T",
        qty_available=8000,
        unit_price=0.48,
        source_type="nexar",
        raw_data={},
        created_at=now,
    )
    db_session.add(s)
    db_session.commit()

    _upsert_material_card("LM317T", [s], db_session, now)

    db_session.refresh(card)
    assert card.search_count == 4

    db_session.refresh(vh)
    assert vh.times_seen == 3
    assert vh.last_qty == 8000


# ── _propagate_vendor_emails() ────────────────────────────────────────


def test_propagate_vendor_emails_creates_contact(db_session, test_user):
    """Creates VendorContact when VendorCard exists for sighting vendor."""
    req = _make_requirement(db_session, test_user)

    # Create a VendorCard matching the sighting vendor
    card = VendorCard(
        normalized_name="arrow electronics",
        display_name="Arrow Electronics",
        emails=["info@arrow.com"],
        phones=[],
    )
    db_session.add(card)
    db_session.commit()

    s = Sighting(
        requirement_id=req.id,
        vendor_name="Arrow Electronics",
        vendor_email="sales@arrow.com",
        mpn_matched="LM317T",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(s)
    db_session.commit()

    _propagate_vendor_emails([s], db_session)

    vc = (
        db_session.query(VendorContact)
        .filter_by(vendor_card_id=card.id, email="sales@arrow.com")
        .first()
    )
    assert vc is not None
    assert vc.source == "brokerbin"
    assert vc.contact_type == "company"
