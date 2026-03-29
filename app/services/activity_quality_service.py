"""AI interaction quality scoring service.

Scores non-email ActivityLog entries using Claude Haiku for quality
classification, sentiment, and clean summary generation.

Called by: app/jobs/quality_jobs.py (batch scorer)
Depends on: app/utils/claude_client.py (claude_structured), app/models/intelligence.py
"""

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy.orm import Session

from ..models.intelligence import ActivityLog

QUALITY_SCHEMA = {
    "type": "object",
    "properties": {
        "is_meaningful": {
            "type": "boolean",
            "description": "True if this is a real business interaction, not noise (voicemail, auto-reply, OOO, bounce)",
        },
        "quality_score": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": "Interaction quality: 0=noise, 50=routine check-in, 100=deal-closing negotiation",
        },
        "classification": {
            "type": "string",
            "enum": [
                "conversation",
                "voicemail",
                "auto_reply",
                "ooo",
                "bounce",
                "follow_up",
                "quote",
                "negotiation",
            ],
        },
        "sentiment": {"type": "string", "enum": ["positive", "neutral", "negative"]},
        "clean_summary": {
            "type": "string",
            "description": "1-2 sentence summary of what happened, stripped of noise. Max 100 words.",
        },
    },
    "required": ["is_meaningful", "quality_score", "classification", "sentiment", "clean_summary"],
}

QUALITY_SYSTEM_PROMPT = """You are an interaction quality analyst for an electronic component sourcing company.
Analyze the following business interaction and classify it.

A "meaningful" interaction is one where real business communication occurred:
- Phone calls with actual conversation (not voicemail, not missed calls)
- Notes documenting real customer/vendor engagement
- Teams messages with substantive business content
- Meetings where business was discussed

NOT meaningful: voicemails, auto-replies, out-of-office, bounced messages, missed calls (<15 sec duration).

Score quality 0-100:
- 0-20: Noise (voicemail, auto-reply, OOO)
- 21-40: Minimal (left message, brief check-in)
- 41-60: Routine (standard follow-up, status update)
- 61-80: Substantive (pricing discussion, requirements review, negotiation)
- 81-100: High-value (deal closing, contract negotiation, strategic partnership discussion)
"""


async def score_activity(activity_id: int, db: Session) -> None:
    """Score a single ActivityLog entry with AI quality classification."""
    log = db.get(ActivityLog, activity_id)
    if not log:
        return

    # Skip already-scored entries
    if log.quality_assessed_at is not None:
        return

    # Build prompt from available fields
    parts = []
    if log.event_type:
        parts.append(f"Type: {log.event_type}")
    if log.channel:
        parts.append(f"Channel: {log.channel}")
    if log.direction:
        parts.append(f"Direction: {log.direction}")
    if log.subject:
        parts.append(f"Subject: {log.subject}")
    if log.notes:
        parts.append(f"Notes: {log.notes[:500]}")
    if log.duration_seconds is not None:
        parts.append(f"Duration: {log.duration_seconds} seconds")
    if log.contact_name:
        parts.append(f"Contact: {log.contact_name}")

    if not parts:
        # Nothing to analyze — mark as assessed with defaults
        log.quality_score = 0.0
        log.quality_classification = "auto_reply"
        log.is_meaningful = False
        log.summary = "No interaction details available"
        log.quality_assessed_at = datetime.now(timezone.utc)
        return

    prompt = "Classify this business interaction:\n\n" + "\n".join(parts)

    from ..utils.claude_client import claude_structured

    result = await claude_structured(
        prompt=prompt,
        schema=QUALITY_SCHEMA,
        system=QUALITY_SYSTEM_PROMPT,
        model_tier="fast",
        max_tokens=512,
        cache_system=True,
    )

    if not result:
        logger.warning(f"AI scoring failed for activity {activity_id}")
        return

    log.quality_score = float(result.get("quality_score", 0))
    log.quality_classification = result.get("classification", "")[:30]
    log.is_meaningful = result.get("is_meaningful", False)
    log.summary = (result.get("clean_summary") or "")[:500]
    log.quality_assessed_at = datetime.now(timezone.utc)
    db.flush()

    logger.debug(f"Scored activity {activity_id}: {log.quality_classification} ({log.quality_score})")


async def score_unscored_activities(db: Session, batch_size: int = 50) -> int:
    """Score all unscored non-email ActivityLog entries.

    Returns count scored.
    """
    unscored = (
        db.query(ActivityLog)
        .filter(
            ActivityLog.quality_assessed_at.is_(None),
            ActivityLog.event_type.notin_(["email"]),
            ActivityLog.created_at >= datetime.now(timezone.utc) - timedelta(days=7),
        )
        .order_by(ActivityLog.created_at.desc())
        .limit(batch_size)
        .all()
    )

    scored = 0
    for log in unscored:
        try:
            await score_activity(log.id, db)
            scored += 1
        except Exception:
            logger.exception(f"Failed to score activity {log.id}")

    if scored:
        db.commit()
        logger.info(f"Quality scorer: {scored}/{len(unscored)} activities scored")

    return scored
