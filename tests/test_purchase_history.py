"""Tests for customer purchase history model and upsert service."""

from datetime import datetime, timezone
from decimal import Decimal

from app.models import Company, CustomerSite, MaterialCard, Offer, Requisition, Requirement
from app.models.purchase_history import CustomerPartHistory
from app.services.purchase_history_service import upsert_purchase


def test_model_creation(db_session):
    """CustomerPartHistory record can be created with all fields."""
    co = Company(name="Test Co", is_active=True)
    db_session.add(co)
    db_session.flush()

    card = MaterialCard(normalized_mpn="abc123", display_mpn="ABC-123", search_count=0)
    db_session.add(card)
    db_session.flush()

    cph = CustomerPartHistory(
        company_id=co.id,
        material_card_id=card.id,
        mpn="ABC-123",
        source="avail_offer",
        purchase_count=1,
        last_unit_price=Decimal("4.50"),
        total_quantity=100,
    )
    db_session.add(cph)
    db_session.commit()

    assert cph.id is not None
    assert cph.company_id == co.id
    assert cph.material_card_id == card.id
    assert cph.source == "avail_offer"


def test_unique_constraint(db_session):
    """Duplicate (company, material_card, source) raises IntegrityError."""
    from sqlalchemy.exc import IntegrityError

    co = Company(name="Test Co", is_active=True)
    db_session.add(co)
    card = MaterialCard(normalized_mpn="xyz789", display_mpn="XYZ-789", search_count=0)
    db_session.add(card)
    db_session.flush()

    db_session.add(CustomerPartHistory(
        company_id=co.id, material_card_id=card.id, mpn="XYZ-789", source="avail_offer",
    ))
    db_session.commit()

    db_session.add(CustomerPartHistory(
        company_id=co.id, material_card_id=card.id, mpn="XYZ-789", source="avail_offer",
    ))
    try:
        db_session.commit()
        assert False, "Should have raised IntegrityError"
    except IntegrityError:
        db_session.rollback()


def test_different_sources_allowed(db_session):
    """Same company+card with different sources creates separate records."""
    co = Company(name="Test Co", is_active=True)
    db_session.add(co)
    card = MaterialCard(normalized_mpn="def456", display_mpn="DEF-456", search_count=0)
    db_session.add(card)
    db_session.flush()

    db_session.add(CustomerPartHistory(
        company_id=co.id, material_card_id=card.id, mpn="DEF-456", source="avail_offer",
    ))
    db_session.add(CustomerPartHistory(
        company_id=co.id, material_card_id=card.id, mpn="DEF-456", source="avail_quote_won",
    ))
    db_session.commit()

    count = db_session.query(CustomerPartHistory).filter_by(company_id=co.id).count()
    assert count == 2


def test_upsert_creates_new(db_session):
    """upsert_purchase creates a new record when none exists."""
    co = Company(name="Test Co", is_active=True)
    db_session.add(co)
    card = MaterialCard(normalized_mpn="new001", display_mpn="NEW-001", search_count=0)
    db_session.add(card)
    db_session.flush()

    result = upsert_purchase(
        db_session,
        company_id=co.id,
        material_card_id=card.id,
        source="avail_offer",
        unit_price=5.00,
        quantity=100,
        source_ref="offer:42",
    )
    db_session.commit()

    assert result.id is not None
    assert result.purchase_count == 1
    assert result.mpn == "NEW-001"
    assert result.last_unit_price == Decimal("5.00")
    assert result.avg_unit_price == Decimal("5.00")
    assert result.total_quantity == 100
    assert result.source_ref == "offer:42"


def test_upsert_updates_existing(db_session):
    """upsert_purchase increments count and updates rolling average on second call."""
    co = Company(name="Test Co", is_active=True)
    db_session.add(co)
    card = MaterialCard(normalized_mpn="upd001", display_mpn="UPD-001", search_count=0)
    db_session.add(card)
    db_session.flush()

    # First purchase: $10, qty 100
    upsert_purchase(
        db_session, company_id=co.id, material_card_id=card.id,
        source="avail_offer", unit_price=10.00, quantity=100,
    )
    db_session.flush()

    # Second purchase: $20, qty 200
    result = upsert_purchase(
        db_session, company_id=co.id, material_card_id=card.id,
        source="avail_offer", unit_price=20.00, quantity=200,
    )
    db_session.commit()

    assert result.purchase_count == 2
    assert result.last_unit_price == Decimal("20.00")
    # Rolling avg: (10 * 1 + 20) / 2 = 15
    assert result.avg_unit_price == Decimal("15.00")
    assert result.total_quantity == 300
    assert result.last_quantity == 200


def test_upsert_no_price(db_session):
    """upsert_purchase handles None price gracefully."""
    co = Company(name="Test Co", is_active=True)
    db_session.add(co)
    card = MaterialCard(normalized_mpn="nop001", display_mpn="NOP-001", search_count=0)
    db_session.add(card)
    db_session.flush()

    result = upsert_purchase(
        db_session, company_id=co.id, material_card_id=card.id,
        source="salesforce_import",
    )
    db_session.commit()

    assert result.purchase_count == 1
    assert result.last_unit_price is None
    assert result.avg_unit_price is None
    assert result.total_quantity == 0


def test_upsert_updates_source_ref(db_session):
    """Second upsert overwrites source_ref with latest reference."""
    co = Company(name="Test Co", is_active=True)
    db_session.add(co)
    card = MaterialCard(normalized_mpn="ref001", display_mpn="REF-001", search_count=0)
    db_session.add(card)
    db_session.flush()

    upsert_purchase(
        db_session, company_id=co.id, material_card_id=card.id,
        source="avail_offer", source_ref="offer:1",
    )
    db_session.flush()

    result = upsert_purchase(
        db_session, company_id=co.id, material_card_id=card.id,
        source="avail_offer", source_ref="offer:2",
    )
    db_session.commit()

    assert result.source_ref == "offer:2"


def test_cascade_delete_company(db_session):
    """Deleting a company cascades to its purchase history."""
    co = Company(name="Cascade Co", is_active=True)
    db_session.add(co)
    card = MaterialCard(normalized_mpn="cas001", display_mpn="CAS-001", search_count=0)
    db_session.add(card)
    db_session.flush()

    db_session.add(CustomerPartHistory(
        company_id=co.id, material_card_id=card.id, mpn="CAS-001", source="avail_offer",
    ))
    db_session.commit()

    db_session.delete(co)
    db_session.commit()

    count = db_session.query(CustomerPartHistory).count()
    assert count == 0


def test_cascade_delete_material_card(db_session):
    """Deleting a material card cascades to its purchase history."""
    co = Company(name="Cascade Co", is_active=True)
    db_session.add(co)
    card = MaterialCard(normalized_mpn="cas002", display_mpn="CAS-002", search_count=0)
    db_session.add(card)
    db_session.flush()

    db_session.add(CustomerPartHistory(
        company_id=co.id, material_card_id=card.id, mpn="CAS-002", source="avail_offer",
    ))
    db_session.commit()

    db_session.delete(card)
    db_session.commit()

    count = db_session.query(CustomerPartHistory).count()
    assert count == 0
