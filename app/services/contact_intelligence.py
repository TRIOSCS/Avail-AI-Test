"""Contact Intelligence Service — contact relationship summary (residual).

The contact-intelligence layer (auto-discovery writers, relationship scoring,
nudges) was deleted in the Wave 2 simplification sweep (spec §5.4 — computed,
displayed nowhere; its compute jobs were already removed in Wave 1). Only
``generate_contact_summary`` remains: it is still imported by the vendor-contact
summary API route, which is slated for deletion in the W2 orphan-batch sweep —
delete this module with it.

Called by: app.routers.vendor_contacts (contact summary route)
Depends on: app.models (ActivityLog, VendorContact), app.utils.claude_client
"""

from __future__ import annotations

from collections.abc import Coroutine
from typing import Any

from loguru import logger
from sqlalchemy.orm import Session


def _run_coro_blocking[T](coro: Coroutine[Any, Any, T], *, timeout: float) -> T:
    """Run an async coroutine to completion from sync code, blocking for the result.

    When a loop is already running (scheduler/async context), `asyncio.run` would
    raise, so the coroutine is executed in a worker thread instead.
    """
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(asyncio.run, coro)
        return future.result(timeout=timeout)


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
        result = _run_coro_blocking(
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
