"""AI interaction quality scoring service.

Scores eligible ActivityLog entries (see _AI_SCORED_TYPES) using Claude Haiku
for quality classification, sentiment, and clean summary generation.

Called by: app/jobs/quality_jobs.py (batch scorer)
Depends on: app/utils/claude_client.py (claude_structured), app/models/intelligence.py
"""

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy.orm import Session

from ..constants import ActivityType
from ..models.intelligence import ActivityLog

# Activity types eligible for the AI quality pass. Explicit allow-list:
# only these types are selected by score_unscored_activities().
_AI_SCORED_TYPES = (ActivityType.SIGHTING_ADDED, ActivityType.EMAIL_RECEIVED)

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
                "sighting_batch",
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

SIGHTING_SYSTEM_PROMPT = """You are a sourcing-activity quality analyst for an electronic component sourcing company.
A "sighting batch" is a group of vendor sightings (stock/price quotes) discovered by an automated
search run and recorded on a requisition's activity timeline.

Judge whether the batch is meaningful enough to surface on the requisition timeline.
A batch is "meaningful" when it represents real sourcing progress — multiple sightings found
across credible supplier sources. It is NOT meaningful when it is empty or trivially small noise.

Always use the classification "sighting_batch" for these events.

Score quality 0-100:
- 0-20: Noise (no sightings, or a single low-signal hit)
- 21-40: Minimal (a couple of sightings from one source)
- 41-60: Routine (a handful of sightings across sources)
- 61-80: Substantive (a solid batch across multiple credible sources)
- 81-100: High-value (a large, broad batch indicating strong sourcing coverage)
"""


def _mark_no_data(log: ActivityLog, db: Session) -> None:
    """Stamp an activity as assessed-with-no-data so it is never retried."""
    log.quality_score = 0.0
    log.quality_classification = "no_data"
    log.is_meaningful = False
    log.summary = "No interaction details available"
    log.quality_assessed_at = datetime.now(timezone.utc)
    db.flush()


async def score_activity(activity_id: int, db: Session) -> None:
    """Score a single ActivityLog entry with AI quality classification."""
    log = db.get(ActivityLog, activity_id)
    if not log:
        logger.debug(f"Activity {activity_id} not found, skipping")
        return

    # Skip already-scored entries
    if log.quality_assessed_at is not None:
        return

    # Build the scoring prompt — branches by activity type. The QUALITY_SCHEMA
    # result shape is identical across branches; only the prompt text differs.
    system_prompt = QUALITY_SYSTEM_PROMPT
    if log.activity_type == ActivityType.SIGHTING_ADDED:
        details = log.details if isinstance(log.details, dict) else {}
        count = details.get("count")
        sources = details.get("sources") or []
        parts = []
        if count is not None:
            parts.append(f"Sightings in batch: {count}")
        if sources:
            parts.append(f"Sources: {', '.join(str(s) for s in sources)}")
        if log.notes:
            parts.append(f"Notes: {log.notes[:500]}")
        if not parts:
            # Nothing to analyze — mark as assessed to prevent infinite retry
            _mark_no_data(log, db)
            return
        prompt = (
            "A batch of vendor sightings was added from an automated search run "
            "for this requirement. Judge whether it is meaningful enough to surface "
            "on the requisition timeline:\n\n" + "\n".join(parts)
        )
        system_prompt = SIGHTING_SYSTEM_PROMPT
    else:
        # Email and any other activity type: build from interaction fields.
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
            # Nothing to analyze — mark as assessed to prevent infinite retry
            _mark_no_data(log, db)
            return

        prompt = "Classify this business interaction:\n\n" + "\n".join(parts)

    from ..utils.claude_client import claude_structured

    result = await claude_structured(
        prompt=prompt,
        schema=QUALITY_SCHEMA,
        system=system_prompt,
        model_tier="fast",
        max_tokens=512,
        cache_system=True,
    )

    if not result:
        # Mark as assessed to prevent infinite retry — AI returned no usable result
        log.quality_score = None
        log.quality_classification = "scoring_failed"
        log.is_meaningful = None
        log.summary = None
        log.quality_assessed_at = datetime.now(timezone.utc)
        db.flush()
        logger.error(f"AI scoring returned no result for activity {activity_id}, marked as assessed")
        return

    log.quality_score = float(result.get("quality_score", 0))
    log.quality_classification = result.get("classification") or None
    if log.quality_classification:
        log.quality_classification = log.quality_classification[:30]
    log.is_meaningful = result.get("is_meaningful", False)
    log.summary = (result.get("clean_summary") or "")[:500] or None
    log.quality_assessed_at = datetime.now(timezone.utc)
    db.flush()

    logger.debug(f"Scored activity {activity_id}: {log.quality_classification} ({log.quality_score})")


async def score_unscored_activities(db: Session, batch_size: int = 50) -> int:
    """Score all unscored AI-eligible ActivityLog entries (see _AI_SCORED_TYPES).

    Returns count scored. Aborts early on auth/config errors to avoid burning API calls
    on systemic failures.
    """
    unscored = (
        db.query(ActivityLog)
        .filter(
            ActivityLog.quality_assessed_at.is_(None),
            ActivityLog.activity_type.in_(_AI_SCORED_TYPES),
            ActivityLog.created_at >= datetime.now(timezone.utc) - timedelta(days=7),
        )
        .order_by(ActivityLog.created_at.desc())
        .limit(batch_size)
        .all()
    )

    if not unscored:
        return 0

    scored = 0
    errors = 0
    for log in unscored:
        try:
            await score_activity(log.id, db)
            scored += 1
        except Exception as e:
            err_name = type(e).__name__
            # Abort on auth/config errors — no point trying remaining activities
            if err_name in ("ClaudeAuthError", "ClaudeUnavailableError"):
                logger.error(f"Quality scoring aborted — configuration error: {e}")
                break
            # Abort on rate limit — stop sending more requests
            if err_name == "ClaudeRateLimitError":
                logger.warning("Rate limited during quality scoring, stopping batch early")
                break
            errors += 1
            logger.exception(f"Failed to score activity {log.id}")

    if scored:
        db.commit()

    if scored or errors:
        logger.info(f"Quality scorer: {scored} scored, {errors} errors out of {len(unscored)} unscored")

    return scored
