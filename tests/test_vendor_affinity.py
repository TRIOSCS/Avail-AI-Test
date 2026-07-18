"""Tests for vendor affinity matching service.

What: Tests L1/L2/L3 vendor affinity matching and scoring
Called by: pytest
Depends on: app.services.vendor_affinity_service, SQLAlchemy test fixtures
"""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.models import (
    EntityTag,
    MaterialCard,
    MaterialVendorHistory,
    Requirement,
    Requisition,
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


def _make_material_card(db: Session, mpn: str, manufacturer: str | None) -> MaterialCard:
    card = MaterialCard(
        normalized_mpn=mpn.lower(),
        display_mpn=mpn,
        manufacturer=manufacturer,
        created_at=datetime.now(UTC),
    )
    db.add(card)
    db.flush()
    return card


def _make_user(db: Session) -> User:
    u = User(
        email="affinity-test@trioscs.com",
        name="Affinity Tester",
        role="buyer",
        azure_id="affinity-az-001",
        created_at=datetime.now(UTC),
    )
    db.add(u)
    db.flush()
    return u


def _make_requisition(db: Session, user: User) -> Requisition:
    r = Requisition(
        name="AFF-REQ-001",
        customer_name="Test Customer",
        status="open",
        created_by=user.id,
        created_at=datetime.now(UTC),
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
        created_at=datetime.now(UTC),
    )
    db.add(req)
    db.flush()
    return req


# ── L1 Tests ────────────────────────────────────────────────────────


def test_l1_finds_vendors_by_manufacturer(db_session: Session):
    """L1 returns vendors who supplied other MPNs from the same manufacturer."""
    # Target MPN
    _make_material_card(db_session, "LM317T", "Texas Instruments")

    # Other TI parts with vendor history
    vendor_names = ["Arrow Electronics", "Digi-Key", "Mouser"]
    for i, vname in enumerate(vendor_names):
        other_card = _make_material_card(db_session, f"TPS{i}000", "Texas Instruments")

        mvh = MaterialVendorHistory(
            material_card_id=other_card.id,
            vendor_name=vname,
            vendor_name_normalized=vname.lower(),
            created_at=datetime.now(UTC),
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
    _make_material_card(db_session, "NOMAKER123", None)
    db_session.commit()

    results = find_affinity_vendors_l1("NOMAKER123", db_session)
    assert results == []


# ── L2 Tests ────────────────────────────────────────────────────────


def test_l2_finds_vendors_by_commodity(db_session: Session):
    """L2 returns vendors sharing commodity tags with the target MPN's vendors."""
    # Create target MPN's MaterialCard
    _make_material_card(db_session, "LM317T", "Texas Instruments")

    # Create a vendor card that has sightings for this MPN
    vc_source = VendorCard(
        normalized_name="arrow electronics",
        display_name="Arrow Electronics",
        sighting_count=10,
        created_at=datetime.now(UTC),
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
        created_at=datetime.now(UTC),
    )
    db_session.add(sighting)
    db_session.flush()

    # Create a commodity tag
    tag = Tag(
        name="Voltage Regulators",
        tag_type="commodity",
        created_at=datetime.now(UTC),
    )
    db_session.add(tag)
    db_session.flush()

    # Link tag to source vendor card
    et_source = EntityTag(
        entity_type="vendor_card",
        entity_id=vc_source.id,
        tag_id=tag.id,
        interaction_count=5,
        total_entity_interactions=10,
        is_visible=True,
    )
    db_session.add(et_source)

    # Create another vendor card that shares the same commodity tag
    vc_other = VendorCard(
        normalized_name="newark electronics",
        display_name="Newark Electronics",
        sighting_count=5,
        created_at=datetime.now(UTC),
    )
    db_session.add(vc_other)
    db_session.flush()

    et_other = EntityTag(
        entity_type="vendor_card",
        entity_id=vc_other.id,
        tag_id=tag.id,
        interaction_count=3,
        total_entity_interactions=8,
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
    _make_material_card(db_session, "NOTAGPART", "Acme")
    db_session.commit()

    results = find_affinity_vendors_l2("NOTAGPART", db_session)
    assert results == []


# ── L3 Tests ────────────────────────────────────────────────────────


def test_l3_skipped_without_api_key(db_session: Session):
    """L3 returns empty list when no Anthropic credential is configured."""
    with patch("app.services.credential_service.get_credential_cached", return_value=None):
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


class TestBehaviorWeightedConfidence:
    """score_affinity_matches(db=...) weights confidence by VendorCard behavioral
    signals (response_rate / ghost_rate / cancellation_rate)."""

    def _make_vc(self, db_session: Session, **overrides) -> VendorCard:
        fields = {
            "normalized_name": "behavior test vendor",
            "display_name": "Behavior Test Vendor",
            "created_at": datetime.now(UTC),
        }
        fields.update(overrides)
        vc = VendorCard(**fields)
        db_session.add(vc)
        db_session.flush()
        return vc

    def test_no_db_leaves_confidence_unweighted(self):
        """Backward compatible: omitting db skips behavioral weighting entirely."""
        matches = [{"vendor_name": "V1", "vendor_id": 1, "mpn_count": 1, "manufacturer": "TI", "level": 1}]
        scored = score_affinity_matches("LM317T", matches)
        assert scored[0]["confidence"] == pytest.approx(0.50, abs=0.001)

    def test_responsive_vendor_boosted(self, db_session: Session):
        vc = self._make_vc(db_session, response_rate=0.9)
        matches = [{"vendor_name": "V1", "vendor_id": vc.id, "mpn_count": 1, "manufacturer": "TI", "level": 1}]
        scored = score_affinity_matches("LM317T", matches, db_session)
        assert scored[0]["confidence"] > 0.50
        assert "responsive" in scored[0]["reasoning"]

    def test_ghosting_vendor_dampened(self, db_session: Session):
        vc = self._make_vc(db_session, ghost_rate=0.8)
        matches = [{"vendor_name": "V1", "vendor_id": vc.id, "mpn_count": 1, "manufacturer": "TI", "level": 1}]
        scored = score_affinity_matches("LM317T", matches, db_session)
        assert scored[0]["confidence"] < 0.50
        assert "ghosting" in scored[0]["reasoning"]

    def test_high_cancellation_rate_dampened(self, db_session: Session):
        vc = self._make_vc(db_session, cancellation_rate=0.6)
        matches = [{"vendor_name": "V1", "vendor_id": vc.id, "mpn_count": 1, "manufacturer": "TI", "level": 1}]
        scored = score_affinity_matches("LM317T", matches, db_session)
        assert scored[0]["confidence"] < 0.50

    def test_final_confidence_stays_within_existing_band(self, db_session: Session):
        """Even an extreme ghost_rate must not push confidence below the existing
        [0.30, 0.75] clamp."""
        vc = self._make_vc(db_session, ghost_rate=1.0, cancellation_rate=1.0, response_rate=0.0)
        matches = [{"vendor_name": "V1", "vendor_id": vc.id, "mpn_count": 100, "manufacturer": "TI", "level": 1}]
        scored = score_affinity_matches("LM317T", matches, db_session)
        assert 0.30 <= scored[0]["confidence"] <= 0.75

    def test_missing_vendor_card_leaves_confidence_unweighted(self, db_session: Session):
        matches = [{"vendor_name": "V1", "vendor_id": 999999, "mpn_count": 1, "manufacturer": "TI", "level": 1}]
        scored = score_affinity_matches("LM317T", matches, db_session)
        assert scored[0]["confidence"] == pytest.approx(0.50, abs=0.001)

    def test_no_vendor_id_leaves_confidence_unweighted(self, db_session: Session):
        matches = [{"vendor_name": "V1", "mpn_count": 1, "manufacturer": "TI", "level": 1}]
        scored = score_affinity_matches("LM317T", matches, db_session)
        assert scored[0]["confidence"] == pytest.approx(0.50, abs=0.001)


# ── Orchestrator Tests ──────────────────────────────────────────────


def test_find_vendor_affinity_deduplicates(db_session: Session):
    """When the same vendor appears at L1 and L2, the higher-confidence version is
    kept."""
    _make_material_card(db_session, "LM317T", "Texas Instruments")

    # Create vendor card
    vc = VendorCard(
        normalized_name="arrow electronics",
        display_name="Arrow Electronics",
        sighting_count=10,
        created_at=datetime.now(UTC),
    )
    db_session.add(vc)
    db_session.flush()

    # L1 data: vendor has history with other TI parts
    other_card = _make_material_card(db_session, "TPS54302", "Texas Instruments")

    mvh = MaterialVendorHistory(
        material_card_id=other_card.id,
        vendor_name="Arrow Electronics",
        vendor_name_normalized="arrow electronics",
        created_at=datetime.now(UTC),
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
        created_at=datetime.now(UTC),
    )
    db_session.add(sighting)
    db_session.flush()

    tag = Tag(
        name="Power ICs",
        tag_type="commodity",
        created_at=datetime.now(UTC),
    )
    db_session.add(tag)
    db_session.flush()

    et = EntityTag(
        entity_type="vendor_card",
        entity_id=vc.id,
        tag_id=tag.id,
        interaction_count=3,
        total_entity_interactions=10,
        is_visible=True,
    )
    db_session.add(et)
    db_session.commit()

    with patch("app.services.credential_service.get_credential_cached", return_value=None):
        results = find_vendor_affinity("LM317T", db_session)

    # Arrow should appear only once
    arrow_results = [r for r in results if r["vendor_name"].lower() == "arrow electronics"]
    assert len(arrow_results) <= 1

    # If present, should be L1 (higher confidence)
    if arrow_results:
        assert arrow_results[0]["level"] == 1


def test_find_vendor_affinity_limits_to_10(db_session: Session):
    """Orchestrator returns at most 10 results even with more matches."""
    _make_material_card(db_session, "LM317T", "Texas Instruments")

    # Create 15 vendors with MaterialVendorHistory for TI parts
    for i in range(15):
        other_card = _make_material_card(db_session, f"TIPART{i:03d}", "Texas Instruments")

        vname = f"Vendor {i:03d}"
        mvh = MaterialVendorHistory(
            material_card_id=other_card.id,
            vendor_name=vname,
            vendor_name_normalized=vname.lower(),
            created_at=datetime.now(UTC),
        )
        db_session.add(mvh)

    db_session.commit()

    with patch("app.services.credential_service.get_credential_cached", return_value=None):
        results = find_vendor_affinity("LM317T", db_session)
    assert len(results) <= 10
