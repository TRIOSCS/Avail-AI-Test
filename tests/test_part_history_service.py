"""Tests for part_history_service — assembles a part's internal history.

Called by: the search history endpoint and the materials detail router.
Depends on: MaterialCard, Offer, Sighting, Requirement, CustomerPartHistory, MaterialPriceSnapshot.
"""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.constants import OfferStatus, SourcingStatus
from app.models.auth import User
from app.models.crm import Company
from app.models.intelligence import MaterialCard
from app.models.offers import Offer
from app.models.price_snapshot import MaterialPriceSnapshot
from app.models.purchase_history import CustomerPartHistory
from app.models.sourcing import Requirement, Requisition, Sighting
from app.services.part_history_service import PartHistory, get_part_history


def _make_card(db: Session, norm="lm317t", display="LM317T", mfr="TI") -> MaterialCard:
    card = MaterialCard(
        normalized_mpn=norm, display_mpn=display, manufacturer=mfr, lifecycle_status="active", search_count=0
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


def _make_user(db: Session, email="b1@trioscs.com", name="Buyer One") -> User:
    u = User(email=email, name=name, role="buyer", azure_id=f"az-{email}", created_at=datetime.now(UTC))
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _make_requisition(db: Session, status="open", customer="ACME") -> Requisition:
    r = Requisition(name="R", customer_name=customer, status=status)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _make_offer(db: Session, card, req, user, status="active", vendor="Avnet") -> Offer:
    o = Offer(
        requisition_id=req.id,
        material_card_id=card.id,
        vendor_name=vendor,
        mpn=card.display_mpn,
        qty_available=100,
        unit_price=Decimal("4.10"),
        status=status,
        entered_by_id=user.id,
        created_at=datetime.now(UTC),
    )
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


# ── Task 1: resolution + header ──


def test_no_card_returns_not_found(db_session: Session):
    result = get_part_history(db_session, "doesnotexist")
    assert isinstance(result, PartHistory)
    assert result.found is False
    assert result.card_id is None


def test_soft_deleted_card_is_not_found(db_session: Session):
    card = _make_card(db_session)
    card.deleted_at = datetime.now(UTC)
    db_session.commit()
    assert get_part_history(db_session, "lm317t").found is False


def test_card_found_populates_header(db_session: Session):
    card = _make_card(db_session)
    result = get_part_history(db_session, "lm317t")
    assert result.found is True
    assert result.card_id == card.id
    assert result.display_mpn == "LM317T"
    assert result.manufacturer == "TI"
    assert result.lifecycle_status == "active"


# ── Task 2: offers + buyers ──


def test_offers_and_buyers(db_session: Session):
    card = _make_card(db_session)
    req = _make_requisition(db_session)
    u1 = _make_user(db_session)
    u2 = _make_user(db_session, email="b2@trioscs.com", name="Buyer Two")
    _make_offer(db_session, card, req, u1, vendor="Avnet")
    _make_offer(db_session, card, req, u1, vendor="TTI")
    _make_offer(db_session, card, req, u2, vendor="Mouser")

    h = get_part_history(db_session, "lm317t")
    assert h.offers_count == 3
    assert len(h.offers) == 3  # top-N (<=5) most recent
    buyer_names = {b.name for b in h.buyers}
    assert buyer_names == {"Buyer One", "Buyer Two"}  # distinct


# ── Task 3: confirmed/won composition ──


def test_confirmed_won_composition(db_session: Session):
    card = _make_card(db_session)
    req = _make_requisition(db_session)
    u1 = _make_user(db_session)
    # 1 won offer + 1 sold offer + 1 active offer
    _make_offer(db_session, card, req, u1, status=OfferStatus.WON, vendor="Avnet")
    _make_offer(db_session, card, req, u1, status=OfferStatus.SOLD, vendor="TTI")
    _make_offer(db_session, card, req, u1, status=OfferStatus.ACTIVE, vendor="Mouser")
    # 1 won requirement
    wr = Requirement(
        requisition_id=req.id, primary_mpn="LM317T", material_card_id=card.id, sourcing_status=SourcingStatus.WON
    )
    db_session.add(wr)
    db_session.commit()
    # 1 customer purchase row
    co = Company(name="ACME Inc")
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    cph = CustomerPartHistory(
        company_id=co.id,
        material_card_id=card.id,
        mpn="LM317T",
        source="acctivate_po",
        purchase_count=2,
        total_quantity=500,
        avg_unit_price=Decimal("3.90"),
    )
    db_session.add(cph)
    db_session.commit()

    h = get_part_history(db_session, "lm317t")
    assert len(h.won_offers) == 2  # won + sold
    assert len(h.customer_purchases) == 1
    # confirmed_count = won/sold offers (2) + won reqs (1) + customer rows (1) = 4
    assert h.confirmed_count == 4


# ── Task 4: sightings + requirements + price trend ──


def test_sightings_requirements_price_trend(db_session: Session):
    card = _make_card(db_session)
    req = _make_requisition(db_session)
    requirement = Requirement(
        requisition_id=req.id, primary_mpn="LM317T", material_card_id=card.id, sourcing_status="open"
    )
    db_session.add(requirement)
    db_session.commit()
    db_session.refresh(requirement)
    db_session.add(
        Sighting(
            requirement_id=requirement.id,
            material_card_id=card.id,
            vendor_name="Avnet",
            qty_available=50,
            unit_price=Decimal("4.0"),
            source_type="brokerbin",
        )
    )
    db_session.commit()
    for p in (Decimal("3.0"), Decimal("5.0"), Decimal("4.0")):
        db_session.add(
            MaterialPriceSnapshot(material_card_id=card.id, vendor_name="Avnet", price=p, source="brokerbin")
        )
    db_session.commit()

    h = get_part_history(db_session, "lm317t")
    assert h.sightings_count == 1
    assert h.requirements_count == 1
    assert h.price_trend is not None
    assert h.price_trend.min_price == Decimal("3.0")
    assert h.price_trend.max_price == Decimal("5.0")


def test_price_trend_none_when_no_snapshots(db_session: Session):
    """A card with offers but no price snapshots has no price trend."""
    card = _make_card(db_session)
    req = _make_requisition(db_session)
    u1 = _make_user(db_session)
    _make_offer(db_session, card, req, u1)
    assert get_part_history(db_session, "lm317t").price_trend is None


def test_price_trend_scoped_to_latest_currency(db_session: Session):
    """Min/max are scoped to the most-recent snapshot's currency, not mixed."""
    card = _make_card(db_session)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    # Older EUR snapshot (would skew an unscoped min/max); newest two are USD.
    db_session.add(
        MaterialPriceSnapshot(
            material_card_id=card.id,
            vendor_name="V",
            price=Decimal("99.0"),
            currency="EUR",
            source="brokerbin",
            recorded_at=base,
        )
    )
    for i, p in enumerate((Decimal("3.0"), Decimal("5.0")), start=1):
        db_session.add(
            MaterialPriceSnapshot(
                material_card_id=card.id,
                vendor_name="V",
                price=p,
                currency="USD",
                source="brokerbin",
                recorded_at=base.replace(day=1 + i),
            )
        )
    db_session.commit()
    h = get_part_history(db_session, "lm317t")
    assert h.price_trend.currency == "USD"
    assert h.price_trend.min_price == Decimal("3.0")
    assert h.price_trend.max_price == Decimal("5.0")  # EUR 99 excluded


def test_offers_truncated_to_top_n_but_count_is_total(db_session: Session):
    """List previews cap at TOP_N while *_count reflects the true total."""
    from app.services.part_history_service import TOP_N

    card = _make_card(db_session)
    req = _make_requisition(db_session)
    u1 = _make_user(db_session)
    for i in range(TOP_N + 2):
        _make_offer(db_session, card, req, u1, vendor=f"V{i}")
    h = get_part_history(db_session, "lm317t")
    assert h.offers_count == TOP_N + 2
    assert len(h.offers) == TOP_N


def test_buyers_excludes_offers_with_null_entered_by(db_session: Session):
    """Offers with no entered_by contribute no buyer (inner join)."""
    card = _make_card(db_session)
    req = _make_requisition(db_session)
    o = Offer(
        requisition_id=req.id,
        material_card_id=card.id,
        vendor_name="Avnet",
        mpn="LM317T",
        qty_available=1,
        unit_price=Decimal("1.0"),
        status="active",
        entered_by_id=None,
        created_at=datetime.now(UTC),
    )
    db_session.add(o)
    db_session.commit()
    h = get_part_history(db_session, "lm317t")
    assert h.offers_count == 1
    assert h.buyers == []


def test_buyers_deduped_when_user_has_json_commodity_tags(db_session: Session):
    """Regression: buyers must dedup even though User carries a JSON column.

    On Postgres a ``SELECT DISTINCT`` over the full User row (which includes
    ``commodity_tags``, a ``JSON`` column with no equality operator) raises
    "could not identify an equality operator for type json". Deduping on the
    offer FK instead avoids that. SQLite tolerates DISTINCT-over-JSON, so it
    can't reproduce the operator error here — the live-Postgres check is the
    real guard; this asserts the dedup contract and that the loaded buyer keeps
    its JSON field intact.
    """
    card = _make_card(db_session)
    req = _make_requisition(db_session)
    u1 = _make_user(db_session)
    u1.commodity_tags = ["resistors", "capacitors"]  # the column that broke DISTINCT
    db_session.commit()
    # Same buyer across two offers must collapse to a single buyer.
    _make_offer(db_session, card, req, u1, vendor="Avnet")
    _make_offer(db_session, card, req, u1, vendor="TTI")

    h = get_part_history(db_session, "lm317t")
    assert len(h.buyers) == 1
    assert h.buyers[0].id == u1.id
    assert h.buyers[0].commodity_tags == ["resistors", "capacitors"]
