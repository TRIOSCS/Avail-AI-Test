"""Engagement Scoring — Email Mining v2 Upgrade 4.

Computes a 0-100 engagement score for each VendorCard based on five
weighted metrics derived from AVAIL's outreach and response data.

Metrics:
  1. Response Rate (30%) — % of outreach that got a reply
  2. Ghost Rate (20%) — inverse; penalizes vendors who never reply
  3. Recency (20%) — how recently they last engaged
  4. Response Velocity (15%) — avg hours from outreach to reply
  5. Win Rate (15%) — % of quotes that turned into orders/wins

The score is stored on VendorCard.engagement_score and recomputed
periodically by the scheduler (daily or on-demand). The engagement_score
is surfaced in the UI as a color-coded ring (green ≥70, yellow 40-69, red <40).

Integration: 20% weight in the master vendor ranking algorithm.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

# ── Weights ──
W_RESPONSE_RATE = 0.30
W_GHOST_RATE = 0.20
W_RECENCY = 0.20
W_VELOCITY = 0.15
W_WIN_RATE = 0.15

# ── Thresholds ──
COLD_START_SCORE = 50  # All vendors start at 50 and move up/down with data
MIN_OUTREACH_FOR_SCORE = 2  # Need at least 2 outreach events to compute
VELOCITY_IDEAL_HOURS = 4  # ≤4h = perfect score
VELOCITY_MAX_HOURS = 168  # ≥168h (7 days) = zero score
RECENCY_IDEAL_DAYS = 7  # Contacted within 7 days = perfect
RECENCY_MAX_DAYS = 365  # Over a year = zero score


def compute_engagement_score(
    total_outreach: int,
    total_responses: int,
    total_wins: int,
    avg_velocity_hours: float | None,
    last_contact_at: datetime | None,
    now: datetime | None = None,
) -> dict:
    """Compute engagement score from raw metrics.

    Returns:
        {
            "engagement_score": float (0-100),
            "response_rate": float (0-1),
            "ghost_rate": float (0-1),
            "recency_score": float (0-100),
            "velocity_score": float (0-100),
            "win_rate": float (0-1),
        }
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if total_outreach < MIN_OUTREACH_FOR_SCORE:
        return {
            "engagement_score": COLD_START_SCORE,
            "response_rate": 0,
            "ghost_rate": 1.0 if total_outreach > 0 and total_responses == 0 else 0,
            "recency_score": 0,
            "velocity_score": 0,
            "win_rate": 0,
        }

    # ── 1. Response Rate (0-100) ──
    response_rate = (
        min(total_responses / total_outreach, 1.0) if total_outreach > 0 else 0
    )
    response_score = response_rate * 100

    # ── 2. Ghost Rate penalty (0-100, inverted: 0 ghosts = 100) ──
    ghost_rate = 1.0 - response_rate if total_outreach > 0 else 1.0
    ghost_score = (1.0 - ghost_rate) * 100

    # ── 3. Recency (0-100) ──
    recency_score = 0.0
    if last_contact_at:
        if last_contact_at.tzinfo is None:
            last_contact_at = last_contact_at.replace(tzinfo=timezone.utc)
        days_since = max((now - last_contact_at).total_seconds() / 86400, 0)
        if days_since <= RECENCY_IDEAL_DAYS:
            recency_score = 100.0
        elif days_since >= RECENCY_MAX_DAYS:
            recency_score = 0.0
        else:
            # Linear decay from ideal to max
            recency_score = max(
                0,
                100
                * (
                    1
                    - (days_since - RECENCY_IDEAL_DAYS)
                    / (RECENCY_MAX_DAYS - RECENCY_IDEAL_DAYS)
                ),
            )

    # ── 4. Response Velocity (0-100) ──
    velocity_score = 0.0
    if avg_velocity_hours is not None and avg_velocity_hours >= 0:
        if avg_velocity_hours <= VELOCITY_IDEAL_HOURS:
            velocity_score = 100.0
        elif avg_velocity_hours >= VELOCITY_MAX_HOURS:
            velocity_score = 0.0
        else:
            velocity_score = max(
                0,
                100
                * (
                    1
                    - (avg_velocity_hours - VELOCITY_IDEAL_HOURS)
                    / (VELOCITY_MAX_HOURS - VELOCITY_IDEAL_HOURS)
                ),
            )

    # ── 5. Win Rate (0-100) ──
    win_rate = min(total_wins / total_responses, 1.0) if total_responses > 0 else 0
    win_score = win_rate * 100

    # ── Weighted composite ──
    engagement_score = (
        response_score * W_RESPONSE_RATE
        + ghost_score * W_GHOST_RATE
        + recency_score * W_RECENCY
        + velocity_score * W_VELOCITY
        + win_score * W_WIN_RATE
    )

    return {
        "engagement_score": round(engagement_score, 1),
        "response_rate": round(response_rate, 3),
        "ghost_rate": round(ghost_rate, 3),
        "recency_score": round(recency_score, 1),
        "velocity_score": round(velocity_score, 1),
        "win_rate": round(win_rate, 3),
    }


async def compute_all_engagement_scores(db: Session) -> dict:
    """Recompute engagement scores for all VendorCards that have outreach data.

    Gathers metrics from:
    - Contact table (outreach count per vendor)
    - VendorResponse table (response count, velocity)
    - Offer table (win count via status='accepted')
    - VendorCard.last_contact_at (recency)

    Returns: {"updated": int, "skipped": int}
    """
    from app.models import VendorCard, Contact, VendorResponse, Offer
    from app.vendor_utils import normalize_vendor_name

    now = datetime.now(timezone.utc)

    # ── Gather outreach counts per vendor ──
    # Count outbound Contact records grouped by normalized vendor_name
    outreach_rows = (
        db.query(
            Contact.vendor_name,
            func.count(Contact.id).label("total_outreach"),
        )
        .filter(Contact.contact_type == "email")
        .group_by(Contact.vendor_name)
        .all()
    )

    # Build vendor_name → outreach count map
    outreach_map = {}
    for row in outreach_rows:
        norm = normalize_vendor_name(row.vendor_name or "")
        outreach_map[norm] = outreach_map.get(norm, 0) + row.total_outreach

    # ── Build email-domain → vendor card normalized_name map ──
    # VendorResponse.vendor_name stores the sender's personal name (e.g. "Michael
    # Khoury"), not the company name. We match responses to vendor cards by the
    # email domain (e.g. trioscs.com → "trio supply chain solutions").
    domain_to_norm = {}
    for card in db.query(VendorCard).filter(VendorCard.domain.isnot(None)).all():
        domain_to_norm[card.domain.lower()] = card.normalized_name
        for alias in (card.domain_aliases or []):
            if alias:
                domain_to_norm[alias.lower()] = card.normalized_name

    # ── Gather response counts per vendor (matched by email domain) ──
    response_rows = (
        db.query(
            VendorResponse.vendor_email,
            func.count(VendorResponse.id).label("total_responses"),
        )
        .filter(VendorResponse.status != "noise")
        .filter(VendorResponse.vendor_email.isnot(None))
        .group_by(VendorResponse.vendor_email)
        .all()
    )

    response_map = {}
    for row in response_rows:
        email = (row.vendor_email or "").lower()
        if "@" not in email:
            continue
        domain = email.split("@", 1)[1]
        norm = domain_to_norm.get(domain)
        if not norm:
            # Fallback: try matching by vendor_name directly
            norm = normalize_vendor_name(row.vendor_email.split("@")[0])
        if norm:
            response_map[norm] = response_map.get(norm, 0) + row.total_responses

    # ── Compute average response velocity per vendor ──
    # Velocity = avg time between outbound Contact.created_at and VendorResponse.received_at
    # Matched via contact_id FK, attributed to vendor card via Contact.vendor_name
    velocity_map = {}
    matched_pairs = (
        db.query(
            Contact.vendor_name,
            Contact.created_at.label("sent_at"),
            VendorResponse.received_at,
        )
        .join(VendorResponse, VendorResponse.contact_id == Contact.id)
        .filter(
            VendorResponse.received_at.isnot(None),
            Contact.created_at.isnot(None),
        )
        .all()
    )

    vendor_velocities = {}
    for pair in matched_pairs:
        norm = normalize_vendor_name(pair.vendor_name or "")
        sent = pair.sent_at
        received = pair.received_at
        if sent and received:
            if sent.tzinfo is None:
                sent = sent.replace(tzinfo=timezone.utc)
            if received.tzinfo is None:
                received = received.replace(tzinfo=timezone.utc)
            hours = max((received - sent).total_seconds() / 3600, 0)
            if hours < 720:  # Ignore >30 days as outliers
                vendor_velocities.setdefault(norm, []).append(hours)

    for norm, hours_list in vendor_velocities.items():
        if hours_list:
            velocity_map[norm] = sum(hours_list) / len(hours_list)

    # ── Gather win counts (won offers) per vendor ──
    win_rows = (
        db.query(
            Offer.vendor_name,
            func.count(Offer.id).label("total_wins"),
        )
        .filter(Offer.status == "won")
        .group_by(Offer.vendor_name)
        .all()
    )

    win_map = {}
    for row in win_rows:
        norm = normalize_vendor_name(row.vendor_name or "")
        win_map[norm] = win_map.get(norm, 0) + row.total_wins

    # ── Update ALL VendorCards in batches (avoid loading 10k+ ORM objects at once) ──
    all_norms = set(outreach_map.keys()) | set(response_map.keys())
    total_count = db.query(func.count(VendorCard.id)).scalar() or 0

    updated = 0
    skipped = 0
    BATCH_SIZE = 1000

    for offset in range(0, total_count, BATCH_SIZE):
        cards = (
            db.query(VendorCard)
            .order_by(VendorCard.id)
            .offset(offset)
            .limit(BATCH_SIZE)
            .all()
        )

        for card in cards:
            norm = card.normalized_name
            outreach = outreach_map.get(norm, card.total_outreach or 0)
            responses = response_map.get(norm, card.total_responses or 0)
            wins = win_map.get(norm, card.total_wins or 0)
            velocity = velocity_map.get(norm)

            result = compute_engagement_score(
                total_outreach=outreach,
                total_responses=responses,
                total_wins=wins,
                avg_velocity_hours=velocity,
                last_contact_at=card.last_contact_at,
                now=now,
            )

            # Update card
            card.total_outreach = outreach
            card.total_responses = responses
            card.total_wins = wins
            card.ghost_rate = result["ghost_rate"]
            card.response_velocity_hours = velocity
            card.engagement_score = result["engagement_score"]
            card.engagement_computed_at = now

            # Compute relationship_months
            if card.created_at:
                created = card.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                card.relationship_months = max(int((now - created).days / 30), 0)

            updated += 1

        # Flush each batch to free memory
        try:
            db.flush()
        except Exception as e:
            log.error(f"Engagement scoring flush failed at offset {offset}: {e}")

    try:
        db.commit()
        log.info(
            f"Engagement scoring: updated {updated} vendor cards, skipped {skipped}"
        )
    except Exception as e:
        log.error(f"Engagement scoring commit failed: {e}")
        db.rollback()
        return {"updated": 0, "skipped": skipped}

    return {"updated": updated, "skipped": skipped}


def compute_single_vendor_score(card, db: Session) -> float | None:
    """Compute engagement score for a single VendorCard. Returns score or None."""
    from app.models import Contact, VendorResponse, Offer

    norm = card.normalized_name
    now = datetime.now(timezone.utc)

    outreach = (
        db.query(func.count(Contact.id))
        .filter(Contact.contact_type == "email")
        .filter(func.lower(Contact.vendor_name) == norm)
        .scalar()
        or 0
    )

    # Match responses by email domain (vendor_name stores person name, not company)
    domains = [card.domain.lower()] if card.domain else []
    for alias in (card.domain_aliases or []):
        if alias:
            domains.append(alias.lower())

    responses = 0
    if domains:
        for domain in domains:
            responses += (
                db.query(func.count(VendorResponse.id))
                .filter(VendorResponse.vendor_email.ilike(f"%@{domain}"))
                .filter(VendorResponse.status != "noise")
                .scalar()
                or 0
            )

    wins = (
        db.query(func.count(Offer.id))
        .filter(func.lower(Offer.vendor_name) == norm)
        .filter(Offer.status == "won")
        .scalar()
        or 0
    )

    result = compute_engagement_score(
        total_outreach=outreach,
        total_responses=responses,
        total_wins=wins,
        avg_velocity_hours=card.response_velocity_hours,
        last_contact_at=card.last_contact_at,
        now=now,
    )

    return result["engagement_score"]


def apply_outbound_stats(db: Session, vendors_contacted: dict[str, int]):
    """Apply outbound RFQ counts from scan_sent_items to VendorCards.

    Called after EmailMiner.scan_sent_items() returns vendor domain → count.
    Increments VendorCard.total_outreach for matching vendors.
    """
    from app.models import VendorCard

    if not vendors_contacted:
        return 0

    updated = 0
    for domain, count in vendors_contacted.items():
        # Find vendor card by domain
        card = (
            db.query(VendorCard)
            .filter(func.lower(VendorCard.domain) == domain.lower())
            .first()
        )

        # Fallback: try matching by normalized name (strip TLD)
        if not card:
            vendor_key = domain.split(".")[0] if "." in domain else domain
            card = (
                db.query(VendorCard)
                .filter(func.lower(VendorCard.normalized_name) == vendor_key.lower())
                .first()
            )

        if card:
            card.total_outreach = (card.total_outreach or 0) + count
            card.last_contact_at = datetime.now(timezone.utc)
            updated += 1

    if updated:
        try:
            db.flush()
            log.info(f"Applied outbound stats to {updated} vendor card(s)")
        except Exception as e:
            log.error(f"Outbound stats flush failed: {e}")
            db.rollback()

    return updated
