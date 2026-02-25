"""Proactive matching engine — finds customer matches for new inventory.

Uses customer_part_history (CPH) as the primary matching backbone.
Falls back to archived requisitions for customers without CPH data.

Scoring: composite of recency (40%), frequency (30%), margin potential (30%).
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    ActivityLog,
    Company,
    CustomerSite,
    Offer,
    ProactiveMatch,
    ProactiveThrottle,
    Requirement,
    Requisition,
    Sighting,
)
from ..models.purchase_history import CustomerPartHistory

log = logging.getLogger("avail.proactive_matching")

_last_scan_at = datetime.min.replace(tzinfo=timezone.utc)


# ── Scoring ──────────────────────────────────────────────────────────────


def _score_recency(last_purchased_at: datetime | None) -> int:
    """Score 0-100 based on how recently the customer bought this part."""
    if not last_purchased_at:
        return 20
    days = (datetime.now(timezone.utc) - last_purchased_at.replace(tzinfo=timezone.utc)).days
    if days <= 180:
        return 100
    if days <= 365:
        return 80
    if days <= 730:
        return 60
    return 40


def _score_frequency(purchase_count: int) -> int:
    """Score 0-100 based on number of purchases."""
    if purchase_count >= 5:
        return 100
    if purchase_count >= 3:
        return 80
    if purchase_count >= 2:
        return 60
    return 40


def _score_margin(customer_avg_price: float | None, our_cost: float | None) -> tuple[int, float | None]:
    """Score 0-100 based on margin potential. Returns (score, margin_pct)."""
    if not customer_avg_price or not our_cost or our_cost <= 0:
        return 50, None  # Unknown margin = neutral score
    margin_pct = (customer_avg_price - our_cost) / customer_avg_price * 100
    if margin_pct >= 30:
        return 100, round(margin_pct, 1)
    if margin_pct >= 20:
        return 80, round(margin_pct, 1)
    if margin_pct >= 10:
        return 60, round(margin_pct, 1)
    if margin_pct > 0:
        return 40, round(margin_pct, 1)
    return 10, round(margin_pct, 1)


def compute_match_score(
    last_purchased_at: datetime | None,
    purchase_count: int,
    customer_avg_price: float | None,
    our_cost: float | None,
) -> tuple[int, float | None]:
    """Composite match score (0-100) and margin percentage.

    Weights: recency 40%, frequency 30%, margin 30%.
    """
    recency = _score_recency(last_purchased_at)
    frequency = _score_frequency(purchase_count)
    margin_score, margin_pct = _score_margin(customer_avg_price, our_cost)
    composite = int(recency * 0.4 + frequency * 0.3 + margin_score * 0.3)
    return min(100, max(0, composite)), margin_pct


# ── Per-offer matching ───────────────────────────────────────────────────


def find_matches_for_offer(offer_id: int, db: Session) -> list[ProactiveMatch]:
    """Find customer matches for a single offer via CPH."""
    offer = db.get(Offer, offer_id)
    if not offer or not offer.material_card_id:
        return []
    return _find_matches(
        db,
        material_card_id=offer.material_card_id,
        mpn=offer.mpn or "",
        our_cost=float(offer.unit_price) if offer.unit_price else None,
        source_offer=offer,
    )


def find_matches_for_sighting(sighting_id: int, db: Session) -> list[ProactiveMatch]:
    """Find customer matches for a confirmed sighting via CPH."""
    sighting = db.get(Sighting, sighting_id)
    if not sighting or not sighting.material_card_id:
        return []
    return _find_matches(
        db,
        material_card_id=sighting.material_card_id,
        mpn=sighting.mpn_matched or "",
        our_cost=float(sighting.unit_price) if sighting.unit_price else None,
        source_sighting=sighting,
    )


def _find_matches(
    db: Session,
    *,
    material_card_id: int,
    mpn: str,
    our_cost: float | None,
    source_offer: Offer | None = None,
    source_sighting: Sighting | None = None,
) -> list[ProactiveMatch]:
    """Core matching logic — query CPH, score, create ProactiveMatch records."""
    throttle_cutoff = datetime.now(timezone.utc) - timedelta(
        days=settings.proactive_throttle_days
    )
    min_margin = settings.proactive_min_margin_pct

    # Find all CPH entries for this part
    cph_rows = (
        db.query(CustomerPartHistory)
        .filter(CustomerPartHistory.material_card_id == material_card_id)
        .all()
    )
    if not cph_rows:
        return []

    matches = []
    for cph in cph_rows:
        # Need a company → site → owner chain
        company = db.get(Company, cph.company_id)
        if not company or not company.account_owner_id:
            continue

        # Get the primary site for this company
        site = (
            db.query(CustomerSite)
            .filter_by(company_id=cph.company_id, is_active=True)
            .first()
        )
        if not site:
            continue

        # Check throttle
        throttled = (
            db.query(ProactiveThrottle)
            .filter(
                ProactiveThrottle.mpn == mpn.upper().strip(),
                ProactiveThrottle.customer_site_id == site.id,
                ProactiveThrottle.last_offered_at > throttle_cutoff,
            )
            .first()
        )
        if throttled:
            continue

        # Score the match
        avg_price = float(cph.avg_unit_price) if cph.avg_unit_price else None
        score, margin_pct = compute_match_score(
            cph.last_purchased_at,
            cph.purchase_count or 0,
            avg_price,
            our_cost,
        )

        # Filter by minimum margin if we can calculate it
        if margin_pct is not None and margin_pct < min_margin:
            continue

        # We need a requirement_id and requisition_id for the existing ProactiveMatch model.
        # Find the most recent archived requisition for this company+part.
        req_row = (
            db.query(Requirement, Requisition)
            .join(Requisition, Requirement.requisition_id == Requisition.id)
            .filter(
                Requirement.material_card_id == material_card_id,
                Requisition.customer_site_id == site.id,
            )
            .order_by(Requisition.created_at.desc())
            .first()
        )
        if not req_row:
            # No requisition history — skip (can't populate required FK)
            continue
        req_item, requisition = req_row

        # Dedup: don't create duplicate matches
        dedup_filter = [
            ProactiveMatch.material_card_id == material_card_id,
            ProactiveMatch.company_id == cph.company_id,
            ProactiveMatch.status.in_(["new", "sent"]),
        ]
        if source_offer:
            dedup_filter.append(ProactiveMatch.offer_id == source_offer.id)
        existing = db.query(ProactiveMatch).filter(*dedup_filter).first()
        if existing:
            continue

        last_price = float(cph.last_unit_price) if cph.last_unit_price else None

        # Resolve offer_id — required NOT NULL FK
        if source_offer:
            offer_id = source_offer.id
        else:
            # Sighting-triggered: find most recent offer for this part
            fallback_offer = (
                db.query(Offer.id)
                .filter(Offer.material_card_id == material_card_id)
                .order_by(Offer.created_at.desc())
                .first()
            )
            if not fallback_offer:
                continue  # Can't create match without an offer FK
            offer_id = fallback_offer[0]

        match = ProactiveMatch(
            offer_id=offer_id,
            requirement_id=req_item.id,
            requisition_id=requisition.id,
            customer_site_id=site.id,
            salesperson_id=company.account_owner_id,
            mpn=mpn.upper().strip(),
            material_card_id=material_card_id,
            company_id=cph.company_id,
            match_score=score,
            margin_pct=margin_pct,
            customer_purchase_count=cph.purchase_count or 0,
            customer_last_price=last_price,
            customer_last_purchased_at=cph.last_purchased_at,
            our_cost=our_cost,
        )
        db.add(match)
        matches.append(match)

        # In-app notification
        if company.account_owner_id:
            db.add(ActivityLog(
                user_id=company.account_owner_id,
                activity_type="proactive_match",
                channel="system",
                requisition_id=requisition.id,
                contact_name=company.name,
                subject=f"Proactive match: {mpn.upper().strip()} — {company.name} (score {score})",
            ))

    return matches


# ── Batch scan ───────────────────────────────────────────────────────────


def run_proactive_scan(db: Session) -> dict:
    """Batch scan: find matches for all new offers/sightings since last run.

    Called by scheduler. Returns {scanned_offers, scanned_sightings, matches_created}.
    """
    global _last_scan_at
    now = datetime.now(timezone.utc)
    since = _last_scan_at

    # Scan new offers
    new_offers = (
        db.query(Offer)
        .filter(
            Offer.created_at > since,
            Offer.material_card_id.isnot(None),
        )
        .all()
    )

    # Scan new sightings (only high-confidence confirmed ones)
    new_sightings = (
        db.query(Sighting)
        .filter(
            Sighting.created_at > since,
            Sighting.material_card_id.isnot(None),
            Sighting.is_unavailable.is_(False),
        )
        .all()
    )

    _last_scan_at = now
    total_matches = 0

    # Deduplicate: don't scan the same material_card_id twice
    scanned_cards: set[int] = set()

    for offer in new_offers:
        if offer.material_card_id in scanned_cards:
            continue
        scanned_cards.add(offer.material_card_id)
        matches = find_matches_for_offer(offer.id, db)
        total_matches += len(matches)

    for sighting in new_sightings:
        if sighting.material_card_id in scanned_cards:
            continue
        scanned_cards.add(sighting.material_card_id)
        matches = find_matches_for_sighting(sighting.id, db)
        total_matches += len(matches)

    if total_matches:
        try:
            db.commit()
        except Exception as e:
            log.error("Failed to commit proactive matches: %s", e)
            db.rollback()
            return {
                "scanned_offers": len(new_offers),
                "scanned_sightings": len(new_sightings),
                "matches_created": 0,
            }

    log.info(
        "Proactive scan: %d offers, %d sightings → %d matches",
        len(new_offers), len(new_sightings), total_matches,
    )
    return {
        "scanned_offers": len(new_offers),
        "scanned_sightings": len(new_sightings),
        "matches_created": total_matches,
    }


# ── Match actions ────────────────────────────────────────────────────────


def dismiss_match(match_id: int, user_id: int, reason: str, db: Session) -> None:
    """Dismiss a proactive match — salesperson says 'not interested'."""
    match = db.get(ProactiveMatch, match_id)
    if not match:
        raise ValueError("Match not found")
    if match.salesperson_id != user_id:
        raise ValueError("Not your match")
    match.status = "dismissed"
    match.dismiss_reason = reason
    db.commit()


def mark_match_sent(match_id: int, user_id: int, db: Session) -> None:
    """Mark a match as sent after email delivery."""
    match = db.get(ProactiveMatch, match_id)
    if not match:
        raise ValueError("Match not found")
    if match.salesperson_id != user_id:
        raise ValueError("Not your match")
    match.status = "sent"
    db.commit()


def expire_old_matches(db: Session) -> int:
    """Expire matches older than proactive_match_expiry_days. Returns count expired."""
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=settings.proactive_match_expiry_days
    )
    expired = (
        db.query(ProactiveMatch)
        .filter(
            ProactiveMatch.status == "new",
            ProactiveMatch.created_at < cutoff,
        )
        .all()
    )
    for m in expired:
        m.status = "expired"
    if expired:
        db.commit()
    return len(expired)
