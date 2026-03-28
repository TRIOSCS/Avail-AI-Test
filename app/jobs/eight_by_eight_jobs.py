"""eight_by_eight_jobs.py — 8x8 Work Analytics polling job.

Polls 8x8 CDR API every 30 minutes for call activity.
Writes matched calls to activity_log and updates company.last_activity_at.

Business Rules:
- Only process calls where caller or callee extension matches
  a User with eight_by_eight_enabled = True
- Skip calls already logged (dedup on external_id)
- Unmatched phone numbers logged with company_id = null
- Watermark stored in system_config table (key='8x8_last_poll')

Called by: app/jobs/__init__.py
Depends on: app/services/eight_by_eight_service.py,
            app/services/activity_service.py
"""

from datetime import datetime, timedelta, timezone

from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from ..constants import RequisitionStatus
from ..scheduler import _traced_job


def register_eight_by_eight_jobs(scheduler, settings):
    """Register 8x8 CDR polling job with the scheduler."""
    if not settings.eight_by_eight_enabled:
        return

    scheduler.add_job(
        _job_poll_8x8_cdrs,
        IntervalTrigger(minutes=settings.eight_by_eight_poll_interval_minutes),
        id="eight_by_eight_poll",
        name="8x8 CDR poll",
    )
    logger.info(f"8x8 CDR polling registered (every {settings.eight_by_eight_poll_interval_minutes}min)")


@_traced_job
async def _job_poll_8x8_cdrs():
    """Poll 8x8 for new CDRs and write to activity_log."""
    from ..config import settings
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        result = _process_cdrs(db, settings)
        db.commit()
        logger.info(
            f"8x8 poll: {result['processed']} calls, {result['matched']} matched, {result['skipped']} skipped/dedup"
        )
    except Exception as e:
        logger.error(f"8x8 CDR poll error: {e}")
        db.rollback()
    finally:
        db.close()


def _process_cdrs(db, settings) -> dict:
    """Core CDR processing logic with CRM reverse lookup.

    After fetching CDRs, runs reverse_lookup_phone() on each external phone. If a match
    is found, sets company_id/vendor_card_id and contact_name on the ActivityLog entry.
    Also links to open requisitions when the matched company has active reqs.

    Returns stats dict with processed, matched, skipped counts.
    """
    from ..models import Requisition, User
    from ..models.config import SystemConfig
    from ..services.activity_service import log_call_activity
    from ..services.eight_by_eight_service import (
        get_access_token,
        get_cdrs,
        normalize_cdr,
        reverse_lookup_phone,
    )

    # Load watermark
    watermark_row = db.query(SystemConfig).filter(SystemConfig.key == "8x8_last_poll").first()
    if watermark_row:
        try:
            since = datetime.fromisoformat(watermark_row.value)
        except (ValueError, TypeError):
            since = datetime.now(timezone.utc) - timedelta(hours=24)
    else:
        since = datetime.now(timezone.utc) - timedelta(hours=24)

    until = datetime.now(timezone.utc)

    # Auth + fetch
    try:
        token = get_access_token(settings)
    except ValueError as e:
        logger.error(f"8x8 auth failed, skipping poll: {e}")
        return {"processed": 0, "matched": 0, "skipped": 0}

    cdrs = get_cdrs(token, settings, since, until)
    if not cdrs:
        _update_watermark(db, watermark_row, until)
        return {"processed": 0, "matched": 0, "skipped": 0}

    # Build extension → user lookup
    users = db.query(User).filter(User.eight_by_eight_enabled.is_(True)).all()
    ext_map = {u.eight_by_eight_extension: u for u in users if u.eight_by_eight_extension}

    processed = 0
    matched = 0
    skipped = 0

    for cdr in cdrs:
        norm = normalize_cdr(cdr)

        # Determine which AVAIL user owns the call
        user = None
        external_phone = ""
        contact_name = ""

        if norm["direction"] == "Outgoing":
            user = ext_map.get(norm["caller_phone"])
            external_phone = norm["callee_phone"]
            contact_name = norm["callee_name"]
        elif norm["direction"] == "Incoming":
            # For incoming, the extension is in norm["extension"]
            user = ext_map.get(norm["extension"])
            external_phone = norm["caller_phone"]
            contact_name = norm["caller_name"]
        else:
            skipped += 1
            continue

        if not user:
            skipped += 1
            continue

        # Map 8x8 direction to activity_service direction
        direction = "outbound" if norm["direction"] == "Outgoing" else "inbound"

        # Reverse lookup: try to match the external phone to a CRM entity
        crm_match = reverse_lookup_phone(external_phone, db)

        # Use CRM contact_name if CDR didn't provide one
        effective_contact_name = contact_name if contact_name and contact_name != "." else None
        if not effective_contact_name and crm_match and crm_match.get("contact_name"):
            effective_contact_name = crm_match["contact_name"]

        record = log_call_activity(
            user_id=user.id,
            direction=direction,
            phone=external_phone,
            duration_seconds=norm["duration_seconds"],
            external_id=norm["external_id"],
            contact_name=effective_contact_name,
            db=db,
        )

        if record is None:
            skipped += 1
            continue

        processed += 1

        # Apply CRM linking from reverse lookup (overrides activity_service match)
        if crm_match:
            if crm_match["entity_type"] in ("contact", "company"):
                record.company_id = crm_match["company_id"]
                if crm_match.get("site_id"):
                    record.customer_site_id = crm_match["site_id"]
                if crm_match.get("entity_type") == "contact":
                    record.site_contact_id = crm_match["entity_id"]
                # Link to open requisition if company has one
                from ..models import CustomerSite

                open_req = (
                    db.query(Requisition)
                    .join(CustomerSite, Requisition.customer_site_id == CustomerSite.id)
                    .filter(
                        CustomerSite.company_id == crm_match["company_id"],
                        Requisition.status.in_(
                            [
                                RequisitionStatus.ACTIVE,
                                RequisitionStatus.SOURCING,
                                RequisitionStatus.OFFERS,
                            ]
                        ),
                    )
                    .first()
                )
                if open_req:
                    record.requisition_id = open_req.id
                    logger.debug(f"CDR linked to open req {open_req.id} for company {crm_match['company_name']}")
            elif crm_match["entity_type"] == "vendor":
                record.vendor_card_id = crm_match["vendor_card_id"]
            matched += 1
            db.flush()
            logger.info(
                f"CDR reverse-linked: {crm_match['entity_type']} "
                f"'{crm_match.get('company_name')}' for phone {external_phone}"
            )
        elif record.company_id or record.vendor_card_id:
            matched += 1

    _update_watermark(db, watermark_row, until)
    return {"processed": processed, "matched": matched, "skipped": skipped}


def _update_watermark(db, watermark_row, until: datetime):
    """Update or create the 8x8 poll watermark in system_config."""
    from ..models.config import SystemConfig

    if watermark_row:
        watermark_row.value = until.isoformat()
    else:
        db.add(
            SystemConfig(
                key="8x8_last_poll",
                value=until.isoformat(),
                description="Last successful 8x8 CDR poll timestamp",
            )
        )
    db.flush()
