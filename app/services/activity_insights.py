"""Activity insight service — detects patterns and generates AI-powered insights.

Analyzes ActivityLog, Requisition, and Quote data to detect:
  - Gone quiet: no outbound activity to company in N days
  - Engagement acceleration: sudden increase in inbound activity
  - Deal stalling: active requisition with no new quotes in 7+ days
  - Response time degradation: user's avg response time trending up

Called by: app/jobs/teams_alert_jobs.py (morning briefing insights block)
Depends on: app/models/intelligence.py, app/services/activity_service.py, app/utils/claude_client.py
"""

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session


def get_user_insights(user_id: int, db: Session, max_insights: int = 3) -> list[dict]:
    """Get top insights for a user, ranked by actionability.

    Returns list of {"type", "title", "detail", "action", "priority"} dicts.
    """
    insights = []

    try:
        insights.extend(_detect_gone_quiet(user_id, db))
    except Exception:
        logger.debug("Gone quiet detection failed", exc_info=True)

    try:
        insights.extend(_detect_stalling_deals(user_id, db))
    except Exception:
        logger.debug("Stalling deals detection failed", exc_info=True)

    try:
        insights.extend(_detect_response_time_trend(user_id, db))
    except Exception:
        logger.debug("Response time trend detection failed", exc_info=True)

    try:
        insights.extend(_detect_engagement_acceleration(user_id, db))
    except Exception:
        logger.debug("Engagement acceleration detection failed", exc_info=True)

    # Sort by priority (lower = more urgent) and return top N
    priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    insights.sort(key=lambda x: priority_order.get(x.get("priority", "low"), 3))
    return insights[:max_insights]


async def get_user_insights_with_ai(user_id: int, db: Session, max_insights: int = 3) -> list[dict]:
    """Get insights with AI-generated natural language summaries."""
    insights = get_user_insights(user_id, db, max_insights)
    if not insights:
        return []

    # Use AI to generate actionable summaries
    try:
        from app.utils.claude_client import claude_structured

        raw_text = "\n".join(
            f"- {i['type']}: {i['title']} — {i['detail']}" for i in insights
        )

        schema = {
            "type": "object",
            "properties": {
                "insights": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "summary": {"type": "string"},
                            "action": {"type": "string"},
                        },
                        "required": ["summary", "action"],
                    },
                }
            },
            "required": ["insights"],
        }

        result = await claude_structured(
            prompt=(
                f"Rewrite these business insights as short, actionable one-liners for a sales rep "
                f"at an electronic component distributor. Each should be max 100 chars and include "
                f"a specific action they can take right now:\n\n{raw_text}"
            ),
            schema=schema,
            system="You write concise, actionable business insights for sales reps. Plain language, no jargon.",
            model_tier="fast",
            max_tokens=300,
        )

        if result and result.get("insights"):
            ai_insights = result["insights"]
            for i, insight in enumerate(insights):
                if i < len(ai_insights):
                    insight["ai_summary"] = ai_insights[i].get("summary", "")
                    insight["ai_action"] = ai_insights[i].get("action", "")

    except Exception:
        logger.debug("AI insight generation failed", exc_info=True)

    return insights


def _detect_gone_quiet(user_id: int, db: Session, threshold_days: int = 10) -> list[dict]:
    """Detect companies the user hasn't contacted in threshold_days+."""
    from app.models.crm import Company
    from app.models.intelligence import ActivityLog

    insights = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=threshold_days)

    # Find companies owned by user with recent requisitions but no recent activity
    owned = (
        db.query(Company)
        .filter(Company.account_owner_id == user_id, Company.is_active.is_(True))
        .all()
    )

    for company in owned[:20]:  # Cap to avoid slow queries
        latest = (
            db.query(func.max(ActivityLog.created_at))
            .filter(
                ActivityLog.company_id == company.id,
                ActivityLog.direction == "outbound",
            )
            .scalar()
        )

        if latest:
            latest_aware = latest.replace(tzinfo=timezone.utc) if latest.tzinfo is None else latest
            days_ago = (datetime.now(timezone.utc) - latest_aware).days
            if days_ago >= threshold_days:
                insights.append({
                    "type": "gone_quiet",
                    "title": f"{company.name} went quiet",
                    "detail": f"No outbound contact in {days_ago} days",
                    "action": f"Call {company.name}",
                    "priority": "high" if days_ago >= 14 else "medium",
                    "entity_type": "company",
                    "entity_id": company.id,
                })
        elif company.created_at:
            created = company.created_at.replace(tzinfo=timezone.utc) if company.created_at.tzinfo is None else company.created_at
            days_since_created = (datetime.now(timezone.utc) - created).days
            if days_since_created >= threshold_days:
                insights.append({
                    "type": "gone_quiet",
                    "title": f"{company.name} — never contacted",
                    "detail": f"Company added {days_since_created} days ago, no outbound activity",
                    "action": f"Reach out to {company.name}",
                    "priority": "medium",
                    "entity_type": "company",
                    "entity_id": company.id,
                })

    return insights[:5]


def _detect_stalling_deals(user_id: int, db: Session, threshold_days: int = 7) -> list[dict]:
    """Detect active requisitions with no new quotes in threshold_days+."""
    from app.models.offers import Offer
    from app.models.sourcing import Requisition

    insights = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=threshold_days)

    active_reqs = (
        db.query(Requisition)
        .filter(
            Requisition.created_by == user_id,
            Requisition.status.in_(["active", "sourcing", "offers"]),
        )
        .all()
    )

    for req in active_reqs:
        latest_offer = (
            db.query(func.max(Offer.created_at))
            .filter(Offer.requisition_id == req.id)
            .scalar()
        )

        if latest_offer:
            latest_aware = latest_offer.replace(tzinfo=timezone.utc) if latest_offer.tzinfo is None else latest_offer
            days_stale = (datetime.now(timezone.utc) - latest_aware).days
        else:
            req_created = req.created_at.replace(tzinfo=timezone.utc) if req.created_at and req.created_at.tzinfo is None else req.created_at
            days_stale = (datetime.now(timezone.utc) - req_created).days if req_created else 0

        if days_stale >= threshold_days:
            insights.append({
                "type": "deal_stalling",
                "title": f"Req #{req.id} stalling — {req.customer_name or 'N/A'}",
                "detail": f"No new offers in {days_stale} days ({req.name})",
                "action": f"Follow up on {req.name}",
                "priority": "high" if days_stale >= 14 else "medium",
                "entity_type": "requisition",
                "entity_id": req.id,
            })

    return insights[:5]


def _detect_response_time_trend(user_id: int, db: Session) -> list[dict]:
    """Detect if user's response time is trending up over 14 days."""
    from app.models.offers import Contact
    from app.models.sourcing import Requisition

    insights = []
    now = datetime.now(timezone.utc)

    # Compare last 7 days vs previous 7 days
    week_ago = now - timedelta(days=7)
    two_weeks_ago = now - timedelta(days=14)

    def _avg_response_hours(start, end):
        result = (
            db.query(
                func.avg(
                    func.extract("epoch", Contact.created_at) - func.extract("epoch", Requisition.created_at)
                ) / 3600
            )
            .join(Requisition, Contact.requisition_id == Requisition.id)
            .filter(
                Contact.user_id == user_id,
                Contact.created_at.between(start, end),
            )
            .scalar()
        )
        return float(result) if result else None

    recent_avg = _avg_response_hours(week_ago, now)
    prev_avg = _avg_response_hours(two_weeks_ago, week_ago)

    if recent_avg and prev_avg and prev_avg > 0:
        increase_pct = ((recent_avg - prev_avg) / prev_avg) * 100
        if increase_pct > 30 and recent_avg > 4:  # 30%+ increase and >4h avg
            insights.append({
                "type": "response_time_degradation",
                "title": "Response time trending up",
                "detail": f"Avg response: {recent_avg:.1f}h (was {prev_avg:.1f}h) — {increase_pct:.0f}% increase",
                "action": "Review open contacts and prioritize follow-ups",
                "priority": "medium",
                "entity_type": "user",
                "entity_id": user_id,
            })

    return insights


def _detect_engagement_acceleration(user_id: int, db: Session) -> list[dict]:
    """Detect companies with sudden increase in inbound activity."""
    from app.models.intelligence import ActivityLog

    insights = []
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    two_weeks_ago = now - timedelta(days=14)

    # Get companies with inbound activity this week
    recent_activity = (
        db.query(ActivityLog.company_id, func.count(ActivityLog.id).label("cnt"))
        .filter(
            ActivityLog.user_id == user_id,
            ActivityLog.direction == "inbound",
            ActivityLog.created_at > week_ago,
        )
        .group_by(ActivityLog.company_id)
        .all()
    )

    for company_id, recent_count in recent_activity:
        if recent_count < 3:
            continue

        prev_count = (
            db.query(func.count(ActivityLog.id))
            .filter(
                ActivityLog.company_id == company_id,
                ActivityLog.direction == "inbound",
                ActivityLog.created_at.between(two_weeks_ago, week_ago),
            )
            .scalar()
        ) or 0

        if prev_count == 0 or (recent_count / max(prev_count, 1)) >= 2:
            from app.models.crm import Company
            company = db.get(Company, company_id)
            if company:
                insights.append({
                    "type": "engagement_acceleration",
                    "title": f"{company.name} — engagement spike",
                    "detail": f"{recent_count} inbound activities this week (was {prev_count} prior week)",
                    "action": f"Capitalize on {company.name}'s interest",
                    "priority": "high",
                    "entity_type": "company",
                    "entity_id": company_id,
                })

    return insights[:3]
