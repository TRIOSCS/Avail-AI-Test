"""Teams call records sync job.

Polls Microsoft Graph for Teams call records and logs them to ActivityLog.

Called by: app/jobs/__init__.py (registered with APScheduler)
Depends on: app/utils/graph_client.py, app/services/activity_service.py
"""

from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from ..scheduler import _traced_job


def register_teams_call_jobs(scheduler, settings):
    """Register Teams call records sync job."""
    scheduler.add_job(
        _job_sync_teams_calls,
        IntervalTrigger(hours=6),
        id="teams_call_records_sync",
        name="Sync Teams call records to activity log",
    )


@_traced_job
async def _job_sync_teams_calls():
    """Sync Teams call records for all connected users."""
    from datetime import datetime, timedelta, timezone

    from ..constants import UserRole
    from ..database import SessionLocal
    from ..models.auth import User

    db = SessionLocal()
    try:
        from ..models.config import SystemConfig
        from ..services.activity_service import log_call_activity
        from ..utils.graph_client import GraphClient
        from ..utils.token_manager import get_valid_token

        # Watermark
        wm_key = "teams_calls_last_poll"
        wm_row = db.query(SystemConfig).filter(SystemConfig.key == wm_key).first()
        since = datetime.now(timezone.utc) - timedelta(days=1)
        if wm_row and wm_row.value:
            try:
                since = datetime.fromisoformat(wm_row.value)
            except ValueError:
                pass

        users = (
            db.query(User)
            .filter(User.m365_connected.is_(True), User.role.in_([UserRole.BUYER, UserRole.SALES, UserRole.TRADER]))
            .all()
        )

        total_logged = 0
        for user in users:
            token = await get_valid_token(user, db)
            if not token:
                continue

            gc = GraphClient(token)
            try:
                records = await gc.get_all_pages(
                    "/me/callRecords",
                    params={
                        "$filter": f"startDateTime gt {since.isoformat()}",
                        "$select": "id,startDateTime,endDateTime,type,modalities",
                        "$top": "50",
                        "$orderby": "startDateTime desc",
                    },
                    max_items=100,
                )
            except Exception as e:
                logger.warning(f"Teams call records fetch failed for {user.email}: {e}")
                continue

            for record in records:
                call_id = record.get("id")
                if not call_id:
                    continue

                start = record.get("startDateTime")
                end = record.get("endDateTime")
                duration = 0
                if start and end:
                    try:
                        duration = int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
                    except (ValueError, TypeError):
                        pass

                log_call_activity(
                    user_id=user.id,
                    direction="outbound",
                    phone="",
                    duration_seconds=duration,
                    external_id=f"teams-call-{call_id}",
                    contact_name=None,
                    db=db,
                )
                total_logged += 1

        db.commit()

        # Update watermark
        now_str = datetime.now(timezone.utc).isoformat()
        if wm_row:
            wm_row.value = now_str
        else:
            db.add(SystemConfig(key=wm_key, value=now_str, description="Teams call records last poll"))
        db.commit()

        if total_logged:
            logger.info(f"Teams call sync: logged {total_logged} records for {len(users)} users")

    except Exception as e:
        logger.exception(f"Teams call records sync failed: {e}")
        db.rollback()
        raise
    finally:
        db.close()
