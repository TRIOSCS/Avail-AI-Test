"""Assemble a material part's internal history for display.

What it does: given a MaterialCard's normalized key, ``get_part_history`` returns a
PartHistory summary (offers, buyers, confirmed/won, sightings, requirements, price
trend). The per-section ``*_for_card`` query helpers below can also be used on their
own.
Called by: ``get_part_history`` → htmx_views.search_history_panel (the search-page
           panel). The ``*_for_card`` helpers are called directly by htmx_views
           material_detail_partial / material_tab_partial (the materials detail page).
Depends on: MaterialCard, Offer, Sighting, Requirement, CustomerPartHistory,
            MaterialPriceSnapshot, User.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.constants import OfferStatus, SourcingStatus
from app.models.auth import User
from app.models.intelligence import MaterialCard
from app.models.offers import Offer
from app.models.price_snapshot import MaterialPriceSnapshot
from app.models.purchase_history import CustomerPartHistory
from app.models.sourcing import Requirement, Sighting

TOP_N = 5
_WON_OFFER_STATUSES = (OfferStatus.WON, OfferStatus.SOLD)


@dataclass
class PriceTrend:
    """A part's price range, scoped to the most-recent snapshot's currency."""

    min_price: Decimal
    max_price: Decimal
    last_price: Decimal | None = None
    currency: str = "USD"


@dataclass
class PartHistory:
    """A part's internal history summary for the search-page panel.

    Contracts: all payload fields below are meaningful only when ``found`` is True.
    The ``*_count`` fields are full totals, while the parallel list fields are capped
    at ``TOP_N`` (previews) — the template uses ``count > list|length`` to show a
    "view all" link. ``confirmed_count`` is a cross-source SUM (won/sold offers + won
    requisitions + customer-purchase rows), NOT the length of any single list.
    """

    found: bool = False
    card_id: int | None = None
    display_mpn: str = ""
    manufacturer: str = ""
    lifecycle_status: str | None = None
    offers: list[Offer] = field(default_factory=list)
    offers_count: int = 0
    buyers: list[User] = field(default_factory=list)
    won_offers: list[Offer] = field(default_factory=list)
    customer_purchases: list[CustomerPartHistory] = field(default_factory=list)
    confirmed_count: int = 0
    sightings: list[Sighting] = field(default_factory=list)
    sightings_count: int = 0
    requirements: list[Requirement] = field(default_factory=list)
    requirements_count: int = 0
    price_trend: PriceTrend | None = None


# ── Per-section query helpers (also consumed by the materials detail router) ──


def offers_for_card(db: Session, card_id: int, limit: int = TOP_N) -> list[Offer]:
    return (
        db.query(Offer)
        .filter(Offer.material_card_id == card_id)
        .order_by(Offer.created_at.desc().nullslast())
        .limit(limit)
        .all()
    )


def buyers_for_card(db: Session, card_id: int) -> list[User]:
    # Dedup on the integer FK, NOT a DISTINCT over the full User row: Postgres can't
    # apply DISTINCT to a row containing User.commodity_tags (JSON has no equality
    # operator → "could not identify an equality operator for type json"). Resolve the
    # distinct buyer ids first, then load those users.
    buyer_ids = [
        bid
        for (bid,) in db.query(Offer.entered_by_id)
        .filter(Offer.material_card_id == card_id, Offer.entered_by_id.isnot(None))
        .distinct()
        .all()
    ]
    if not buyer_ids:
        return []
    return db.query(User).filter(User.id.in_(buyer_ids)).all()


def won_offers_for_card(db: Session, card_id: int, limit: int = TOP_N) -> list[Offer]:
    return (
        db.query(Offer)
        .filter(Offer.material_card_id == card_id, Offer.status.in_(_WON_OFFER_STATUSES))
        .order_by(Offer.created_at.desc().nullslast())
        .limit(limit)
        .all()
    )


def customer_purchases_for_card(db: Session, card_id: int, limit: int = TOP_N) -> list[CustomerPartHistory]:
    return (
        db.query(CustomerPartHistory)
        .filter(CustomerPartHistory.material_card_id == card_id)
        .order_by(CustomerPartHistory.last_purchased_at.desc().nullslast())
        .limit(limit)
        .all()
    )


def sightings_for_card(db: Session, card_id: int, limit: int = TOP_N) -> list[Sighting]:
    return (
        db.query(Sighting)
        .filter(Sighting.material_card_id == card_id)
        .order_by(Sighting.created_at.desc().nullslast())
        .limit(limit)
        .all()
    )


def requirements_for_card(db: Session, card_id: int, limit: int = TOP_N) -> list[Requirement]:
    return (
        db.query(Requirement)
        .filter(Requirement.material_card_id == card_id)
        .order_by(Requirement.created_at.desc().nullslast())
        .limit(limit)
        .all()
    )


def price_trend_for_card(db: Session, card_id: int) -> PriceTrend | None:
    # Anchor on the most-recent snapshot, then scope min/max to ITS currency so the
    # range is meaningful (mixing currencies into one min/max would be nonsense).
    last = (
        db.query(MaterialPriceSnapshot)
        .filter(MaterialPriceSnapshot.material_card_id == card_id)
        .order_by(MaterialPriceSnapshot.recorded_at.desc().nullslast())
        .first()
    )
    if last is None:
        return None
    currency = last.currency or "USD"
    agg = (
        db.query(func.min(MaterialPriceSnapshot.price), func.max(MaterialPriceSnapshot.price))
        .filter(
            MaterialPriceSnapshot.material_card_id == card_id,
            MaterialPriceSnapshot.currency == currency,
        )
        .first()
    )
    return PriceTrend(
        min_price=agg[0],
        max_price=agg[1],
        last_price=last.price,
        currency=currency,
    )


# ── Resolution + assembly ──


def _count(db: Session, model: type, *conditions) -> int:
    """COUNT(model.id) over the given filter conditions, coalescing NULL → 0."""
    return db.query(func.count(model.id)).filter(*conditions).scalar() or 0


def _resolve_card(db: Session, normalized_key: str) -> MaterialCard | None:
    if not normalized_key:
        return None
    return (
        db.query(MaterialCard)
        .filter(MaterialCard.normalized_mpn == normalized_key)
        .filter(MaterialCard.deleted_at.is_(None))
        .first()
    )


def get_part_history(db: Session, normalized_key: str) -> PartHistory:
    card = _resolve_card(db, normalized_key)
    if card is None:
        return PartHistory(found=False)

    offers = offers_for_card(db, card.id)
    offers_count = _count(db, Offer, Offer.material_card_id == card.id)
    buyers = buyers_for_card(db, card.id)

    # "Confirmed / Won" = three independent kinds of evidence the part actually moved:
    # won/sold offers (_WON_OFFER_STATUSES includes SOLD; the UI labels it "Won"),
    # WON requisitions, and customer-purchase rows. The total is their sum.
    won_offers = won_offers_for_card(db, card.id)
    customer_purchases = customer_purchases_for_card(db, card.id)
    won_offer_count = _count(db, Offer, Offer.material_card_id == card.id, Offer.status.in_(_WON_OFFER_STATUSES))
    won_req_count = _count(
        db, Requirement, Requirement.material_card_id == card.id, Requirement.sourcing_status == SourcingStatus.WON
    )
    customer_count = _count(db, CustomerPartHistory, CustomerPartHistory.material_card_id == card.id)
    confirmed_count = won_offer_count + won_req_count + customer_count

    sightings = sightings_for_card(db, card.id)
    sightings_count = _count(db, Sighting, Sighting.material_card_id == card.id)
    requirements = requirements_for_card(db, card.id)
    requirements_count = _count(db, Requirement, Requirement.material_card_id == card.id)
    price_trend = price_trend_for_card(db, card.id)

    return PartHistory(
        found=True,
        card_id=card.id,
        display_mpn=card.display_mpn or "",
        manufacturer=card.manufacturer or "",
        lifecycle_status=card.lifecycle_status,
        offers=offers,
        offers_count=offers_count,
        buyers=buyers,
        won_offers=won_offers,
        customer_purchases=customer_purchases,
        confirmed_count=confirmed_count,
        sightings=sightings,
        sightings_count=sightings_count,
        requirements=requirements,
        requirements_count=requirements_count,
        price_trend=price_trend,
    )
