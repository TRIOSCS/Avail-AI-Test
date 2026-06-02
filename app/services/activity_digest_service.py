"""AI activity-timeline digest service.

Builds and caches one structured ActivityDigest per (entity_type, entity_id),
regenerated lazily on view when the timeline basis changes. Guarded by a Redis
nx-lock (anti-stampede) and a short cooldown (anti-burst).

Called by: digest HTMX endpoints in app/routers/htmx_views.py
Depends on: app/utils/claude_client.py (claude_structured), app/services/activity_service.py,
            app/cache/intel_cache.py (_get_redis), app/models/intelligence.py
"""

from datetime import datetime, timedelta, timezone
from enum import StrEnum

from loguru import logger
from sqlalchemy.orm import Session

from ..config import settings
from ..constants import DigestEntityType
from ..models.intelligence import ActivityDigest

ACTIVITY_CAP = 30


class DigestState(StrEnum):
    READY = "ready"
    INSUFFICIENT = "insufficient"
    GENERATING = "generating"
    ERROR = "error"


DIGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "One-line summary, <= 200 chars."},
        "narrative": {"type": "string", "description": "2-4 sentence plain-language summary."},
        "highlights": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["label", "value"],
            },
        },
        "next_step": {"type": "string", "description": "Suggested next action, or empty."},
        "status_signal": {
            "type": "string",
            "enum": ["on_track", "stalled", "needs_attention"],
        },
    },
    "required": ["headline", "narrative", "highlights", "status_signal"],
}

_REQ_SYSTEM = """You summarize the sourcing progress of an electronic-component RFQ for a buyer.
Given a requisition's recent activity timeline, produce a tight digest: which vendors were
contacted, who replied, the best offer seen, what is blocked or outstanding, and the single
most useful next action. status_signal: 'on_track' when progressing, 'stalled' when no recent
inbound movement, 'needs_attention' when replies/offers await a decision."""

_ACCOUNT_SYSTEM = """You summarize the relationship with a customer account for a salesperson.
Given the account's recent activity timeline, produce a tight digest: recent engagement,
responsiveness, sentiment trend, open RFQs, and the single most useful follow-up. status_signal:
'on_track' for healthy engagement, 'stalled' when contact has gone quiet, 'needs_attention'
when something awaits a reply."""


def _system_prompt(entity_type: DigestEntityType) -> str:
    if entity_type == DigestEntityType.REQUISITION:
        return _REQ_SYSTEM
    if entity_type == DigestEntityType.COMPANY:
        return _ACCOUNT_SYSTEM
    raise ValueError(entity_type)


def _build_activity_lines(activities) -> str:
    """One line per activity, newest-first, reusing the AI-cleaned summary."""
    lines = []
    for a in activities:
        when = a.occurred_at or a.created_at
        when_s = when.strftime("%Y-%m-%d") if when else "?"
        text = a.summary or (a.notes[:200] if a.notes else "")
        parts = [when_s, a.activity_type]
        if a.direction:
            parts.append(a.direction)
        if a.contact_name:
            parts.append(a.contact_name)
        if a.subject:
            parts.append(a.subject)
        if text:
            parts.append(f"— {text}")
        lines.append(" | ".join(str(p) for p in parts))
    return "\n".join(lines)


def _get_redis():
    from ..cache.intel_cache import _get_redis as _r

    return _r()


def _load_activities(entity_type: DigestEntityType, entity_id: int, db: Session):
    from .activity_service import get_company_activities, get_requisition_activities

    if entity_type == DigestEntityType.REQUISITION:
        acts = get_requisition_activities(entity_id, db, limit=ACTIVITY_CAP, meaningful_only=True)
    else:
        acts = get_company_activities(entity_id, db, limit=ACTIVITY_CAP)
        acts = [a for a in acts if a.is_meaningful in (True, None)]
    return acts


def _digest_to_dict(row: ActivityDigest) -> dict:
    return {
        "state": DigestState.READY,
        "headline": row.headline,
        "narrative": row.narrative,
        "highlights": row.highlights or [],
        "next_step": row.next_step,
        "status_signal": row.status_signal,
        "generated_at": row.generated_at,
    }


async def get_or_build_digest(entity_type: DigestEntityType, entity_id: int, db: Session, force: bool = False) -> dict:
    """Return a cached or freshly-built digest dict.

    See module docstring for the algorithm.
    """
    now = datetime.now(timezone.utc)
    existing = (
        db.query(ActivityDigest)
        .filter(ActivityDigest.entity_type == entity_type, ActivityDigest.entity_id == entity_id)
        .first()
    )

    if existing and not force and existing.cooldown_until and existing.cooldown_until > now:
        return _digest_to_dict(existing)

    activities = _load_activities(entity_type, entity_id, db)
    if len(activities) < 2:
        return {"state": DigestState.INSUFFICIENT}

    basis_last = max((a.created_at for a in activities if a.created_at), default=None)
    basis_count = len(activities)

    if (
        existing
        and not force
        and existing.basis_last_activity_at == basis_last
        and existing.basis_activity_count == basis_count
    ):
        return _digest_to_dict(existing)

    r = _get_redis()
    lock_key = f"lock:digest:{entity_type}:{entity_id}"
    acquired = False
    if r is not None:
        try:
            acquired = bool(r.set(lock_key, "1", nx=True, ex=30))
        except Exception as e:
            logger.warning("Digest lock acquire failed ({}): {}", lock_key, e)
            acquired = True
    else:
        acquired = True

    if not acquired:
        if existing:
            return _digest_to_dict(existing)
        return {"state": DigestState.GENERATING}

    try:
        from ..utils.claude_client import claude_structured

        prompt = "Recent activity (newest first):\n" + _build_activity_lines(activities)
        result = await claude_structured(
            prompt=prompt,
            schema=DIGEST_SCHEMA,
            system=_system_prompt(entity_type),
            model_tier="smart",
            max_tokens=700,
            cache_system=True,
        )
        if not result:
            logger.error("Digest AI returned no result for {} {}", entity_type, entity_id)
            return {"state": DigestState.ERROR}

        cooldown = now + timedelta(seconds=settings.digest_cooldown_seconds)
        row = existing or ActivityDigest(entity_type=entity_type, entity_id=entity_id)
        row.headline = (result.get("headline") or "")[:300] or None
        row.narrative = result.get("narrative") or None
        row.highlights = result.get("highlights") or []
        row.next_step = (result.get("next_step") or "")[:500] or None
        row.status_signal = result.get("status_signal") or None
        row.generated_at = now
        row.basis_last_activity_at = basis_last
        row.basis_activity_count = basis_count
        row.cooldown_until = cooldown
        row.model = "smart"
        if existing is None:
            db.add(row)
        db.commit()
        return _digest_to_dict(row)
    finally:
        if r is not None and acquired:
            try:
                r.delete(lock_key)
            except Exception as e:
                logger.warning("Digest lock release failed ({}): {}", lock_key, e)
