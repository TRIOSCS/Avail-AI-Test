"""Assemble a material part's internal history for display.

What it does: given a MaterialCard's normalized key, returns a PartHistory summary
(offers, buyers, confirmed/won, sightings, requirements, price trend).
Called by: htmx_views search-history endpoint and materials detail/tab partials.
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
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    last_price: Decimal | None = None
    currency: str = "USD"


@dataclass
class PartHistory:
    found: bool = False
    card_id: int | None = None
    display_mpn: str = ""
    manufacturer: str = ""
    lifecycle_status: str | None = None
    offers: list = field(default_factory=list)
    offers_count: int = 0
    buyers: list = field(default_factory=list)
    won_offers: list = field(default_factory=list)
    customer_purchases: list = field(default_factory=list)
    confirmed_count: int = 0
    sightings: list = field(default_factory=list)
    sightings_count: int = 0
    requirements: list = field(default_factory=list)
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
    return (
        db.query(User)
        .join(Offer, Offer.entered_by_id == User.id)
        .filter(Offer.material_card_id == card_id)
        .distinct()
        .all()
    )


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
    agg = (
        db.query(func.min(MaterialPriceSnapshot.price), func.max(MaterialPriceSnapshot.price))
        .filter(MaterialPriceSnapshot.material_card_id == card_id)
        .first()
    )
    if not agg or agg[0] is None:
        return None
    last = (
        db.query(MaterialPriceSnapshot)
        .filter(MaterialPriceSnapshot.material_card_id == card_id)
        .order_by(MaterialPriceSnapshot.recorded_at.desc().nullslast())
        .first()
    )
    return PriceTrend(
        min_price=agg[0],
        max_price=agg[1],
        last_price=last.price if last else None,
        currency=(last.currency if last else "USD"),
    )


# ── Resolution + assembly ──


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
    offers_count = db.query(func.count(Offer.id)).filter(Offer.material_card_id == card.id).scalar() or 0
    buyers = buyers_for_card(db, card.id)

    won_offers = won_offers_for_card(db, card.id)
    customer_purchases = customer_purchases_for_card(db, card.id)
    won_offer_count = (
        db.query(func.count(Offer.id))
        .filter(Offer.material_card_id == card.id, Offer.status.in_(_WON_OFFER_STATUSES))
        .scalar()
    ) or 0
    won_req_count = (
        db.query(func.count(Requirement.id))
        .filter(Requirement.material_card_id == card.id, Requirement.sourcing_status == SourcingStatus.WON)
        .scalar()
    ) or 0
    customer_count = (
        db.query(func.count(CustomerPartHistory.id)).filter(CustomerPartHistory.material_card_id == card.id).scalar()
    ) or 0
    confirmed_count = won_offer_count + won_req_count + customer_count

    sightings = sightings_for_card(db, card.id)
    sightings_count = db.query(func.count(Sighting.id)).filter(Sighting.material_card_id == card.id).scalar() or 0
    requirements = requirements_for_card(db, card.id)
    requirements_count = (
        db.query(func.count(Requirement.id)).filter(Requirement.material_card_id == card.id).scalar() or 0
    )
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
