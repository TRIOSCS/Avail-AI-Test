"""Contact Intelligence Service — auto-discovery, scoring, nudges.

Contacts emerge from activity: emails parsed, calls logged, RFQs exchanged.
Never manually created — only enriched and scored.

Core functions:
  - process_inbound_email_contact: Full pipeline for email→contact
  - log_pipeline_event: Record vendor interactions as ActivityLog
  - compute_contact_relationship_score: Weighted 0-100 score
  - compute_all_contact_scores: Nightly batch job
  - generate_contact_nudges: AI-powered outreach suggestions
  - generate_contact_summary: Relationship summary
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from ..models import ActivityLog, VendorContact

# ── Name splitting ──────────────────────────────────────────────────


# Common surname prefixes that should stay with the last name
_NAME_PREFIXES = {"de", "del", "van", "von", "di", "da", "la", "le", "el", "al", "bin", "ibn"}


def split_name(full_name: str | None) -> tuple[str | None, str | None]:
    """Split a full name into (first_name, last_name).

    Handles prefixes (de, van, etc.) and single-word names.
    """
    if not full_name or not full_name.strip():
        return (None, None)

    parts = full_name.strip().split()
    if len(parts) == 1:
        return (parts[0], None)

    # Check for surname prefixes: "John van der Berg" → ("John", "van der Berg")
    first = parts[0]
    rest = parts[1:]

    # If second word is a prefix, everything after first is last name
    if rest and rest[0].lower() in _NAME_PREFIXES:
        return (first, " ".join(rest))

    return (first, " ".join(rest))


# ── Contact auto-discovery ──────────────────────────────────────────


def process_inbound_email_contact(
    db: Session,
    sender_email: str,
    sender_name: str | None,
    body: str,
    subject: str | None,
    received_at: datetime | None,
    user_id: int,
    requisition_id: int | None = None,
) -> "VendorContact | None":
    """Full pipeline: parse signature → match/create VendorContact → log interaction.

    Returns the VendorContact if one was created/updated, else None.
    """
    from ..models import ActivityLog, VendorCard, VendorContact

    if not sender_email or "@" not in sender_email:
        return None

    email_lower = sender_email.lower().strip()
    domain = email_lower.split("@")[-1]

    # 1. Parse signature
    sig_data = {}
    try:
        from .signature_parser import cache_signature_extract, extract_signature

        sig_data = _run_sync_or_return_empty(extract_signature, body, sender_name or "", email_lower)
        if sig_data and sig_data.get("confidence", 0) > 0:
            cache_signature_extract(db, email_lower, sig_data)
    except Exception as e:
        logger.debug("Signature extraction failed for %s: %s", email_lower, e)

    # 2. Find matching VendorCard by domain
    card = db.query(VendorCard).filter(VendorCard.domain == domain).first()
    if not card:
        # Try domain_aliases
        from sqlalchemy import String

        card = db.query(VendorCard).filter(VendorCard.domain_aliases.cast(String).contains(domain)).first()

    if not card:
        return None

    # 3. Create/update VendorContact
    full_name = sig_data.get("full_name") or sender_name
    first, last = split_name(full_name)
    title = sig_data.get("title")
    phone = sig_data.get("phone")
    phone_mobile = sig_data.get("mobile")
    linkedin = sig_data.get("linkedin_url")

    vc = (
        db.query(VendorContact)
        .filter(
            VendorContact.vendor_card_id == card.id,
            VendorContact.email == email_lower,
        )
        .first()
    )

    if vc:
        # Update fields only if we have better data
        if full_name and not vc.full_name:
            vc.full_name = full_name
        if first and not vc.first_name:
            vc.first_name = first
        if last and not vc.last_name:
            vc.last_name = last
        if title and not vc.title:
            vc.title = title
        if phone and not vc.phone:
            vc.phone = phone
        if phone_mobile and not vc.phone_mobile:
            vc.phone_mobile = phone_mobile
        if linkedin and not vc.linkedin_url:
            vc.linkedin_url = linkedin
        vc.interaction_count = (vc.interaction_count or 0) + 1
        vc.last_interaction_at = received_at or datetime.now(timezone.utc)
        vc.last_seen_at = datetime.now(timezone.utc)
    else:
        if not full_name:
            # Can't create contact without at least a name
            return None
        vc = VendorContact(
            vendor_card_id=card.id,
            full_name=full_name,
            first_name=first,
            last_name=last,
            title=title,
            email=email_lower,
            phone=phone,
            phone_mobile=phone_mobile,
            linkedin_url=linkedin,
            source="email_signature",
            confidence=int((sig_data.get("confidence") or 0.5) * 100),
            interaction_count=1,
            last_interaction_at=received_at or datetime.now(timezone.utc),
        )
        db.add(vc)
        try:
            db.flush()
        except Exception as e:
            logger.debug("VendorContact flush conflict for %s: %s", email_lower, e)
            db.rollback()
            return None

    # 4. Create ActivityLog
    now = datetime.now(timezone.utc)
    activity = ActivityLog(
        user_id=user_id,
        activity_type="email_received",
        channel="outlook",
        vendor_card_id=card.id,
        vendor_contact_id=vc.id,
        requisition_id=requisition_id,
        contact_email=email_lower,
        contact_name=full_name,
        subject=subject,
        auto_logged=True,
        occurred_at=received_at or now,
        created_at=now,
    )
    db.add(activity)

    try:
        db.flush()
    except Exception as e:
        logger.debug("ActivityLog flush error: %s", e)
        db.rollback()

    return vc


def _run_sync_or_return_empty(async_fn, *args):
    """Run an async function synchronously, or return empty dict on failure."""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Already in an async context — can't use run()
        # Return empty; caller should handle async path separately
        return {}

    try:
        return asyncio.run(async_fn(*args))
    except Exception:
        return {}


# ── Pipeline event logging ──────────────────────────────────────────


def log_pipeline_event(
    db: Session,
    user_id: int,
    event_type: str,
    vendor_card_id: int | None = None,
    requisition_id: int | None = None,
    quote_id: int | None = None,
    contact_email: str | None = None,
    notes: str | None = None,
) -> "ActivityLog | None":
    """Log a pipeline event (rfq_sent, quote_received, po_issued, etc.).

    Resolves contact_email → vendor_contact_id if possible.
    """
    from ..models import ActivityLog, VendorContact

    vendor_contact_id = None
    contact_name = None
    if contact_email and vendor_card_id:
        vc = (
            db.query(VendorContact)
            .filter(
                VendorContact.vendor_card_id == vendor_card_id,
                VendorContact.email == contact_email.lower(),
            )
            .first()
        )
        if vc:
            vendor_contact_id = vc.id
            contact_name = vc.full_name
            vc.interaction_count = (vc.interaction_count or 0) + 1
            vc.last_interaction_at = datetime.now(timezone.utc)

    now = datetime.now(timezone.utc)
    activity = ActivityLog(
        user_id=user_id,
        activity_type=event_type,
        channel="avail_system",
        vendor_card_id=vendor_card_id,
        vendor_contact_id=vendor_contact_id,
        requisition_id=requisition_id,
        quote_id=quote_id,
        contact_email=contact_email,
        contact_name=contact_name,
        notes=notes,
        auto_logged=True,
        occurred_at=now,
        created_at=now,
    )
    db.add(activity)

    try:
        db.flush()
    except Exception as e:
        logger.debug("Pipeline event flush error: %s", e)
        db.rollback()
        return None

    return activity


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
    now = now or datetime.now(timezone.utc)

    # Recency: 0-7d = 100, decays linearly to 0 at 365d
    if last_interaction_at:
        lia = (
            last_interaction_at.replace(tzinfo=last_interaction_at.tzinfo or timezone.utc)
            if last_interaction_at
            else last_interaction_at
        )
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

    now = datetime.now(timezone.utc)
    cutoff_30d = now - timedelta(days=30)
    cutoff_60d = now - timedelta(days=60)
    cutoff_90d = now - timedelta(days=90)

    contacts = db.query(VendorContact).all()
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
            except Exception as e:
                logger.warning("Batch score flush error: %s", e)
                db.rollback()
                skipped += len(batch)
                batch = []

    # Final flush
    if batch:
        try:
            db.flush()
        except Exception as e:
            logger.warning("Final score flush error: %s", e)
            db.rollback()
            skipped += len(batch)

    try:
        db.commit()
    except Exception as e:
        logger.error("Score commit error: %s", e)
        db.rollback()

    return {"updated": updated, "skipped": skipped}


# ── AI Nudges (Gradient-powered) ────────────────────────────────────


def generate_contact_nudges(db: Session, vendor_card_id: int) -> list[dict]:
    """Generate nudge suggestions for dormant/cooling contacts.

    Rule-based triage first, then Gradient AI enrichment for actionable messages.
    """
    from ..config import settings
    from ..models import VendorContact

    contacts = db.query(VendorContact).filter(VendorContact.vendor_card_id == vendor_card_id).all()
    if not contacts:
        return []

    now = datetime.now(timezone.utc)
    nudges = []

    for c in contacts:
        if not c.activity_trend or c.activity_trend == "stable":
            if c.relationship_score and c.relationship_score >= 40:
                continue  # Stable and healthy — no nudge needed

        days_since = None
        if c.last_interaction_at:
            ts = (
                c.last_interaction_at
                if c.last_interaction_at.tzinfo
                else c.last_interaction_at.replace(tzinfo=timezone.utc)
            )
            days_since = (now - ts).days
        elif c.last_seen_at:
            ts = c.last_seen_at if c.last_seen_at.tzinfo else c.last_seen_at.replace(tzinfo=timezone.utc)
            days_since = (now - ts).days

        if days_since is None:
            continue

        nudge_type = None
        if c.activity_trend == "dormant" and days_since >= settings.contact_nudge_dormant_days:
            nudge_type = "dormant"
        elif c.activity_trend == "cooling" and days_since >= settings.contact_nudge_cooling_days:
            nudge_type = "cooling"
        elif not c.activity_trend and days_since >= settings.contact_nudge_dormant_days:
            nudge_type = "dormant"

        if not nudge_type:
            continue

        # Template-based message (fast, no AI needed)
        if nudge_type == "dormant":
            message = f"No contact with {c.full_name or c.email} in {days_since} days. Consider reaching out to maintain the relationship."
        else:
            message = f"Activity with {c.full_name or c.email} is declining ({days_since}d since last contact). A quick check-in could prevent losing touch."

        nudges.append(
            {
                "contact_id": c.id,
                "contact_name": c.full_name or c.email,
                "nudge_type": nudge_type,
                "message": message,
                "days_since_contact": days_since,
                "relationship_score": c.relationship_score,
                "activity_trend": c.activity_trend or "unknown",
            }
        )

    # Try Gradient AI enrichment for personalized nudge messages
    if nudges:
        try:
            nudges = _enrich_nudges_with_ai(db, nudges, vendor_card_id)
        except Exception as e:
            logger.debug("Gradient nudge enrichment skipped: %s", e)

    return nudges


def _enrich_nudges_with_ai(db: Session, nudges: list[dict], vendor_card_id: int) -> list[dict]:
    """Enrich nudge messages with AI suggestions via Gradient."""
    from ..config import settings

    if not settings.do_gradient_api_key:
        return nudges

    import asyncio

    from .gradient_service import gradient_json

    for nudge in nudges[:5]:  # Limit to 5 to avoid excessive API calls
        prompt = (
            f"Generate a concise, actionable outreach suggestion for this vendor contact relationship:\n\n"
            f"Contact: {nudge['contact_name']}\n"
            f"Status: {nudge['nudge_type']} ({nudge['days_since_contact']} days since last contact)\n"
            f"Trend: {nudge['activity_trend']}\n"
            f"Score: {nudge.get('relationship_score', 'N/A')}/100\n\n"
            f'Return JSON: {{"message": "<1-2 sentence suggestion>"}}'
        )
        try:
            result = asyncio.get_event_loop().run_until_complete(
                gradient_json(prompt, system="You are a B2B relationship advisor. Return ONLY valid JSON.", timeout=10)
            )
            if result and isinstance(result, dict) and result.get("message"):
                nudge["message"] = result["message"]
        except Exception as e:
            logger.debug("AI nudge generation failed, using template: %s", e)

    return nudges


def generate_contact_summary(db: Session, vendor_card_id: int, contact_id: int) -> str:
    """Generate a Gradient-powered summary of a contact's relationship."""
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

    # Try Gradient AI
    try:
        from ..config import settings

        if settings.do_gradient_api_key:
            import asyncio

            from .gradient_service import gradient_text

            prompt = (
                f"Write a 2-3 sentence relationship summary for this vendor contact:\n\n{context}\n\n"
                f"Focus on the health of the relationship and any recommended actions."
            )
            result = asyncio.get_event_loop().run_until_complete(
                gradient_text(prompt, system="You are a B2B relationship analyst. Be concise.", timeout=15)
            )
            if result:
                return result
    except Exception as e:
        logger.debug("Gradient summary failed: %s", e)

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
