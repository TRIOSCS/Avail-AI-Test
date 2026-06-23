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

from ..constants import MEANINGFUL_CALL_OUTCOMES, CallOutcome, RequisitionStatus
from ..scheduler import _traced_job


def register_eight_by_eight_jobs(scheduler, settings):
    """Register 8x8 CDR polling job with the scheduler."""
    if not settings.eight_by_eight_enabled:
        logger.info("8x8 CDR polling NOT registered (EIGHT_BY_EIGHT_ENABLED is false)")
        return

    missing = [
        name
        for name, val in [
            ("EIGHT_BY_EIGHT_API_KEY", settings.eight_by_eight_api_key),
            ("EIGHT_BY_EIGHT_USERNAME", settings.eight_by_eight_username),
            ("EIGHT_BY_EIGHT_PASSWORD", settings.eight_by_eight_password),
            ("EIGHT_BY_EIGHT_PBX_ID", settings.eight_by_eight_pbx_id),
        ]
        if not val
    ]
    if missing:
        logger.warning(
            "8x8 CDR polling NOT registered — enabled flag is true but credentials missing: {}",
            ", ".join(missing),
        )
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
        result = await _process_cdrs(db, settings)
        db.commit()
        logger.info(
            f"8x8 poll: {result['processed']} calls, {result['matched']} matched, {result['skipped']} skipped/dedup"
        )
    except Exception as e:
        logger.error(f"8x8 CDR poll error: {e}")
        db.rollback()
    finally:
        db.close()


async def _process_cdrs(db, settings) -> dict:
    """Core CDR processing logic — unified E.164 phone matcher.

    Fetches CDRs, runs match_phone_to_entity() on each external phone, and logs every
    call (including those where no internal user matched) with user_id=None. Ambiguous
    matches stamp details.match_ambiguous + details.candidates. Links to open
    requisitions when a company match exists.

    Returns stats dict with processed, matched, skipped counts.
    """
    from ..models import Requisition, User
    from ..models.config import SystemConfig
    from ..services.activity_service import log_call_activity, match_phone_to_entity
    from ..services.eight_by_eight_service import (
        get_access_token,
        get_cdrs,
        normalize_cdr,
    )

    # Load watermark
    watermark_row = db.query(SystemConfig).filter(SystemConfig.key == "8x8_last_poll").first()
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    if watermark_row:
        try:
            since = datetime.fromisoformat(watermark_row.value)
        except (ValueError, TypeError):
            pass

    until = datetime.now(timezone.utc)

    # Auth + fetch
    try:
        token = await get_access_token(settings)
    except ValueError as e:
        logger.error(f"8x8 auth failed, skipping poll: {e}")
        return {"processed": 0, "matched": 0, "skipped": 0}

    cdrs = await get_cdrs(token, settings, since, until)
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

        # Determine direction + external phone
        if norm["direction"] == "Outgoing":
            external_phone = norm["callee_phone"]
            contact_name = norm["callee_name"]
            direction = "outbound"
            user = ext_map.get(norm["caller_phone"])
        elif norm["direction"] == "Incoming":
            external_phone = norm["caller_phone"]
            contact_name = norm["caller_name"]
            direction = "inbound"
            user = ext_map.get(norm["extension"])
        else:
            skipped += 1
            continue

        # Run unified matcher on external phone
        match = match_phone_to_entity(external_phone, db)

        # Use CRM contact name if CDR didn't provide one
        effective_contact_name = contact_name if contact_name and contact_name != "." else None
        if not effective_contact_name and match and match.get("contact_name"):
            effective_contact_name = match["contact_name"]

        # Build details
        cdr_outcome = CallOutcome.CONNECTED if norm["is_answered"] else CallOutcome.NO_ANSWER
        cdr_details: dict = {"call_outcome": cdr_outcome.value, "source": "8x8_cdr"}
        if norm["department"]:
            cdr_details["department"] = norm["department"]
        if norm.get("recording_url"):
            cdr_details["recording_url"] = norm["recording_url"]
        if match and match.get("ambiguous"):
            cdr_details["match_ambiguous"] = True
            cdr_details["candidates"] = match.get("candidates", [])

        # --- Optimistic-row reconciliation (FIX 1) ---
        user_id_val = user.id if user else None
        optimistic = _find_optimistic_row(db, user_id_val, direction, external_phone, norm["occurred_at"])

        if optimistic is not None:
            # Enrich the existing click row instead of creating a second one.
            # Always merge additive slots; only adopt CDR call_outcome + recompute
            # is_meaningful when the optimistic row has no existing outcome
            # (preserves rep-stamped LEFT_MESSAGE against an un-answered CDR).
            optimistic.external_id = norm["external_id"]
            optimistic.occurred_at = norm["occurred_at"]
            optimistic.duration_seconds = norm["duration_seconds"]
            merged = dict(optimistic.details or {})
            merged["source"] = "8x8_cdr"
            if norm["department"]:
                merged["department"] = norm["department"]
            if norm.get("recording_url"):
                merged["recording_url"] = norm["recording_url"]
            if not merged.get("call_outcome"):
                merged["call_outcome"] = cdr_outcome.value
                optimistic.is_meaningful = cdr_outcome.value in MEANINGFUL_CALL_OUTCOMES
            optimistic.details = merged
            db.flush()
            from ..services.cadence_service import bump_clocks_from_activity

            bump_clocks_from_activity(db, optimistic)
            record = optimistic
            logger.info(f"CDR reconciled: enriched optimistic row id={optimistic.id} with callId={norm['external_id']}")
        else:
            record = log_call_activity(
                user_id=user_id_val,
                direction=direction,
                phone=external_phone,
                duration_seconds=norm["duration_seconds"],
                external_id=norm["external_id"],
                contact_name=effective_contact_name,
                db=db,
                occurred_at=norm["occurred_at"],
                details=cdr_details,
                match_result=match,
            )

        if record is None:  # dedup (re-poll of already-logged CDR)
            skipped += 1
            continue

        processed += 1

        # Open-req linking (only for company matches)
        if match and match.get("type") == "company" and match.get("company_id"):
            from ..models import CustomerSite

            open_req = (
                db.query(Requisition)
                .join(CustomerSite, Requisition.customer_site_id == CustomerSite.id)
                .filter(
                    CustomerSite.company_id == match["company_id"],
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
                db.flush()
                logger.debug(f"CDR linked to open req {open_req.id}")

        if match:
            matched += 1
            logger.info(f"CDR matched: {match['type']} '{match.get('name')}' for {external_phone}")
        elif record.company_id or record.vendor_card_id:
            matched += 1

    _update_watermark(db, watermark_row, until)
    return {"processed": processed, "matched": matched, "skipped": skipped}


def _find_optimistic_row(db, user_id, direction, external_phone, cdr_occurred_at):
    """Find an un-reconciled optimistic click-to-call row matching this CDR.

    Match key: activity_type=CALL_LOGGED, channel=PHONE, external_id IS NULL,
    user_id matches, direction matches, phone normalizes to same E.164,
    and occurred_at (or created_at) is within 10 minutes of cdr_occurred_at.

    Returns the ActivityLog row or None. Never matches if user_id is None
    (unmatched CDRs should not absorb click rows).

    Called by: _process_cdrs
    Depends on: app/models.ActivityLog, app/constants, app/utils/phone
    """
    if user_id is None:
        return None

    from datetime import timedelta, timezone

    from ..constants import ActivityType, Channel
    from ..models import ActivityLog
    from ..utils.phone import normalize_e164

    cdr_e164 = normalize_e164(external_phone)
    if cdr_e164 is None:
        return None

    window = timedelta(minutes=10)
    window_start = cdr_occurred_at - window
    window_end = cdr_occurred_at + window

    # Fetch candidate rows: unreconciled, matching user+direction+channel+type
    candidates = (
        db.query(ActivityLog)
        .filter(
            ActivityLog.activity_type == ActivityType.CALL_LOGGED,
            ActivityLog.channel == Channel.PHONE,
            ActivityLog.external_id.is_(None),
            ActivityLog.user_id == user_id,
            ActivityLog.direction == direction,
        )
        .all()
    )

    matches: list[tuple] = []
    for row in candidates:
        # Phone match: normalize both sides
        row_e164 = normalize_e164(row.contact_phone or "")
        if row_e164 != cdr_e164:
            continue
        # Time match: use occurred_at if set, else created_at
        row_time = row.occurred_at or row.created_at
        if row_time is None:
            continue
        # Make timezone-aware for comparison
        if row_time.tzinfo is None:
            row_time = row_time.replace(tzinfo=timezone.utc)
        if window_start <= row_time <= window_end:
            matches.append((abs((row_time - cdr_occurred_at).total_seconds()), row.id, row))

    if not matches:
        return None
    # Return nearest-in-time row (ties broken by lowest id)
    matches.sort(key=lambda t: (t[0], t[1]))
    return matches[0][2]


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
