"""Tests for MaterialPriceSnapshot model and service."""

from datetime import datetime, timezone

from app.models.price_snapshot import MaterialPriceSnapshot


def _make_card(db_session, mpn):
    """Create and flush a MaterialCard, returning it."""
    from app.models import MaterialCard

    card = MaterialCard(normalized_mpn=mpn, display_mpn=mpn)
    db_session.add(card)
    db_session.flush()
    return card


def test_price_snapshot_creation(db_session):
    """Verify MaterialPriceSnapshot can be created with all fields."""
    card = _make_card(db_session, "TEST-MPN-001")

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
    assert float(saved.price) == 12.50
    assert saved.vendor_name == "Test Vendor"
    assert saved.material_card_id == card.id


def test_record_price_snapshot(db_session):
    """Verify record_price_snapshot creates a snapshot row."""
    from app.services.price_snapshot_service import record_price_snapshot

    card = _make_card(db_session, "SNAP-001")

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
    assert float(snaps[0].price) == 5.25
    assert snaps[0].vendor_name == "Mouser"


def test_record_price_snapshot_skips_none_price(db_session):
    """Verify no snapshot created when price is None."""
    from app.services.price_snapshot_service import record_price_snapshot

    card = _make_card(db_session, "SNAP-002")

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
