"""AI activity-timeline digest service.

Builds and caches one structured ActivityDigest per (entity_type, entity_id),
regenerated lazily on view when the timeline basis changes. Guarded by a Redis
nx-lock (anti-stampede) and a short cooldown (anti-burst).

Called by: digest HTMX endpoints in app/routers/htmx_views.py
Depends on: app/utils/claude_client.py (claude_structured), app/services/activity_service.py,
            app/cache/intel_cache.py (_get_redis), app/models/intelligence.py
"""

from enum import StrEnum

from ..constants import DigestEntityType

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
    return _REQ_SYSTEM if entity_type == DigestEntityType.REQUISITION else _ACCOUNT_SYSTEM


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
