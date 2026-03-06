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
            f"8x8 poll: {result['processed']} calls, "
            f"{result['matched']} matched, {result['skipped']} skipped/dedup"
        )
    except Exception as e:
        logger.error(f"8x8 CDR poll error: {e}")
        db.rollback()
    finally:
        db.close()


def _process_cdrs(db, settings) -> dict:
    """Core CDR processing logic. Returns stats dict."""
    from ..models import User
    from ..models.config import SystemConfig
    from ..services.activity_service import log_call_activity
    from ..services.eight_by_eight_service import get_access_token, get_cdrs, normalize_cdr

    # Load watermark
    watermark_row = db.query(SystemConfig).filter(SystemConfig.key == "8x8_last_poll").first()
    if watermark_row:
        since = datetime.fromisoformat(watermark_row.value)
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

        record = log_call_activity(
            user_id=user.id,
            direction=direction,
            phone=external_phone,
            duration_seconds=norm["duration_seconds"],
            external_id=norm["external_id"],
            contact_name=contact_name if contact_name and contact_name != "." else None,
            db=db,
        )

        if record is None:
            skipped += 1
        else:
            processed += 1
            if record.company_id or record.vendor_card_id:
                matched += 1

    _update_watermark(db, watermark_row, until)
    return {"processed": processed, "matched": matched, "skipped": skipped}


def _update_watermark(db, watermark_row, until: datetime):
    """Update or create the 8x8 poll watermark in system_config."""
    from ..models.config import SystemConfig

    if watermark_row:
        watermark_row.value = until.isoformat()
    else:
        db.add(SystemConfig(
            key="8x8_last_poll",
            value=until.isoformat(),
            description="Last successful 8x8 CDR poll timestamp",
        ))
    db.flush()


# ── Dry-run for testing ──────────────────────────────────────────────


def run_8x8_poll_dry_run(ext_map: dict | None = None):
    """Fetch real CDRs and print what WOULD be written, without DB writes.

    ext_map: optional {extension: SimpleNamespace(id, name)} override.
    If not provided, uses hardcoded Trio staff extensions.
    """
    from types import SimpleNamespace

    from ..config import settings
    from ..services.eight_by_eight_service import get_access_token, get_cdrs, normalize_cdr

    print("=" * 60)
    print("8x8 CDR Poll — DRY RUN (no database writes)")
    print("=" * 60)

    since = datetime.now(timezone.utc) - timedelta(hours=24)
    until = datetime.now(timezone.utc)
    print(f"Window: {since:%Y-%m-%d %H:%M} → {until:%Y-%m-%d %H:%M} UTC")
    print()

    token = get_access_token(settings)
    cdrs = get_cdrs(token, settings, since, until)
    print(f"CDRs fetched: {len(cdrs)}")
    print()

    if ext_map is None:
        ext_map = {
            "1001": SimpleNamespace(id=1, name="Michael Khoury"),
            "1002": SimpleNamespace(id=2, name="Marcus Moawad"),
            "1009": SimpleNamespace(id=5, name="Martina Tewes"),
            "1032": SimpleNamespace(id=11, name="Sally Garcia"),
        }

    print(f"Extension map: {', '.join(f'{k}={v.name}' for k, v in ext_map.items())}")
    print()

    processed = 0
    skipped = 0
    matched_users = 0

    for cdr in cdrs:
        norm = normalize_cdr(cdr)
        user = None
        external_phone = ""
        contact_name = ""

        if norm["direction"] == "Outgoing":
            user = ext_map.get(norm["caller_phone"])
            external_phone = norm["callee_phone"]
            contact_name = norm["callee_name"]
        elif norm["direction"] == "Incoming":
            user = ext_map.get(norm["extension"])
            external_phone = norm["caller_phone"]
            contact_name = norm["caller_name"]

        if user:
            direction = "outbound" if norm["direction"] == "Outgoing" else "inbound"
            status = f"WRITE → user={user.name}, {direction}, phone={external_phone}, dur={norm['duration_seconds']}s"
            if norm["is_missed"]:
                status += " [MISSED]"
            processed += 1
            matched_users += 1
        else:
            status = "SKIP (no user match)"
            skipped += 1

        caller = norm["caller_name"] or norm["caller_phone"]
        callee = norm["callee_name"] or norm["callee_phone"]
        print(f"  CDR {norm['external_id']}: {norm['direction']:8s} {caller:25s} → {callee:30s} | {status}")

    print()
    print(f"Summary: {processed} would write, {skipped} skipped")
    print("Dry run complete. No data written.")
