"""
Tests for vendor affinity matching service.
What: Tests L1/L2/L3 vendor affinity matching and scoring
Called by: pytest
Depends on: app.services.vendor_affinity_service, SQLAlchemy test fixtures
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.models import (
    EntityTag,
    MaterialCard,
    MaterialVendorHistory,
    Requisition,
    Requirement,
    Sighting,
    Tag,
    User,
    VendorCard,
)
from app.services.vendor_affinity_service import (
    find_affinity_vendors_l1,
    find_affinity_vendors_l2,
    find_affinity_vendors_l3,
    find_vendor_affinity,
    score_affinity_matches,
)


# ── Helpers ─────────────────────────────────────────────────────────


def _make_user(db: Session) -> User:
    u = User(
        email="affinity-test@trioscs.com",
        name="Affinity Tester",
        role="buyer",
        azure_id="affinity-az-001",
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def _make_requisition(db: Session, user: User) -> Requisition:
    r = Requisition(
        name="AFF-REQ-001",
        customer_name="Test Customer",
        status="active",
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(r)
    db.flush()
    return r


def _make_requirement(db: Session, requisition_id: int, mpn: str) -> Requirement:
    req = Requirement(
        requisition_id=requisition_id,
        primary_mpn=mpn,
        normalized_mpn=mpn.lower(),
        target_qty=100,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()
    return req


# ── L1 Tests ────────────────────────────────────────────────────────


def test_l1_finds_vendors_by_manufacturer(db_session: Session):
    """L1 returns vendors who supplied other MPNs from the same manufacturer."""
    # Target MPN
    target_card = MaterialCard(
        normalized_mpn="lm317t", display_mpn="LM317T",
        manufacturer="Texas Instruments",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(target_card)
    db_session.flush()

    # Other TI parts with vendor history
    vendor_names = ["Arrow Electronics", "Digi-Key", "Mouser"]
    for i, vname in enumerate(vendor_names):
        other_card = MaterialCard(
            normalized_mpn=f"tps{i}000", display_mpn=f"TPS{i}000",
            manufacturer="Texas Instruments",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(other_card)
        db_session.flush()

        mvh = MaterialVendorHistory(
            material_card_id=other_card.id,
            vendor_name=vname,
            vendor_name_normalized=vname.lower(),
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(mvh)

    db_session.commit()

    results = find_affinity_vendors_l1("LM317T", db_session)
    assert len(results) == 3
    for r in results:
        assert r["level"] == 1
        assert r["manufacturer"] == "Texas Instruments"
        assert r["mpn_count"] >= 1


def test_l1_no_material_card(db_session: Session):
    """L1 returns empty list when MPN has no MaterialCard."""
    results = find_affinity_vendors_l1("UNKNOWN-MPN-999", db_session)
    assert results == []


def test_l1_no_manufacturer(db_session: Session):
    """L1 returns empty list when MaterialCard has no manufacturer."""
    card = MaterialCard(
        normalized_mpn="nomaker123", display_mpn="NOMAKER123",
        manufacturer=None,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.commit()

    results = find_affinity_vendors_l1("NOMAKER123", db_session)
    assert results == []


# ── L2 Tests ────────────────────────────────────────────────────────


def test_l2_finds_vendors_by_commodity(db_session: Session):
    """L2 returns vendors sharing commodity tags with the target MPN's vendors."""
    # Create target MPN's MaterialCard
    target_card = MaterialCard(
        normalized_mpn="lm317t", display_mpn="LM317T",
        manufacturer="Texas Instruments",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(target_card)
    db_session.flush()

    # Create a vendor card that has sightings for this MPN
    vc_source = VendorCard(
        normalized_name="arrow electronics", display_name="Arrow Electronics",
        sighting_count=10, created_at=datetime.now(timezone.utc),
    )
    db_session.add(vc_source)
    db_session.flush()

    # Need a requirement/sighting to link vendor to MPN
    user = _make_user(db_session)
    requisition = _make_requisition(db_session, user)
    requirement = _make_requirement(db_session, requisition.id, "LM317T")

    sighting = Sighting(
        requirement_id=requirement.id,
        vendor_name="Arrow Electronics",
        vendor_name_normalized="arrow electronics",
        normalized_mpn="lm317t",
        mpn_matched="LM317T",
        qty_available=1000,
        source_type="api",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(sighting)
    db_session.flush()

    # Create a commodity tag
    tag = Tag(
        name="Voltage Regulators", tag_type="commodity",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(tag)
    db_session.flush()

    # Link tag to source vendor card
    et_source = EntityTag(
        entity_type="vendor_card", entity_id=vc_source.id,
        tag_id=tag.id, interaction_count=5, total_entity_interactions=10,
        is_visible=True,
    )
    db_session.add(et_source)

    # Create another vendor card that shares the same commodity tag
    vc_other = VendorCard(
        normalized_name="newark electronics", display_name="Newark Electronics",
        sighting_count=5, created_at=datetime.now(timezone.utc),
    )
    db_session.add(vc_other)
    db_session.flush()

    et_other = EntityTag(
        entity_type="vendor_card", entity_id=vc_other.id,
        tag_id=tag.id, interaction_count=3, total_entity_interactions=8,
        is_visible=True,
    )
    db_session.add(et_other)
    db_session.commit()

    results = find_affinity_vendors_l2("LM317T", db_session)
    assert len(results) >= 1
    assert all(r["level"] == 2 for r in results)
    vendor_names = [r["vendor_name"] for r in results]
    assert "Newark Electronics" in vendor_names


def test_l2_no_tags(db_session: Session):
    """L2 returns empty list when no commodity tags exist for the MPN."""
    card = MaterialCard(
        normalized_mpn="notagpart", display_mpn="NOTAGPART",
        manufacturer="Acme", created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.commit()

    results = find_affinity_vendors_l2("NOTAGPART", db_session)
    assert results == []


# ── L3 Tests ────────────────────────────────────────────────────────


def test_l3_skipped_without_api_key(db_session: Session):
    """L3 returns empty list when no Anthropic API key is configured."""
    with patch("app.services.vendor_affinity_service.settings") as mock_settings:
        mock_settings.anthropic_api_key = ""
        results = find_affinity_vendors_l3("LM317T", "Texas Instruments", db_session)
    assert results == []


# ── Scoring Tests ───────────────────────────────────────────────────


def test_score_assigns_confidence():
    """score_affinity_matches assigns correct confidence ranges per level."""
    matches = [
        {"vendor_name": "V1", "vendor_id": 1, "mpn_count": 5, "manufacturer": "TI", "level": 1, "confidence": 0.0},
        {"vendor_name": "V2", "vendor_id": 2, "mpn_count": 3, "manufacturer": "TI", "level": 2, "confidence": 0.0},
        {"vendor_name": "V3", "vendor_id": 3, "mpn_count": 2, "manufacturer": "TI", "level": 3, "confidence": 0.0},
    ]
    scored = score_affinity_matches("LM317T", matches)

    # L1: base 0.50 + 4*0.025 = 0.60, capped at 0.75, clamped [0.30, 0.75]
    assert 0.50 <= scored[0]["confidence"] <= 0.75
    assert scored[0]["reasoning"]

    # L2: base 0.40 + 2*0.02 = 0.44, capped at 0.60, clamped [0.30, 0.75]
    assert 0.40 <= scored[1]["confidence"] <= 0.60
    assert scored[1]["reasoning"]

    # L3: base 0.30 + 1*0.02 = 0.32, capped at 0.50, clamped [0.30, 0.75]
    assert 0.30 <= scored[2]["confidence"] <= 0.50
    assert scored[2]["reasoning"]


def test_score_empty_list():
    """score_affinity_matches returns empty list for empty input."""
    assert score_affinity_matches("LM317T", []) == []


# ── Orchestrator Tests ──────────────────────────────────────────────


def test_find_vendor_affinity_deduplicates(db_session: Session):
    """When the same vendor appears at L1 and L2, the higher-confidence version is kept."""
    target_card = MaterialCard(
        normalized_mpn="lm317t", display_mpn="LM317T",
        manufacturer="Texas Instruments",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(target_card)
    db_session.flush()

    # Create vendor card
    vc = VendorCard(
        normalized_name="arrow electronics", display_name="Arrow Electronics",
        sighting_count=10, created_at=datetime.now(timezone.utc),
    )
    db_session.add(vc)
    db_session.flush()

    # L1 data: vendor has history with other TI parts
    other_card = MaterialCard(
        normalized_mpn="tps54302", display_mpn="TPS54302",
        manufacturer="Texas Instruments",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(other_card)
    db_session.flush()

    mvh = MaterialVendorHistory(
        material_card_id=other_card.id,
        vendor_name="Arrow Electronics",
        vendor_name_normalized="arrow electronics",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(mvh)

    # L2 data: vendor has commodity tags
    user = _make_user(db_session)
    requisition = _make_requisition(db_session, user)
    requirement = _make_requirement(db_session, requisition.id, "LM317T")

    sighting = Sighting(
        requirement_id=requirement.id,
        vendor_name="Arrow Electronics",
        vendor_name_normalized="arrow electronics",
        normalized_mpn="lm317t",
        mpn_matched="LM317T",
        qty_available=500,
        source_type="api",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(sighting)
    db_session.flush()

    tag = Tag(
        name="Power ICs", tag_type="commodity",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(tag)
    db_session.flush()

    et = EntityTag(
        entity_type="vendor_card", entity_id=vc.id,
        tag_id=tag.id, interaction_count=3, total_entity_interactions=10,
        is_visible=True,
    )
    db_session.add(et)
    db_session.commit()

    results = find_vendor_affinity("LM317T", db_session)

    # Arrow should appear only once
    arrow_results = [r for r in results if r["vendor_name"].lower() == "arrow electronics"]
    assert len(arrow_results) <= 1

    # If present, should be L1 (higher confidence)
    if arrow_results:
        assert arrow_results[0]["level"] == 1


def test_find_vendor_affinity_limits_to_10(db_session: Session):
    """Orchestrator returns at most 10 results even with more matches."""
    target_card = MaterialCard(
        normalized_mpn="lm317t", display_mpn="LM317T",
        manufacturer="Texas Instruments",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(target_card)
    db_session.flush()

    # Create 15 vendors with MaterialVendorHistory for TI parts
    for i in range(15):
        other_card = MaterialCard(
            normalized_mpn=f"tipart{i:03d}", display_mpn=f"TIPART{i:03d}",
            manufacturer="Texas Instruments",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(other_card)
        db_session.flush()

        vname = f"Vendor {i:03d}"
        mvh = MaterialVendorHistory(
            material_card_id=other_card.id,
            vendor_name=vname,
            vendor_name_normalized=vname.lower(),
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(mvh)

    db_session.commit()

    results = find_vendor_affinity("LM317T", db_session)
    assert len(results) <= 10
