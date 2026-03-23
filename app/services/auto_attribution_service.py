"""Auto-attribution service — background matching of unmatched activities.

Runs every 2 hours via scheduler. Two-pass approach:
  1. Fast path (no AI cost): re-run rule-based email/phone matching — new vendor/customer
     data may have been added since the activity was first logged.
  2. AI path: for still-unmatched items, call Claude (fast tier) with activity contact info
     and known entity names/domains. Only attributes if confidence >= 0.8.
  3. Activities unmatched for >30 days get auto-dismissed.

Called by: scheduler.py (job_auto_attribute_activities)
Depends on: activity_service, claude_client, models
"""

import json
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy.orm import Session

from ..models import ActivityLog


def run_auto_attribution(db: Session) -> dict:
    """Process unmatched activities with rule-based and AI matching.

    Returns summary dict with counts of actions taken.
    """
    from .activity_service import (
        attribute_activity,
        dismiss_activity,
        match_email_to_entity,
        match_phone_to_entity,
    )

    stats = {"rule_matched": 0, "ai_matched": 0, "auto_dismissed": 0, "skipped": 0}

    # Fetch unmatched, non-dismissed activities (batch of 200)
    activities = (
        db.query(ActivityLog)
        .filter(
            ActivityLog.company_id.is_(None),
            ActivityLog.vendor_card_id.is_(None),
            ActivityLog.dismissed_at.is_(None),
        )
        .order_by(ActivityLog.created_at.asc())
        .limit(200)
        .all()
    )

    if not activities:
        logger.debug("No unmatched activities to process")
        return stats

    logger.info("Processing %d unmatched activities", len(activities))
    now = datetime.now(timezone.utc)
    cutoff_30d = now - timedelta(days=30)
    still_unmatched = []

    # Pass 1: Rule-based matching (free, fast)
    for act in activities:
        match = None
        if act.contact_email:
            match = match_email_to_entity(act.contact_email, db)
        if not match and act.contact_phone:
            match = match_phone_to_entity(act.contact_phone, db)

        if match:
            attribute_activity(act.id, match["type"], match["id"], db, user_id=act.user_id)
            stats["rule_matched"] += 1
        elif act.created_at and act.created_at.replace(tzinfo=act.created_at.tzinfo or timezone.utc) < cutoff_30d:
            # Auto-dismiss activities older than 30 days
            dismiss_activity(act.id, db)
            stats["auto_dismissed"] += 1
        else:
            still_unmatched.append(act)

    db.commit()

    # Pass 2: AI matching for remaining items (batched)
    if still_unmatched:
        ai_results = _ai_match_batch(still_unmatched, db)
        for act_id, result in ai_results.items():
            if result and result.get("confidence", 0) >= 0.8:
                attribute_activity(
                    act_id,
                    result["entity_type"],
                    result["entity_id"],
                    db,
                    user_id=None,
                )
                stats["ai_matched"] += 1
            else:
                stats["skipped"] += 1
        db.commit()

    logger.info(
        "Auto-attribution complete: %d rule-matched, %d AI-matched, %d dismissed, %d skipped",
        stats["rule_matched"],
        stats["ai_matched"],
        stats["auto_dismissed"],
        stats["skipped"],
    )
    return stats


def _ai_match_batch(activities: list[ActivityLog], db: Session) -> dict:
    """Use Claude (fast tier) to match activities to known entities.

    Returns {activity_id: {"entity_type": str, "entity_id": int, "confidence": float} |
    None}
    """
    import asyncio

    from ..models import Company, VendorCard

    # Build entity reference lists
    companies = db.query(Company.id, Company.name, Company.domain).filter(Company.is_active.is_(True)).limit(500).all()
    vendors = (
        db.query(VendorCard.id, VendorCard.display_name, VendorCard.domain)
        .filter(VendorCard.is_blacklisted.is_(False))
        .limit(500)
        .all()
    )

    company_list = [{"id": c.id, "name": c.name, "domain": c.domain or ""} for c in companies]
    vendor_list = [{"id": v.id, "name": v.display_name, "domain": v.domain or ""} for v in vendors]

    # Build activity descriptions for Claude
    activity_items = []
    for act in activities[:20]:  # Cap at 20 per batch to limit cost
        activity_items.append(
            {
                "id": act.id,
                "email": act.contact_email or "",
                "phone": act.contact_phone or "",
                "name": act.contact_name or "",
                "subject": (act.subject or "")[:100],
            }
        )

    if not activity_items:
        return {}

    try:
        result = asyncio.get_event_loop().run_until_complete(
            _call_claude_for_matching(activity_items, company_list, vendor_list)
        )
        return result or {}
    except RuntimeError:
        # If there's already a running event loop (likely), use it
        try:
            loop = asyncio.get_running_loop()
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as pool:
                result = loop.run_in_executor(
                    pool,
                    lambda: asyncio.run(_call_claude_for_matching(activity_items, company_list, vendor_list)),
                )
                # Can't await in sync context; skip AI matching this round
                logger.debug("AI matching deferred — event loop conflict")
                return {}
        except Exception:
            return {}
    except Exception:
        logger.exception("AI matching failed")
        return {}


async def _call_claude_for_matching(
    activities: list[dict],
    companies: list[dict],
    vendors: list[dict],
) -> dict:
    """Call Claude to match activities to entities."""
    from ..utils.claude_client import claude_structured
    from ..utils.claude_errors import ClaudeError, ClaudeUnavailableError

    prompt = "Match these unmatched activities to the correct company or vendor.\n\nACTIVITIES:\n"
    for a in activities:
        prompt += f"- ID {a['id']}: email={a['email']}, phone={a['phone']}, name={a['name']}, subject={a['subject']}\n"

    prompt += "\nKNOWN COMPANIES:\n"
    for c in companies[:100]:  # Limit to keep prompt manageable
        prompt += f"- ID {c['id']}: {c['name']} (domain: {c['domain']})\n"

    prompt += "\nKNOWN VENDORS:\n"
    for v in vendors[:100]:
        prompt += f"- ID {v['id']}: {v['name']} (domain: {v['domain']})\n"

    prompt += (
        "\nFor each activity, determine if it matches a company or vendor. "
        "Return matches with confidence 0.0-1.0. Only match if confident."
    )

    schema = {
        "type": "object",
        "properties": {
            "matches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "activity_id": {"type": "integer"},
                        "entity_type": {"type": "string", "enum": ["company", "vendor"]},
                        "entity_id": {"type": "integer"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": ["activity_id", "entity_type", "entity_id", "confidence"],
                },
            }
        },
        "required": ["matches"],
    }

    try:
        result = await claude_structured(
            prompt=prompt,
            schema=schema,
            system="You match email/phone activity records to known companies and vendors in a CRM system. Be conservative — only match when confident.",
            model_tier="fast",
            max_tokens=2048,
        )
    except ClaudeUnavailableError:
        logger.info("Claude not configured — skipping auto attribution")
        return {}
    except ClaudeError as e:
        logger.warning("Claude AI failed for auto attribution: %s", e)
        return {}

    if not result:
        return {}

    # API occasionally returns tool input as JSON string instead of dict
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return {}

    if not isinstance(result, dict) or "matches" not in result:
        return {}

    return {
        m["activity_id"]: {
            "entity_type": m["entity_type"],
            "entity_id": m["entity_id"],
            "confidence": m["confidence"],
        }
        for m in result["matches"]
    }
