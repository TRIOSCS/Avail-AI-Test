"""Teams DM alert scheduled jobs — morning briefing + director digest.

Called by: app/jobs/__init__.py via register_teams_alert_jobs()
Depends on: app.database, app.models, app.services.teams_alert_service
"""

from datetime import datetime, timedelta, timezone

from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from ..scheduler import _traced_job


def register_teams_alert_jobs(scheduler, settings):
    """Register morning briefing and director digest jobs."""
    scheduler.add_job(
        _job_am_morning_briefing,
        CronTrigger(hour=14, minute=0),  # 7:00 AM PT = 14:00 UTC
        id="am_morning_briefing",
        name="AM morning briefing",
    )
    scheduler.add_job(
        _job_director_daily_digest,
        CronTrigger(hour=14, minute=15),  # 7:15 AM PT
        id="director_daily_digest",
        name="Director daily digest",
    )


@_traced_job
async def _job_am_morning_briefing():
    """Build and send personalized morning briefing to each active user."""
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        from ..models.auth import User

        users = db.query(User).filter(User.is_active.is_(True), User.role.in_(["buyer", "sales", "trader"])).all()
        for user in users:
            try:
                await _send_user_briefing(user, db)
            except Exception:
                logger.debug("Briefing failed for user %d", user.id, exc_info=True)
    except Exception:
        logger.exception("AM morning briefing job failed")
    finally:
        db.close()


async def _send_user_briefing(user, db):
    """Compose and send one user's morning briefing."""
    from ..models.offers import Contact, Offer, VendorResponse
    from ..models.quotes import Quote
    from ..models.sourcing import Requisition
    from ..models.teams_alert_config import TeamsAlertConfig
    from ..services.teams_alert_service import send_alert

    # Check if user has alerts enabled
    config = db.query(TeamsAlertConfig).filter(TeamsAlertConfig.user_id == user.id).first()
    if config and not config.alerts_enabled:
        return

    now = datetime.now(timezone.utc)
    blocks = []

    # Block 1: Open RFQs with no offers yet
    open_reqs = (
        db.query(Requisition)
        .filter(
            Requisition.created_by == user.id,
            Requisition.status.in_(["active", "sourcing", "draft"]),
        )
        .all()
    )
    no_offer_reqs = []
    for req in open_reqs:
        offer_count = db.query(Offer).filter(Offer.requisition_id == req.id).count()
        if offer_count == 0:
            created = (
                req.created_at.replace(tzinfo=timezone.utc)
                if req.created_at and req.created_at.tzinfo is None
                else req.created_at
            )
            days_old = (now - created).days if created else 0
            no_offer_reqs.append(f"  {req.customer_name or 'N/A'} — {req.name} ({days_old}d)")

    if no_offer_reqs:
        blocks.append("OPEN RFQs (no offers)\n" + "\n".join(no_offer_reqs[:5]))
        if len(no_offer_reqs) > 5:
            blocks[-1] += f"\n  +{len(no_offer_reqs) - 5} more"

    # Block 2: Quotes needing follow-up (sent >48h ago, no activity since)
    cutoff_48h = now - timedelta(hours=48)
    stale_quotes = (
        db.query(Quote)
        .join(Requisition, Quote.requisition_id == Requisition.id)
        .filter(
            Requisition.created_by == user.id,
            Quote.status == "sent",
            Quote.sent_at < cutoff_48h,
            Quote.followup_alert_sent_at.is_(None),
        )
        .all()
    )
    followup_items = []
    for q in stale_quotes:
        sent = q.sent_at.replace(tzinfo=timezone.utc) if q.sent_at and q.sent_at.tzinfo is None else q.sent_at
        hours_ago = int((now - sent).total_seconds() / 3600) if sent else 0
        followup_items.append(f"  Q-{q.quote_number} — quoted {hours_ago}h ago, no response")
        q.followup_alert_sent_at = now

    if followup_items:
        blocks.append("QUOTES NEEDING FOLLOW-UP\n" + "\n".join(followup_items[:5]))

    # Block 3: Overnight vendor quotes (last 24h)
    cutoff_24h = now - timedelta(hours=24)
    overnight = (
        db.query(VendorResponse)
        .join(Contact, VendorResponse.contact_id == Contact.id)
        .filter(
            Contact.user_id == user.id,
            VendorResponse.classification == "quoted",
            VendorResponse.confidence >= 0.7,
            VendorResponse.created_at > cutoff_24h,
        )
        .all()
    )
    if overnight:
        overnight_items = []
        for vr in overnight[:5]:
            mpn = vr.parsed_data.get("mpn", "N/A") if vr.parsed_data else "N/A"
            overnight_items.append(f"  {vr.vendor_name} on {mpn}")
        block = "OVERNIGHT QUOTES\n" + "\n".join(overnight_items)
        if len(overnight) > 5:
            block += f"\n  +{len(overnight) - 5} more"
        blocks.append(block)

    # Block 4: AI Insights (Phase 2)
    try:
        from ..services.activity_insights import get_user_insights

        insights = get_user_insights(user.id, db, max_insights=3)
        if insights:
            insight_lines = []
            for ins in insights:
                insight_lines.append(f"  {ins['title']} — {ins.get('action', '')}")
            blocks.append("AI INSIGHTS\n" + "\n".join(insight_lines))
    except Exception:
        logger.debug("AI insights block failed for user %d", user.id, exc_info=True)

    # Compose final message — use AI to make it clean if we have blocks
    if not blocks:
        msg = f"Good morning {user.name or 'team'} — all caught up."
    else:
        raw = f"Good morning {user.name or 'team'}:\n\n" + "\n\n".join(blocks)
        msg = await _ai_clean_briefing(raw) or raw

    ok = await send_alert(db, user.id, msg, "morning_briefing", str(user.id))
    if ok and stale_quotes:
        db.commit()  # Persist followup_alert_sent_at stamps


async def _ai_clean_briefing(raw_text: str) -> str | None:
    """Use AI to clean up and consolidate a briefing into a scannable summary."""
    try:
        from ..utils.claude_client import claude_text

        result = await claude_text(
            f"Rewrite this morning briefing as a clean, scannable Teams message. "
            f"Keep it under 500 chars. Use bullet points. No greetings beyond the first line. "
            f"Merge similar items. Drop redundant info:\n\n{raw_text}",
            system="You rewrite business notifications to be concise and scannable. Plain text only, no markdown.",
            model_tier="fast",
            max_tokens=400,
        )
        return result
    except Exception:
        return None


@_traced_job
async def _job_director_daily_digest():
    """Build and send daily digest to all managers."""
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        from ..models.auth import User

        managers = db.query(User).filter(User.role == "manager", User.is_active.is_(True)).all()
        for mgr in managers:
            try:
                await _send_director_digest(mgr, db)
            except Exception:
                logger.debug("Director digest failed for user %d", mgr.id, exc_info=True)
    except Exception:
        logger.exception("Director daily digest job failed")
    finally:
        db.close()


async def _send_director_digest(user, db):
    """Compose and send one manager's daily digest."""
    from sqlalchemy import func

    from ..models.auth import User
    from ..models.offers import Contact, Offer
    from ..models.quotes import Quote
    from ..models.sourcing import Requisition
    from ..models.teams_alert_config import TeamsAlertConfig
    from ..services.teams_alert_service import send_alert

    config = db.query(TeamsAlertConfig).filter(TeamsAlertConfig.user_id == user.id).first()
    if config and not config.alerts_enabled:
        return

    now = datetime.now(timezone.utc)
    today = now.strftime("%b %d")
    blocks = []

    # Block 1: High-value deals idle >48h
    cutoff_48h = now - timedelta(hours=48)
    idle_reqs = (
        db.query(Requisition)
        .filter(
            Requisition.status == "offers",
            Requisition.updated_at < cutoff_48h,
        )
        .all()
    )
    idle_items = []
    for req in idle_reqs[:5]:
        creator = db.get(User, req.created_by) if req.created_by else None
        updated = (
            req.updated_at.replace(tzinfo=timezone.utc)
            if req.updated_at and req.updated_at.tzinfo is None
            else req.updated_at
        )
        days_idle = (now - updated).days if updated else 0
        idle_items.append(f"  {req.name} — {creator.name if creator else 'N/A'} — {days_idle}d idle")
    if idle_items:
        blocks.append("DEALS NEEDING ATTENTION\n" + "\n".join(idle_items))

    # Block 2: Response times (7-day trailing avg per AM)
    week_ago = now - timedelta(days=7)
    ams = db.query(User).filter(User.is_active.is_(True), User.role.in_(["buyer", "sales", "trader"])).all()
    resp_times = []
    for am in ams:
        avg_hours = (
            db.query(
                func.avg(func.extract("epoch", Contact.created_at) - func.extract("epoch", Requisition.created_at))
                / 3600
            )
            .join(Requisition, Contact.requisition_id == Requisition.id)
            .filter(Contact.user_id == am.id, Contact.created_at > week_ago)
            .scalar()
        )
        if avg_hours is not None:
            resp_times.append((am.name or am.email, round(float(avg_hours), 1)))

    if resp_times:
        team_avg = sum(t for _, t in resp_times) / len(resp_times) if resp_times else 0
        lines = [f"  Team avg: {round(team_avg, 1)}h"]
        for name, hours in sorted(resp_times, key=lambda x: x[1], reverse=True)[:5]:
            flag = " (!)" if team_avg > 0 and hours > team_avg * 2 else ""
            lines.append(f"  {name}: {hours}h{flag}")
        blocks.append("RESPONSE TIMES (7d)\n" + "\n".join(lines))

    # Block 3: Workload snapshot
    week_start = now - timedelta(days=7)
    workload_lines = []
    for am in ams:
        open_count = (
            db.query(Requisition)
            .filter(Requisition.created_by == am.id, Requisition.status.in_(["active", "sourcing", "offers"]))
            .count()
        )
        quotes_sent = db.query(Quote).filter(Quote.created_by_id == am.id, Quote.sent_at > week_start).count()
        offers_count = db.query(Offer).filter(Offer.entered_by_id == am.id, Offer.created_at > week_start).count()
        if open_count or quotes_sent or offers_count:
            workload_lines.append(
                f"  {am.name or am.email}: {open_count} open / {quotes_sent} quoted / {offers_count} offers"
            )

    if workload_lines:
        blocks.append("WORKLOAD\n" + "\n".join(workload_lines))

    # Block 4: Stale accounts (no outbound contact in 5+ days)
    stale_cutoff = now - timedelta(days=5)
    from ..models.crm import Company

    stale_items = []
    owned_companies = db.query(Company).filter(Company.account_owner_id.isnot(None), Company.is_active.is_(True)).all()
    owner_stale: dict[str, list[str]] = {}
    for co in owned_companies:
        latest_contact = (
            db.query(func.max(Contact.created_at))
            .join(Requisition, Contact.requisition_id == Requisition.id)
            .filter(Requisition.customer_name == co.name)
            .scalar()
        )
        if not latest_contact or latest_contact < stale_cutoff:
            owner = db.get(User, co.account_owner_id) if co.account_owner_id else None
            owner_name = owner.name if owner else "Unassigned"
            owner_stale.setdefault(owner_name, []).append(co.name)

    for owner_name, companies in list(owner_stale.items())[:5]:
        stale_items.append(f"  {owner_name}: {', '.join(companies[:3])}")
        if len(companies) > 3:
            stale_items[-1] += f" +{len(companies) - 3}"
    if stale_items:
        blocks.append("STALE ACCOUNTS (5d+ no contact)\n" + "\n".join(stale_items))

    if not blocks:
        msg = f"AVAIL Brief {today} — all clear, nothing flagged."
    else:
        raw = f"AVAIL Brief — {today}\n\n" + "\n\n".join(blocks)
        msg = await _ai_clean_briefing(raw) or raw

    await send_alert(db, user.id, msg, "director_digest", str(user.id))
