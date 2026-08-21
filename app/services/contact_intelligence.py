"""Contact Intelligence Service — contact scoring and relationship summaries.

Contacts emerge from activity: emails parsed, calls logged, RFQs exchanged.
Never manually created — only enriched and scored.

Core functions:
  - split_name: Split a full name into (first, last)
  - compute_contact_relationship_score: Weighted 0-100 score
  - compute_all_contact_scores: Nightly batch job
  - generate_contact_summary: Relationship summary
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import sqlalchemy.exc
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ..utils.async_helpers import run_coro_blocking

# ── Name splitting ──────────────────────────────────────────────────


def split_name(full_name: str | None) -> tuple[str | None, str | None]:
    """Split a full name into (first_name, last_name).

    Keeps surname prefixes (de, van, etc.) with the last name; handles single-word
    names.
    """
    if not full_name or not full_name.strip():
        return (None, None)

    parts = full_name.strip().split()
    if len(parts) == 1:
        return (parts[0], None)

    # First word is the first name; everything after is the last name. This keeps
    # surname prefixes attached: "John van der Berg" → ("John", "van der Berg").
    return (parts[0], " ".join(parts[1:]))


# ── Contact relationship scoring ────────────────────────────────────


# Weight constants (sum = 1.0)
W_RECENCY = 0.25
W_FREQUENCY = 0.25
W_RESPONSIVENESS = 0.20
W_WIN_RATE = 0.15
W_CHANNEL_DIVERSITY = 0.15

RECENCY_IDEAL_DAYS = 7
RECENCY_MAX_DAYS = 365
FREQUENCY_IDEAL_30D = 10
RESPONSIVENESS_IDEAL_HOURS = 4
RESPONSIVENESS_MAX_HOURS = 168


def compute_contact_relationship_score(
    last_interaction_at: datetime | None,
    interactions_30d: int,
    interactions_60d: int,
    interactions_90d: int,
    avg_response_hours: float | None,
    wins: int,
    total_interactions: int,
    distinct_channels: int,
    now: datetime | None = None,
) -> dict:
    """Compute a 0-100 relationship score for a vendor contact.

    Returns: {relationship_score, recency_score, frequency_score,
              responsiveness_score, win_rate_score, channel_score, activity_trend}
    """
    now = now or datetime.now(UTC)

    # Recency: 0-7d = 100, decays linearly to 0 at 365d
    if last_interaction_at:
        lia = last_interaction_at.replace(tzinfo=last_interaction_at.tzinfo or UTC)
        days_since = max((now - lia).total_seconds() / 86400, 0)
        if days_since <= RECENCY_IDEAL_DAYS:
            recency = 100.0
        elif days_since >= RECENCY_MAX_DAYS:
            recency = 0.0
        else:
            recency = max(
                0.0, 100.0 * (1.0 - (days_since - RECENCY_IDEAL_DAYS) / (RECENCY_MAX_DAYS - RECENCY_IDEAL_DAYS))
            )
    else:
        recency = 0.0

    # Frequency: 10+/30d = 100
    frequency = min(100.0, (interactions_30d / FREQUENCY_IDEAL_30D) * 100.0) if FREQUENCY_IDEAL_30D > 0 else 0.0

    # Responsiveness: ≤4h = 100, ≥168h = 0
    if avg_response_hours is not None and avg_response_hours >= 0:
        if avg_response_hours <= RESPONSIVENESS_IDEAL_HOURS:
            responsiveness = 100.0
        elif avg_response_hours >= RESPONSIVENESS_MAX_HOURS:
            responsiveness = 0.0
        else:
            responsiveness = max(
                0.0,
                100.0
                * (
                    1.0
                    - (avg_response_hours - RESPONSIVENESS_IDEAL_HOURS)
                    / (RESPONSIVENESS_MAX_HOURS - RESPONSIVENESS_IDEAL_HOURS)
                ),
            )
    else:
        responsiveness = 50.0  # Unknown defaults to neutral

    # Win rate: wins / total interactions
    if total_interactions > 0 and wins > 0:
        win_rate = min(100.0, (wins / total_interactions) * 100.0)
    else:
        win_rate = 0.0

    # Channel diversity: 3+ distinct channels = 100
    channel_score = min(100.0, (distinct_channels / 3.0) * 100.0) if distinct_channels > 0 else 0.0

    # Weighted sum
    score = (
        W_RECENCY * recency
        + W_FREQUENCY * frequency
        + W_RESPONSIVENESS * responsiveness
        + W_WIN_RATE * win_rate
        + W_CHANNEL_DIVERSITY * channel_score
    )

    trend = _compute_trend(interactions_30d, interactions_60d, interactions_90d)

    return {
        "relationship_score": round(score, 1),
        "recency_score": round(recency, 1),
        "frequency_score": round(frequency, 1),
        "responsiveness_score": round(responsiveness, 1),
        "win_rate_score": round(win_rate, 1),
        "channel_score": round(channel_score, 1),
        "activity_trend": trend,
    }


def _compute_trend(interactions_30d: int, interactions_60d: int, interactions_90d: int) -> str:
    """Determine activity trend from interaction windows."""
    if interactions_30d == 0 and interactions_60d == 0 and interactions_90d == 0:
        return "dormant"

    # Compute per-30d rate for older period
    older_rate = (interactions_90d - interactions_30d) / 2.0 if interactions_90d > interactions_30d else 0.0

    if older_rate <= 0 and interactions_30d > 0:
        return "warming"

    if older_rate > 0:
        if interactions_30d > 1.5 * older_rate:
            return "warming"
        if interactions_30d < 0.5 * older_rate:
            return "cooling"

    return "stable"


def compute_all_contact_scores(db: Session) -> dict:
    """Batch-compute scores for all VendorContacts.

    Returns {updated: int, skipped: int}.
    """
    from ..models import ActivityLog, VendorContact

    now = datetime.now(UTC)
    cutoff_30d = now - timedelta(days=30)
    cutoff_60d = now - timedelta(days=60)
    cutoff_90d = now - timedelta(days=90)

    contacts = db.query(VendorContact).limit(5000).all()
    if not contacts:
        return {"updated": 0, "skipped": 0}

    contact_ids = [c.id for c in contacts]

    # Batch query: interaction counts per contact per window
    def _count_window(since):
        rows = (
            db.query(
                ActivityLog.vendor_contact_id,
                sqlfunc.count(ActivityLog.id),
            )
            .filter(
                ActivityLog.vendor_contact_id.in_(contact_ids),
                ActivityLog.occurred_at >= since,
            )
            .group_by(ActivityLog.vendor_contact_id)
            .all()
        )
        return {r[0]: r[1] for r in rows}

    counts_30d = _count_window(cutoff_30d)
    counts_60d = _count_window(cutoff_60d)
    counts_90d = _count_window(cutoff_90d)

    # Channel diversity per contact
    channel_rows = (
        db.query(
            ActivityLog.vendor_contact_id,
            sqlfunc.count(sqlfunc.distinct(ActivityLog.channel)),
        )
        .filter(ActivityLog.vendor_contact_id.in_(contact_ids))
        .group_by(ActivityLog.vendor_contact_id)
        .all()
    )
    channel_map = {r[0]: r[1] for r in channel_rows}

    # Win counts: activities with type containing 'won' or 'po_issued'
    win_rows = (
        db.query(
            ActivityLog.vendor_contact_id,
            sqlfunc.count(ActivityLog.id),
        )
        .filter(
            ActivityLog.vendor_contact_id.in_(contact_ids),
            ActivityLog.activity_type.in_(["po_issued", "quote_won", "deal_won"]),
        )
        .group_by(ActivityLog.vendor_contact_id)
        .all()
    )
    win_map = {r[0]: r[1] for r in win_rows}

    # Total interactions per contact
    total_rows = (
        db.query(
            ActivityLog.vendor_contact_id,
            sqlfunc.count(ActivityLog.id),
        )
        .filter(ActivityLog.vendor_contact_id.in_(contact_ids))
        .group_by(ActivityLog.vendor_contact_id)
        .all()
    )
    total_map = {r[0]: r[1] for r in total_rows}

    updated = 0
    skipped = 0
    batch = []

    # Build vendor_card_id → avg_response_hours map
    vc_ids = {c.vendor_card_id for c in contacts if c.vendor_card_id}
    response_hours_map: dict[int, float | None] = {}
    if vc_ids:
        from app.models import VendorCard

        vc_rows = (
            db.query(VendorCard.id, VendorCard.avg_response_hours)
            .filter(
                VendorCard.id.in_(vc_ids),
                VendorCard.avg_response_hours.isnot(None),
            )
            .all()
        )
        response_hours_map = {r[0]: r[1] for r in vc_rows}

    for contact in contacts:
        cid = contact.id
        i30 = counts_30d.get(cid, 0)
        i60 = counts_60d.get(cid, 0)
        i90 = counts_90d.get(cid, 0)

        result = compute_contact_relationship_score(
            last_interaction_at=contact.last_interaction_at,
            interactions_30d=i30,
            interactions_60d=i60,
            interactions_90d=i90,
            avg_response_hours=response_hours_map.get(contact.vendor_card_id),
            wins=win_map.get(cid, 0),
            total_interactions=total_map.get(cid, 0),
            distinct_channels=channel_map.get(cid, 0),
            now=now,
        )

        contact.relationship_score = result["relationship_score"]
        contact.activity_trend = result["activity_trend"]
        contact.score_computed_at = now
        batch.append(contact)
        updated += 1

        # Flush in batches of 500
        if len(batch) >= 500:
            try:
                db.flush()
                batch = []
            except sqlalchemy.exc.SQLAlchemyError as e:
                logger.warning("Batch score flush error: {}", e)
                db.rollback()
                skipped += len(batch)
                batch = []

    # Final flush
    if batch:
        try:
            db.flush()
        except sqlalchemy.exc.SQLAlchemyError as e:
            logger.warning("Final score flush error: {}", e)
            db.rollback()
            skipped += len(batch)

    try:
        db.commit()
    except sqlalchemy.exc.SQLAlchemyError as e:
        logger.error("Score commit error: {}", e)
        db.rollback()

    return {"updated": updated, "skipped": skipped}


def generate_contact_summary(db: Session, vendor_card_id: int, contact_id: int) -> str:
    """Generate an AI-powered summary of a contact's relationship."""
    from ..models import ActivityLog, VendorContact

    contact = db.get(VendorContact, contact_id)
    if not contact or contact.vendor_card_id != vendor_card_id:
        return "Contact not found."

    # Get recent activity for context
    activities = (
        db.query(ActivityLog)
        .filter(ActivityLog.vendor_contact_id == contact_id)
        .order_by(ActivityLog.occurred_at.desc())
        .limit(10)
        .all()
    )

    activity_summary = []
    for a in activities:
        date_str = a.occurred_at.strftime("%Y-%m-%d") if a.occurred_at else "unknown"
        activity_summary.append(f"- {date_str}: {a.activity_type} via {a.channel}")

    context = (
        f"Contact: {contact.full_name or contact.email}\n"
        f"Title: {contact.title or 'Unknown'}\n"
        f"Score: {contact.relationship_score or 'N/A'}/100\n"
        f"Trend: {contact.activity_trend or 'Unknown'}\n"
        f"Total interactions: {contact.interaction_count or 0}\n"
        f"Recent activity:\n" + ("\n".join(activity_summary) if activity_summary else "No recent activity")
    )

    try:
        from app.utils.claude_client import claude_text

        prompt = (
            f"Write a 2-3 sentence relationship summary for this vendor contact:\n\n{context}\n\n"
            f"Focus on the health of the relationship and any recommended actions."
        )
        result = run_coro_blocking(
            claude_text(
                prompt, system="You are a B2B relationship analyst. Be concise.", model_tier="fast", timeout=15
            ),
            timeout=20,
        )
        if result:
            return result
    except Exception as e:
        logger.warning("AI summary failed: {}", e)

    # Fallback: template-based summary
    trend_desc = {
        "warming": "improving",
        "stable": "steady",
        "cooling": "declining",
        "dormant": "inactive",
    }.get(contact.activity_trend or "", "unknown")

    return (
        f"{contact.full_name or 'This contact'} has had {contact.interaction_count or 0} "
        f"interactions. The relationship trend is {trend_desc} with a score of "
        f"{contact.relationship_score or 0:.0f}/100."
    )
