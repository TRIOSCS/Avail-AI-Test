"""Tests for MaterialPriceSnapshot model and service."""

from datetime import datetime, timezone

from app.models.price_snapshot import MaterialPriceSnapshot


def test_price_snapshot_creation(db_session):
    """Verify MaterialPriceSnapshot can be created with all fields."""
    from app.models import MaterialCard

    card = MaterialCard(normalized_mpn="TEST-MPN-001", display_mpn="TEST-MPN-001")
    db_session.add(card)
    db_session.flush()

    snap = MaterialPriceSnapshot(
        material_card_id=card.id,
        vendor_name="Test Vendor",
        price=12.50,
        currency="USD",
        quantity=100,
        source="api_sighting",
        recorded_at=datetime.now(timezone.utc),
    )
    db_session.add(snap)
    db_session.commit()

    saved = db_session.query(MaterialPriceSnapshot).first()
    assert saved.price == 12.50
    assert saved.vendor_name == "Test Vendor"
    assert saved.material_card_id == card.id


def test_record_price_snapshot(db_session):
    """Verify record_price_snapshot creates a snapshot row."""
    from app.models import MaterialCard
    from app.services.price_snapshot_service import record_price_snapshot

    card = MaterialCard(normalized_mpn="SNAP-001", display_mpn="SNAP-001")
    db_session.add(card)
    db_session.flush()

    record_price_snapshot(
        db=db_session,
        material_card_id=card.id,
        vendor_name="Mouser",
        price=5.25,
        currency="USD",
        quantity=500,
        source="api_sighting",
    )

    snaps = db_session.query(MaterialPriceSnapshot).filter_by(material_card_id=card.id).all()
    assert len(snaps) == 1
    assert snaps[0].price == 5.25
    assert snaps[0].vendor_name == "Mouser"


def test_record_price_snapshot_skips_none_price(db_session):
    """Verify no snapshot created when price is None."""
    from app.models import MaterialCard
    from app.services.price_snapshot_service import record_price_snapshot

    card = MaterialCard(normalized_mpn="SNAP-002", display_mpn="SNAP-002")
    db_session.add(card)
    db_session.flush()

    record_price_snapshot(
        db=db_session,
        material_card_id=card.id,
        vendor_name="DigiKey",
        price=None,
        currency="USD",
        quantity=100,
        source="api_sighting",
    )

    snaps = db_session.query(MaterialPriceSnapshot).filter_by(material_card_id=card.id).all()
    assert len(snaps) == 0
