"""Notification intelligence scheduled jobs — batch digest delivery.

Collects BATCH-queued alerts from Redis and sends a single digest DM
to each user every 2 hours.

Called by: app/jobs/__init__.py via register_notify_intelligence_jobs()
Depends on: app.services.notify_intelligence, app.services.teams_alert_service
"""

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from ..scheduler import _traced_job


def register_notify_intelligence_jobs(scheduler, settings):
    """Register notification intelligence jobs."""
    scheduler.add_job(
        _job_batch_digest,
        IntervalTrigger(hours=2),
        id="notify_batch_digest",
        name="Notification batch digest",
    )
    scheduler.add_job(
        _job_deal_risk_scan,
        CronTrigger(hour=11, minute=0),  # 3:00 AM PT = 11:00 UTC
        id="deal_risk_scan",
        name="Daily deal risk scan",
    )


@_traced_job
async def _job_batch_digest():
    """Collect batched alerts and send digest DMs to each user."""
    from ..database import SessionLocal
    from ..models.auth import User
    from ..services.notify_intelligence import get_batch_queue, is_intelligence_enabled

    if not is_intelligence_enabled():
        return

    db = SessionLocal()
    try:
        users = db.query(User).filter(User.is_active.is_(True)).all()
        sent_count = 0

        for user in users:
            try:
                items = get_batch_queue(user.id)
                if not items:
                    continue

                digest_msg = _format_digest(user.name or user.email, items)
                if digest_msg:
                    from ..services.teams_alert_service import _try_graph_dm, _try_webhook, _mark_sent, _log_alert
                    from ..models.teams_alert_config import TeamsAlertConfig

                    graph_ok = await _try_graph_dm(user, digest_msg, db)
                    if graph_ok:
                        _mark_sent(user.id)
                        _log_alert(db, "batch_digest", str(user.id), True, user_id=user.id)
                        sent_count += 1
                    else:
                        config = db.query(TeamsAlertConfig).filter(TeamsAlertConfig.user_id == user.id).first()
                        if config and config.teams_webhook_url:
                            webhook_ok = await _try_webhook(config.teams_webhook_url, digest_msg)
                            if webhook_ok:
                                _mark_sent(user.id)
                                _log_alert(db, "batch_digest", str(user.id), True, user_id=user.id)
                                sent_count += 1

            except Exception:
                logger.debug("Batch digest failed for user %d", user.id, exc_info=True)

        if sent_count:
            logger.info("Batch digest sent to %d users", sent_count)

    except Exception:
        logger.exception("Batch digest job failed")
    finally:
        db.close()


@_traced_job
async def _job_deal_risk_scan():
    """Daily scan of active requisitions for risk transitions."""
    from ..database import SessionLocal
    from ..services.deal_risk import scan_active_requisitions

    db = SessionLocal()
    try:
        results = scan_active_requisitions(db)
        red_deals = [r for r in results if r["risk_level"] == "red"]

        if not red_deals:
            logger.debug("Deal risk scan: no red-risk deals found")
            return

        # Alert each deal's creator about red-risk transitions
        from ..services.teams_alert_service import send_alert
        from ..services.notify_intelligence import is_intelligence_enabled

        r = None
        if is_intelligence_enabled():
            from ..services.notify_intelligence import _get_redis
            r = _get_redis()

        alerted = 0
        for deal in red_deals:
            req_id = deal["requisition_id"]
            user_id = deal.get("created_by")
            if not user_id:
                continue

            # Check if we already alerted for this deal today
            cache_key = f"risk_alert:{req_id}"
            if r:
                try:
                    if r.exists(cache_key):
                        continue
                except Exception:
                    pass

            msg = (
                f"DEAL AT RISK: {deal.get('requisition_name', f'Req #{req_id}')} "
                f"({deal.get('customer_name', 'N/A')})\n"
                f"{deal['explanation']}\n"
                f"Action: {deal['suggested_action']}"
            )

            ok = await send_alert(db, user_id, msg, "deal_risk", str(req_id))
            if ok:
                alerted += 1
                if r:
                    try:
                        r.setex(cache_key, 86400, "1")  # 24h dedup
                    except Exception:
                        pass

        if alerted:
            logger.info("Deal risk scan: alerted %d users about %d red-risk deals", alerted, len(red_deals))

    except Exception:
        logger.exception("Deal risk scan job failed")
    finally:
        db.close()


def _format_digest(user_name: str, items: list[dict]) -> str | None:
    """Format batched alerts into a single digest message."""
    if not items:
        return None

    # Group by event_type
    groups: dict[str, list[str]] = {}
    for item in items:
        et = item.get("event_type", "other")
        msg = item.get("message", "")
        if msg:
            groups.setdefault(et, []).append(msg[:200])

    lines = [f"ALERT DIGEST ({len(items)} batched)"]
    for event_type, messages in groups.items():
        label = event_type.replace("_", " ").upper()
        lines.append(f"\n{label}:")
        for msg in messages[:5]:
            # Extract first meaningful line
            first_line = msg.split("\n")[0][:150]
            lines.append(f"  - {first_line}")
        if len(messages) > 5:
            lines.append(f"  +{len(messages) - 5} more")

    return "\n".join(lines)
