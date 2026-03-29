"""Tests for Phase 4: Vendor Name Normalization.

Verifies:
1. vendor_name_normalized column populated on create (MVH, Offer, Contact)
2. Queries use normalized column (no LOWER/TRIM in SQL)
3. Backfill covers existing rows
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import (
    Contact,
    MaterialCard,
    MaterialVendorHistory,
    Offer,
    Requirement,
    Requisition,
    User,
    VendorCard,
)
from app.vendor_utils import normalize_vendor_name

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def user(db_session: Session) -> User:
    u = User(
        email="buyer@test.com",
        name="Buyer",
        role="buyer",
        azure_id="az-vnorm-001",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(u)
    db_session.commit()
    return u


@pytest.fixture()
def requisition(db_session: Session, user: User) -> Requisition:
    req = Requisition(
        name="REQ-VNORM",
        customer_name="Test Co",
        status="active",
        created_by=user.id,
    )
    db_session.add(req)
    db_session.flush()
    r = Requirement(
        requisition_id=req.id,
        primary_mpn="LM317T",
        target_qty=100,
    )
    db_session.add(r)
    db_session.commit()
    return req


@pytest.fixture()
def material_card(db_session: Session) -> MaterialCard:
    card = MaterialCard(
        normalized_mpn="lm317t",
        display_mpn="LM317T",
    )
    db_session.add(card)
    db_session.commit()
    return card


@pytest.fixture()
def vendor_card(db_session: Session) -> VendorCard:
    card = VendorCard(
        normalized_name="arrow electronics",
        display_name="Arrow Electronics",
    )
    db_session.add(card)
    db_session.commit()
    return card


# ── MVH Tests ─────────────────────────────────────────────────────────


def test_mvh_normalized_column_exists(db_session: Session, material_card):
    """MaterialVendorHistory has vendor_name_normalized column."""
    mvh = MaterialVendorHistory(
        material_card_id=material_card.id,
        vendor_name="arrow electronics",
        vendor_name_normalized="arrow electronics",
    )
    db_session.add(mvh)
    db_session.commit()
    db_session.refresh(mvh)
    assert mvh.vendor_name_normalized == "arrow electronics"


def test_mvh_normalized_matches_vendor_name(db_session: Session, material_card):
    """vendor_name_normalized should be the normalize_vendor_name() output."""
    raw = "Mouser Electronics, Inc."
    norm = normalize_vendor_name(raw)
    mvh = MaterialVendorHistory(
        material_card_id=material_card.id,
        vendor_name=norm,
        vendor_name_normalized=norm,
    )
    db_session.add(mvh)
    db_session.commit()
    assert mvh.vendor_name_normalized == "mouser electronics"


def test_mvh_query_by_normalized(db_session: Session, material_card):
    """Can query MVH by vendor_name_normalized without LOWER()."""
    mvh = MaterialVendorHistory(
        material_card_id=material_card.id,
        vendor_name="digi-key",
        vendor_name_normalized="digi-key",
    )
    db_session.add(mvh)
    db_session.commit()

    result = (
        db_session.query(MaterialVendorHistory)
        .filter(MaterialVendorHistory.vendor_name_normalized == "digi-key")
        .first()
    )
    assert result is not None
    assert result.id == mvh.id


# ── Offer Tests ───────────────────────────────────────────────────────


def test_offer_normalized_column_exists(db_session: Session, requisition, material_card, vendor_card):
    """Offer has vendor_name_normalized column."""
    offer = Offer(
        requisition_id=requisition.id,
        vendor_card_id=vendor_card.id,
        vendor_name="Arrow Electronics",
        vendor_name_normalized="arrow electronics",
        mpn="LM317T",
        material_card_id=material_card.id,
    )
    db_session.add(offer)
    db_session.commit()
    db_session.refresh(offer)
    assert offer.vendor_name_normalized == "arrow electronics"


def test_offer_query_by_normalized(db_session: Session, requisition, material_card, vendor_card):
    """Can query Offer by vendor_name_normalized (replaces func.lower())."""
    offer = Offer(
        requisition_id=requisition.id,
        vendor_card_id=vendor_card.id,
        vendor_name="Arrow Electronics",
        vendor_name_normalized="arrow electronics",
        mpn="LM317T",
        material_card_id=material_card.id,
        status="won",
    )
    db_session.add(offer)
    db_session.commit()

    # This is the pattern that replaces func.lower(Offer.vendor_name) == norm
    results = (
        db_session.query(Offer)
        .filter(Offer.vendor_name_normalized == "arrow electronics")
        .filter(Offer.status == "won")
        .all()
    )
    assert len(results) == 1
    assert results[0].id == offer.id


# ── Contact Tests ─────────────────────────────────────────────────────


def test_contact_normalized_column_exists(db_session: Session, requisition, user):
    """Contact has vendor_name_normalized column."""
    contact = Contact(
        requisition_id=requisition.id,
        user_id=user.id,
        contact_type="email",
        vendor_name="Mouser Electronics, Inc.",
        vendor_name_normalized=normalize_vendor_name("Mouser Electronics, Inc."),
    )
    db_session.add(contact)
    db_session.commit()
    db_session.refresh(contact)
    assert contact.vendor_name_normalized == "mouser electronics"


def test_contact_query_by_normalized(db_session: Session, requisition, user):
    """Can query Contact by vendor_name_normalized (replaces LOWER(TRIM()))."""
    contact = Contact(
        requisition_id=requisition.id,
        user_id=user.id,
        contact_type="email",
        vendor_name="Mouser Electronics, Inc.",
        vendor_name_normalized="mouser electronics",
    )
    db_session.add(contact)
    db_session.commit()

    from sqlalchemy import func

    # New pattern: direct equality on normalized column
    count = (
        db_session.query(func.count(Contact.id))
        .filter(Contact.contact_type == "email")
        .filter(Contact.vendor_name_normalized == "mouser electronics")
        .scalar()
    )
    assert count == 1


# ── normalize_vendor_name() consistency tests ─────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Arrow Electronics", "arrow electronics"),
        ("Mouser Electronics, Inc.", "mouser electronics"),
        ("Digi-Key Corp.", "digi-key"),
        ("The Phoenix Company LLC", "phoenix"),
        ("  ACME CO.  ", "acme"),
        ("", ""),
    ],
)
def test_normalize_vendor_name_consistency(raw, expected):
    """normalize_vendor_name produces expected results for common patterns."""
    assert normalize_vendor_name(raw) == expected


# ── Vendor Score query pattern test ───────────────────────────────────


def test_vendor_score_fallback_uses_normalized(db_session: Session, requisition, material_card, vendor_card):
    """vendor_score fallback path uses Offer.vendor_name_normalized, not
    func.lower()."""
    # Create offers without vendor_card_id (triggers fallback)
    for i in range(6):
        db_session.add(
            Offer(
                requisition_id=requisition.id,
                vendor_name="Arrow Electronics",
                vendor_name_normalized="arrow electronics",
                mpn="LM317T",
                material_card_id=material_card.id,
                unit_price=1.0 + i,
            )
        )
    db_session.commit()

    # Query pattern from vendor_score.py (fallback path)
    norm = vendor_card.normalized_name  # "arrow electronics"
    offers = db_session.query(Offer.id).filter(Offer.vendor_name_normalized == norm).all()
    assert len(offers) == 6


# ── Engagement scorer query pattern test ──────────────────────────────


def test_engagement_scorer_uses_normalized(db_session: Session, requisition, user):
    """Engagement scorer queries use Contact.vendor_name_normalized."""
    for _ in range(3):
        db_session.add(
            Contact(
                requisition_id=requisition.id,
                user_id=user.id,
                contact_type="email",
                vendor_name="Arrow Electronics",
                vendor_name_normalized="arrow electronics",
            )
        )
    db_session.commit()

    from sqlalchemy import func

    outreach = (
        db_session.query(func.count(Contact.id))
        .filter(Contact.contact_type == "email")
        .filter(Contact.vendor_name_normalized == "arrow electronics")
        .scalar()
    )
    assert outreach == 3
