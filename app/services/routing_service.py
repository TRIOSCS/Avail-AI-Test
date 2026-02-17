"""Buyer routing service — expertise-based assignment with 48-hour waterfall.

Scoring algorithm ranks all buyers for a given requirement+vendor pair:
  1. Brand match (40pts) — buyer's brand_specialties vs requirement.brand
  2. Commodity match (25pts) — buyer's primary/secondary commodity vs part type
  3. Geography match (15pts) — buyer's primary_geography vs vendor region
  4. Vendor relationship (20pts) — buyer_vendor_stats win_rate + response_rate

Top-3 buyers are assigned. First buyer to enter an offer within 48 hours
claims the routing. If nobody claims, assignment expires.

Usage:
    # When a new sighting arrives or vendor is matched to a requirement
    assignment = create_routing_assignment(requirement_id, vendor_card_id, db)

    # When a buyer enters an offer
    claim_routing(assignment_id, buyer_id, db)

    # Nightly cron — expire stale assignments
    expire_stale_assignments(db)

    # Also: expire stale offers (14-day TTL)
    expire_stale_offers(db)
"""

import html
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy.orm import Session

from app.models import (
    RoutingAssignment,
    BuyerProfile,
    BuyerVendorStats,
    Requirement,
    VendorCard,
    Offer,
    User,
)
from app.config import settings

log = logging.getLogger("avail.routing")


# ═══════════════════════════════════════════════════════════════════════
#  SCORING ENGINE
# ═══════════════════════════════════════════════════════════════════════

# Weight constants
W_BRAND = 40
W_COMMODITY = 25
W_GEOGRAPHY = 15
W_RELATIONSHIP = 20


def score_buyer(
    profile: BuyerProfile,
    stats: BuyerVendorStats | None,
    requirement: Requirement,
    vendor: VendorCard,
) -> dict:
    """Score a single buyer for a requirement+vendor pair.

    Returns {total, brand, commodity, geography, relationship, breakdown}.
    """
    brand_score = _score_brand(profile, requirement)
    commodity_score = _score_commodity(profile, requirement)
    geography_score = _score_geography(profile, vendor)
    relationship_score = _score_relationship(stats)

    total = brand_score + commodity_score + geography_score + relationship_score

    return {
        "total": round(total, 2),
        "brand": round(brand_score, 2),
        "commodity": round(commodity_score, 2),
        "geography": round(geography_score, 2),
        "relationship": round(relationship_score, 2),
        "breakdown": {
            "brand_match": brand_score > 0,
            "commodity_match": commodity_score > 0,
            "geography_match": geography_score > 0,
            "has_history": stats is not None and (stats.rfqs_sent or 0) > 0,
        },
    }


def _score_brand(profile: BuyerProfile, requirement: Requirement) -> float:
    """Brand specialty match: full weight if the requirement's brand is in the buyer's specialties."""
    if not requirement.brand or not profile.brand_specialties:
        return 0.0

    req_brand = requirement.brand.strip().lower()
    specialties = [b.strip().lower() for b in (profile.brand_specialties or [])]

    if req_brand in specialties:
        return float(W_BRAND)

    # Partial match: brand contains or is contained
    for s in specialties:
        if req_brand in s or s in req_brand:
            return float(W_BRAND) * 0.5

    return 0.0


def _score_commodity(profile: BuyerProfile, requirement: Requirement) -> float:
    """Commodity match based on part type heuristics.

    Checks if requirement brand/MPN suggests a commodity category that
    matches the buyer's primary or secondary commodity.
    """
    if not profile.primary_commodity:
        return 0.0

    # Infer commodity from requirement context
    req_commodity = _infer_commodity(requirement)
    if not req_commodity:
        return (
            float(W_COMMODITY) * 0.25
        )  # Small baseline — can't determine, don't penalize

    if profile.primary_commodity == req_commodity:
        return float(W_COMMODITY)
    if profile.secondary_commodity == req_commodity:
        return float(W_COMMODITY) * 0.6

    return 0.0


def _score_geography(profile: BuyerProfile, vendor: VendorCard) -> float:
    """Geography match: buyer's primary_geography vs vendor's country/region."""
    if not profile.primary_geography or not vendor.hq_country:
        return 0.0

    vendor_region = _country_to_region(vendor.hq_country)
    if not vendor_region:
        return 0.0

    if profile.primary_geography == vendor_region:
        return float(W_GEOGRAPHY)

    # "global" geography gets partial credit
    if profile.primary_geography == "global":
        return float(W_GEOGRAPHY) * 0.5

    return 0.0


def _score_relationship(stats: BuyerVendorStats | None) -> float:
    """Vendor relationship score based on historical performance."""
    if not stats or (stats.rfqs_sent or 0) == 0:
        return 0.0

    # Composite: 60% response rate + 40% win rate, scaled to W_RELATIONSHIP
    response_factor = min((stats.response_rate or 0) / 100.0, 1.0) * 0.6
    win_factor = min((stats.win_rate or 0) / 100.0, 1.0) * 0.4

    return float(W_RELATIONSHIP) * (response_factor + win_factor)


# ═══════════════════════════════════════════════════════════════════════
#  INFERENCE HELPERS
# ═══════════════════════════════════════════════════════════════════════

# Brand→commodity and country→region maps loaded from config/routing_maps.json
# Edit the JSON file and call POST /api/admin/reload-routing-maps to update.
from app.routing_maps import get_brand_commodity_map, get_country_region_map


def _infer_commodity(requirement: Requirement) -> str | None:
    """Infer commodity category from the requirement's brand."""
    if requirement.brand:
        brand_lower = requirement.brand.strip().lower()
        brand_map = get_brand_commodity_map()
        if brand_lower in brand_map:
            return brand_map[brand_lower]
    return None


def _country_to_region(country: str) -> str | None:
    """Map a country name/code to a broad region."""
    if not country:
        return None
    return get_country_region_map().get(country.strip().lower())


# ═══════════════════════════════════════════════════════════════════════
#  ASSIGNMENT ENGINE — rank buyers, create routing
# ═══════════════════════════════════════════════════════════════════════


def rank_buyers_for_assignment(
    requirement_id: int,
    vendor_card_id: int,
    db: Session,
) -> list[dict]:
    """Rank all active buyers for a requirement+vendor pair.

    Returns sorted list of {user_id, user_name, score_details}.
    """
    requirement = db.get(Requirement, requirement_id)
    vendor = db.get(VendorCard, vendor_card_id)
    if not requirement or not vendor:
        return []

    # Get all buyer profiles
    profiles = db.query(BuyerProfile).all()
    if not profiles:
        return []

    results = []
    for profile in profiles:
        # Get this buyer's stats with this vendor
        stats = (
            db.query(BuyerVendorStats)
            .filter(
                BuyerVendorStats.user_id == profile.user_id,
                BuyerVendorStats.vendor_card_id == vendor_card_id,
            )
            .first()
        )

        score = score_buyer(profile, stats, requirement, vendor)

        user = db.get(User, profile.user_id)
        results.append(
            {
                "user_id": profile.user_id,
                "user_name": user.name if user else "Unknown",
                "score_details": score,
            }
        )

    # Sort by total score descending
    results.sort(key=lambda x: x["score_details"]["total"], reverse=True)
    return results


async def rank_buyers_with_availability(
    requirement_id: int,
    vendor_card_id: int,
    db: Session,
) -> list[dict]:
    """Rank buyers with calendar availability filtering.

    Wraps rank_buyers_for_assignment and removes buyers who are OOO today.
    Falls back to the full list if calendar checks fail entirely.
    """
    from datetime import date

    ranked = rank_buyers_for_assignment(requirement_id, vendor_card_id, db)
    if not ranked:
        return ranked

    try:
        from app.services.calendar import is_buyer_available
        check_date = date.today()
        available = []
        for entry in ranked:
            if await is_buyer_available(entry["user_id"], check_date, db):
                available.append(entry)
            else:
                entry["score_details"]["calendar_ooo"] = True
                log.info(f"Buyer {entry['user_id']} ({entry['user_name']}) skipped — OOO on {check_date}")
        # If ALL buyers are OOO, fall back to the full list (don't leave nobody)
        return available if available else ranked
    except Exception as e:
        log.debug(f"Calendar availability check skipped: {e}")
        return ranked


def create_routing_assignment(
    requirement_id: int,
    vendor_card_id: int,
    db: Session,
) -> RoutingAssignment | None:
    """Create a new routing assignment with top-3 buyer ranking.

    Returns the assignment, or None if no buyers are available.
    Skips if an active assignment already exists for this pair.
    """
    # Check for existing active assignment
    existing = (
        db.query(RoutingAssignment)
        .filter(
            RoutingAssignment.requirement_id == requirement_id,
            RoutingAssignment.vendor_card_id == vendor_card_id,
            RoutingAssignment.status == "active",
        )
        .first()
    )
    if existing:
        return existing

    ranked = rank_buyers_for_assignment(requirement_id, vendor_card_id, db)
    if not ranked:
        return None

    now = datetime.now(timezone.utc)
    top3 = ranked[:3]

    assignment = RoutingAssignment(
        requirement_id=requirement_id,
        vendor_card_id=vendor_card_id,
        buyer_1_id=top3[0]["user_id"] if len(top3) > 0 else None,
        buyer_2_id=top3[1]["user_id"] if len(top3) > 1 else None,
        buyer_3_id=top3[2]["user_id"] if len(top3) > 2 else None,
        buyer_1_score=top3[0]["score_details"]["total"] if len(top3) > 0 else None,
        buyer_2_score=top3[1]["score_details"]["total"] if len(top3) > 1 else None,
        buyer_3_score=top3[2]["score_details"]["total"] if len(top3) > 2 else None,
        scoring_details=ranked[:5],  # Store top-5 for transparency
        assigned_at=now,
        expires_at=now + timedelta(hours=settings.routing_window_hours),
        status="active",
    )
    db.add(assignment)
    db.flush()

    log.info(
        f"Routing assignment created: req={requirement_id} vendor={vendor_card_id} "
        f"top3=[{', '.join(str(t['user_id']) for t in top3)}]"
    )
    return assignment


# ═══════════════════════════════════════════════════════════════════════
#  CLAIM — first buyer to enter offer wins
# ═══════════════════════════════════════════════════════════════════════


def claim_routing(assignment_id: int, buyer_id: int, db: Session) -> dict:
    """Claim a routing assignment by entering an offer.

    Returns {success, message, assignment_id}.
    """
    assignment = db.get(RoutingAssignment, assignment_id)
    if not assignment:
        return {"success": False, "message": "Assignment not found"}

    if assignment.status == "claimed":
        return {
            "success": False,
            "message": f"Already claimed by user {assignment.claimed_by_id}",
        }

    if assignment.status == "expired":
        return {"success": False, "message": "Assignment has expired"}

    now = datetime.now(timezone.utc)
    if now >= assignment.expires_at:
        assignment.status = "expired"
        db.flush()
        return {"success": False, "message": "Assignment has expired"}

    # Verify buyer is in the top-3 (or allow any if past 24 hours for the waterfall)
    is_top3 = buyer_id in [
        assignment.buyer_1_id,
        assignment.buyer_2_id,
        assignment.buyer_3_id,
    ]
    hours_elapsed = (now - assignment.assigned_at).total_seconds() / 3600

    if not is_top3 and hours_elapsed < 24:
        return {
            "success": False,
            "message": "Only top-3 ranked buyers can claim in the first 24 hours",
        }

    assignment.claimed_by_id = buyer_id
    assignment.claimed_at = now
    assignment.status = "claimed"
    db.flush()

    log.info(
        f"Routing claimed: assignment={assignment_id} by user={buyer_id} "
        f"(top3={is_top3}, hours={hours_elapsed:.1f})"
    )
    return {
        "success": True,
        "message": "Routing claimed",
        "assignment_id": assignment_id,
    }


# ═══════════════════════════════════════════════════════════════════════
#  EXPIRATION SWEEPS
# ═══════════════════════════════════════════════════════════════════════


def expire_stale_assignments(db: Session) -> int:
    """Expire routing assignments past their 48-hour window.

    Called by nightly scheduler.
    Returns count of expired assignments.
    """
    now = datetime.now(timezone.utc)
    stale = (
        db.query(RoutingAssignment)
        .filter(
            RoutingAssignment.status == "active",
            RoutingAssignment.expires_at <= now,
        )
        .all()
    )

    for assignment in stale:
        assignment.status = "expired"

    if stale:
        db.flush()
        log.info(f"Expired {len(stale)} routing assignments")

    return len(stale)


def expire_stale_offers(db: Session) -> int:
    """Expire offers past the attribution window (14 days).

    Called by nightly scheduler.
    Returns count of expired offers.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=settings.offer_attribution_days
    )
    stale = (
        db.query(Offer)
        .filter(
            Offer.attribution_status == "active",
            Offer.expires_at.isnot(None),
            Offer.expires_at <= cutoff,
        )
        .all()
    )

    for offer in stale:
        offer.attribution_status = "expired"

    if stale:
        db.flush()
        log.info(
            f"Expired {len(stale)} offers past {settings.offer_attribution_days}-day window"
        )

    return len(stale)


def auto_route_search_results(
    results: dict,
    db: Session,
) -> list[RoutingAssignment]:
    """Create routing assignments for new (requirement, vendor_card) pairs in search results.

    Iterates enriched search results, collects unique pairs, skips those that already
    have an active assignment, and calls create_routing_assignment() for new ones.

    Returns list of newly created assignments.
    """
    # Collect unique (requirement_id, vendor_card_id) pairs from fresh sightings
    pairs: set[tuple[int, int]] = set()
    for req_id_str, group in results.items():
        try:
            req_id = int(req_id_str)
        except (ValueError, TypeError):
            continue
        for s in group.get("sightings", []):
            if s.get("is_historical") or s.get("is_material_history"):
                continue
            card_id = (s.get("vendor_card") or {}).get("card_id")
            if card_id:
                pairs.add((req_id, card_id))

    if not pairs:
        return []

    # Batch-query existing active assignments for these requirement IDs
    req_ids = {p[0] for p in pairs}
    existing = (
        db.query(
            RoutingAssignment.requirement_id,
            RoutingAssignment.vendor_card_id,
        )
        .filter(
            RoutingAssignment.requirement_id.in_(req_ids),
            RoutingAssignment.status == "active",
        )
        .all()
    )
    existing_set = {(r, v) for r, v in existing}

    new_pairs = pairs - existing_set
    if not new_pairs:
        return []

    created = []
    for req_id, card_id in new_pairs:
        try:
            assignment = create_routing_assignment(req_id, card_id, db)
            if assignment:
                created.append(assignment)
        except Exception:
            log.exception(f"Failed to create routing for req={req_id} vendor={card_id}")

    if created:
        db.commit()
        log.info(f"Auto-routed {len(created)} new assignments from search results")

    return created


def reconfirm_offer(offer_id: int, db: Session) -> dict:
    """Reconfirm an offer to extend its TTL by another attribution window.

    Returns {success, message, new_expires_at}.
    """
    offer = db.get(Offer, offer_id)
    if not offer:
        return {"success": False, "message": "Offer not found"}

    if offer.attribution_status == "converted":
        return {
            "success": False,
            "message": "Offer already converted — no reconfirm needed",
        }

    now = datetime.now(timezone.utc)
    offer.reconfirmed_at = now
    offer.reconfirm_count = (offer.reconfirm_count or 0) + 1
    offer.expires_at = now + timedelta(days=settings.offer_attribution_days)
    offer.attribution_status = "active"
    db.flush()

    return {
        "success": True,
        "message": "Offer reconfirmed",
        "new_expires_at": offer.expires_at.isoformat(),
        "reconfirm_count": offer.reconfirm_count,
    }


# ═══════════════════════════════════════════════════════════════════════
#  QUERY HELPERS
# ═══════════════════════════════════════════════════════════════════════


def get_active_assignments_for_buyer(user_id: int, db: Session) -> list[dict]:
    """Get all active routing assignments where the user is in the top-3."""
    now = datetime.now(timezone.utc)
    assignments = (
        db.query(RoutingAssignment)
        .filter(
            RoutingAssignment.status == "active",
            RoutingAssignment.expires_at > now,
            (
                (RoutingAssignment.buyer_1_id == user_id)
                | (RoutingAssignment.buyer_2_id == user_id)
                | (RoutingAssignment.buyer_3_id == user_id)
            ),
        )
        .order_by(RoutingAssignment.expires_at.asc())
        .all()
    )

    return [_assignment_to_dict(a, user_id) for a in assignments]


def get_assignment_details(assignment_id: int, db: Session) -> dict | None:
    """Get full details of a routing assignment."""
    a = db.get(RoutingAssignment, assignment_id)
    if not a:
        return None
    return _assignment_to_dict(a)


def _assignment_to_dict(a: RoutingAssignment, for_user_id: int | None = None) -> dict:
    """Serialize a routing assignment to dict."""
    now = datetime.now(timezone.utc)
    hours_remaining = (
        max(0, (a.expires_at - now).total_seconds() / 3600)
        if a.status == "active"
        else 0
    )

    rank = None
    if for_user_id:
        if a.buyer_1_id == for_user_id:
            rank = 1
        elif a.buyer_2_id == for_user_id:
            rank = 2
        elif a.buyer_3_id == for_user_id:
            rank = 3

    return {
        "id": a.id,
        "requirement_id": a.requirement_id,
        "vendor_card_id": a.vendor_card_id,
        "status": a.status,
        "assigned_at": a.assigned_at.isoformat() if a.assigned_at else None,
        "expires_at": a.expires_at.isoformat() if a.expires_at else None,
        "hours_remaining": round(hours_remaining, 1),
        "buyer_1_id": a.buyer_1_id,
        "buyer_2_id": a.buyer_2_id,
        "buyer_3_id": a.buyer_3_id,
        "buyer_1_score": a.buyer_1_score,
        "buyer_2_score": a.buyer_2_score,
        "buyer_3_score": a.buyer_3_score,
        "claimed_by_id": a.claimed_by_id,
        "claimed_at": a.claimed_at.isoformat() if a.claimed_at else None,
        "my_rank": rank,
    }


# ═══════════════════════════════════════════════════════════════════════
#  NOTIFICATIONS — email top-3 buyers when assigned
# ═══════════════════════════════════════════════════════════════════════


async def notify_routing_assignment(
    assignment: RoutingAssignment,
    db: Session,
) -> int:
    """Send email notifications to top-3 buyers for a routing assignment.

    Uses the first admin user's Graph API token to send from a system account.
    Returns count of emails sent.
    """
    from app.utils.graph_client import GraphClient

    requirement = db.get(Requirement, assignment.requirement_id)
    vendor = db.get(VendorCard, assignment.vendor_card_id)
    if not requirement or not vendor:
        log.warning(
            f"Cannot notify: missing requirement or vendor for assignment {assignment.id}"
        )
        return 0

    # Get admin token for sending (with refresh if near expiry)
    from app.scheduler import get_valid_token

    admin_user = (
        db.query(User)
        .filter(
            User.access_token.isnot(None),
        )
        .first()
    )
    if not admin_user:
        log.warning("No admin token available for routing notifications")
        return 0

    token = await get_valid_token(admin_user, db)
    if not token:
        log.warning("Admin token refresh failed for routing notifications")
        return 0

    client = GraphClient(token)

    buyer_ids = [
        (assignment.buyer_1_id, assignment.buyer_1_score, 1),
        (assignment.buyer_2_id, assignment.buyer_2_score, 2),
        (assignment.buyer_3_id, assignment.buyer_3_score, 3),
    ]

    mpn = requirement.primary_mpn or requirement.oem_pn or "Unknown MPN"
    brand = requirement.brand or "Unknown"
    vendor_name = vendor.display_name or "Unknown Vendor"
    hours = settings.routing_window_hours
    expires_str = (
        assignment.expires_at.strftime("%b %d at %I:%M %p UTC")
        if assignment.expires_at
        else "48 hours"
    )

    sent = 0
    for buyer_id, score, rank in buyer_ids:
        if not buyer_id:
            continue

        buyer = db.get(User, buyer_id)
        if not buyer or not buyer.email:
            continue

        subject = f"🔔 Routing Assignment: {mpn} ({brand}) from {vendor_name}"

        body = f"""<html><body style="font-family: Segoe UI, Arial, sans-serif; color: #333;">
<h2 style="color: #0078d4;">New Routing Assignment</h2>
<p>You've been ranked <strong>#{rank}</strong> for this sourcing opportunity:</p>
<table style="border-collapse: collapse; margin: 16px 0;">
  <tr><td style="padding: 6px 16px 6px 0; font-weight: bold;">MPN:</td><td>{html.escape(str(mpn))}</td></tr>
  <tr><td style="padding: 6px 16px 6px 0; font-weight: bold;">Brand:</td><td>{html.escape(str(brand))}</td></tr>
  <tr><td style="padding: 6px 16px 6px 0; font-weight: bold;">Vendor:</td><td>{html.escape(str(vendor_name))}</td></tr>
  <tr><td style="padding: 6px 16px 6px 0; font-weight: bold;">Your Score:</td><td>{score:.1f} / 100</td></tr>
  <tr><td style="padding: 6px 16px 6px 0; font-weight: bold;">Expires:</td><td>{html.escape(str(expires_str))}</td></tr>
</table>
<p><strong>⏱ You have {hours} hours to claim this assignment</strong> by entering an offer.</p>
<p style="margin-top: 8px; font-size: 13px; color: #666;">
  The first buyer to enter an offer claims the routing. After 24 hours, 
  the assignment opens to all buyers.
</p>
</body></html>"""

        try:
            await client.post_json(
                "/me/sendMail",
                {
                    "message": {
                        "subject": subject,
                        "body": {"contentType": "HTML", "content": body},
                        "toRecipients": [{"emailAddress": {"address": buyer.email}}],
                    },
                    "saveToSentItems": False,
                },
            )
            sent += 1
            log.info(
                f"Routing notification sent to {buyer.email} (rank #{rank}) for assignment {assignment.id}"
            )
        except Exception as e:
            log.error(f"Failed to send routing notification to {buyer.email}: {e}")

    return sent
