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
