"""Inventory background jobs — PO verification, stock auto-complete, stock list parsing.

Called by: app/jobs/__init__.py via register_inventory_jobs()
Depends on: app.database, app.models, app.services.buyplan_service
"""

import asyncio
import base64
from datetime import datetime, timezone

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from ..scheduler import _traced_job


def register_inventory_jobs(scheduler, settings):
    """Register inventory/buy-plan jobs with the scheduler."""
    scheduler.add_job(
        _job_po_verification,
        IntervalTrigger(minutes=settings.po_verify_interval_min),
        id="po_verification",
        name="PO verification",
    )
    scheduler.add_job(
        _job_stock_autocomplete,
        CronTrigger(hour=settings.buyplan_auto_complete_hour, timezone=settings.buyplan_auto_complete_tz),
        id="stock_autocomplete",
        name="Stock sale auto-complete",
    )


@_traced_job
async def _job_po_verification():
    """Verify PO sent status for pending buy plans."""
    from ..database import SessionLocal
    from ..models import BuyPlan

    db = SessionLocal()
    try:
        from ..services.buyplan_service import verify_po_sent

        unverified_plans = db.query(BuyPlan).filter(BuyPlan.status == "po_entered").all()

        async def _safe_verify(plan):
            try:
                await verify_po_sent(plan, db)
            except Exception as e:
                logger.error(f"PO verify error for plan {plan.id}: {e}")

        if unverified_plans:
            await asyncio.gather(*[_safe_verify(p) for p in unverified_plans])
    except Exception as e:
        logger.error(f"PO verification scan error: {e}")
        db.rollback()
    finally:
        db.close()


@_traced_job
async def _job_stock_autocomplete():
    """Auto-complete stock sales at configured hour."""
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        from ..services.buyplan_service import auto_complete_stock_sales

        loop = asyncio.get_running_loop()
        completed = await asyncio.wait_for(
            loop.run_in_executor(None, auto_complete_stock_sales, db),
            timeout=300,
        )
        if completed:
            logger.info(f"Stock sale auto-complete: {completed} plan(s) completed")
    except asyncio.TimeoutError:
        logger.error("Stock sale auto-complete timed out after 300s")
        db.rollback()
    except Exception as e:
        logger.error(f"Stock sale auto-complete error: {e}")
        db.rollback()
    finally:
        db.close()


# ── Stock List Helpers ──────────────────────────────────────────────────


async def _scan_stock_list_attachments(user, db, is_backfill: bool = False):
    """Find and process stock list attachments from vendor emails."""
    from ..config import settings
    from ..connectors.email_mining import EmailMiner
    from ..utils.token_manager import get_valid_token

    lookback = settings.inbox_backfill_days if is_backfill else 1

    fresh_token = await get_valid_token(user, db) or user.access_token
    miner = EmailMiner(fresh_token, db=db, user_id=user.id)
    stock_emails = await miner.scan_for_stock_lists(lookback_days=lookback)

    if not stock_emails:
        return

    logger.info(f"Stock list scan [{user.email}]: found {len(stock_emails)} emails with attachments")

    for email_info in stock_emails:
        for att_info in email_info.get("stock_files", []):
            try:
                await _download_and_import_stock_list(
                    user,
                    db,
                    message_id=att_info["message_id"],
                    attachment_id=att_info["attachment_id"],
                    filename=att_info["filename"],
                    vendor_name=email_info.get("vendor_name", "Unknown"),
                    vendor_email=email_info.get("from_email", ""),
                )
            except Exception as e:
                logger.error(f"Stock list import failed [{att_info.get('filename')}]: {e}")


async def _download_and_import_stock_list(
    user,
    db,
    message_id: str,
    attachment_id: str,
    filename: str,
    vendor_name: str,
    vendor_email: str,
):
    """Download an attachment via Graph API and import as material cards + sightings."""
    from ..models import MaterialCard, MaterialVendorHistory
    from ..utils.normalization import normalize_mpn, normalize_mpn_key
    from ..utils.token_manager import get_valid_token
    from ..vendor_utils import normalize_vendor_name

    # Extract vendor domain from email for column mapping cache
    vendor_domain = ""
    if vendor_email and "@" in vendor_email:
        vendor_domain = vendor_email.split("@", 1)[1].lower()

    # Download the attachment via GraphClient (H1: immutable IDs, H6: retry)
    from app.utils.graph_client import GraphClient

    dl_token = await get_valid_token(user, db) or user.access_token
    gc = GraphClient(dl_token)
    try:
        att_data = await gc.get_json(f"/me/messages/{message_id}/attachments/{attachment_id}")
    except Exception as e:
        logger.warning(f"Attachment download failed: {e}")
        return

    if not att_data or "error" in att_data:
        logger.warning(f"Attachment download error: {att_data}")
        return

    content_bytes = att_data.get("contentBytes")
    if not content_bytes:
        return

    file_bytes = base64.b64decode(content_bytes)

    # H3: Validate file type before parsing
    from app.utils.file_validation import validate_file

    is_valid, detected_type = validate_file(file_bytes, filename)
    if not is_valid:
        logger.warning(f"File validation failed for {filename}: detected {detected_type}")
        return

    # Parse the file — use new AI-powered parser (Upgrade 2), fallback to legacy
    try:
        from app.services.attachment_parser import parse_attachment

        rows = await parse_attachment(file_bytes, filename, vendor_domain=vendor_domain, db=db)
    except Exception as e:
        logger.warning(f"AI attachment parser failed, using legacy parser: {e}")
        rows = _parse_stock_file(file_bytes, filename)

    if not rows:
        logger.info(f"No valid rows in {filename}")
        return

    # Phase 2B: Classify sender — stock list (vendor) vs excess list (customer)
    sender_match = None
    is_excess_list = False
    source_company_id = None
    if vendor_email:
        from app.services.activity_service import match_email_to_entity

        sender_match = match_email_to_entity(vendor_email, db)
        if sender_match and sender_match["type"] == "company":
            is_excess_list = True
            source_company_id = sender_match["id"]  # noqa: F841
            logger.info(f"Excess list detected from company '{sender_match['name']}' ({vendor_email}): {filename}")

    # Import into material cards — batch pre-load for performance
    imported = 0
    norm_vendor = normalize_vendor_name(vendor_name)

    # Pre-load existing MaterialCards in one query instead of per-row
    valid_mpns = list(
        {
            normalize_mpn_key((row.get("mpn") or "").strip())
            for row in rows
            if (row.get("mpn") or "").strip() and len((row.get("mpn") or "").strip()) >= 3
        }
    )
    valid_mpns = [k for k in valid_mpns if k]  # filter empty keys
    card_map = {}
    mvh_map = {}
    if valid_mpns:
        # Batch in chunks of 500 to keep IN clause manageable
        for i in range(0, len(valid_mpns), 500):
            chunk = valid_mpns[i : i + 500]
            for c in (
                db.query(MaterialCard)
                .filter(MaterialCard.normalized_mpn.in_(chunk), MaterialCard.deleted_at.is_(None))
                .all()
            ):
                card_map[c.normalized_mpn] = c
        # Pre-load existing MVH entries for this vendor
        existing_card_ids = [c.id for c in card_map.values()]
        if existing_card_ids:
            for i in range(0, len(existing_card_ids), 500):
                chunk = existing_card_ids[i : i + 500]
                for m in (
                    db.query(MaterialVendorHistory)
                    .filter(
                        MaterialVendorHistory.material_card_id.in_(chunk),
                        MaterialVendorHistory.vendor_name == norm_vendor,
                    )
                    .all()
                ):
                    mvh_map[m.material_card_id] = m

    for row in rows:
        raw_mpn = (row.get("mpn") or "").strip()
        if not raw_mpn or len(raw_mpn) < 3:
            continue
        norm_key = normalize_mpn_key(raw_mpn)
        if not norm_key:
            continue

        card = card_map.get(norm_key)
        if not card:
            card = MaterialCard(
                normalized_mpn=norm_key,
                display_mpn=normalize_mpn(raw_mpn) or raw_mpn,
                manufacturer=row.get("manufacturer", ""),
                description=row.get("description", ""),
            )
            db.add(card)
            try:
                db.flush()
                card_map[norm_key] = card
            except Exception as e:
                logger.debug(f"MaterialCard flush conflict for '{norm_key}': {e}")
                db.rollback()
                continue

        # Add/update vendor history (richer fields from Upgrade 2)
        mvh = mvh_map.get(card.id)
        if mvh:
            mvh.last_seen = datetime.now(timezone.utc)
            mvh.times_seen = (mvh.times_seen or 0) + 1
            if row.get("qty"):
                mvh.last_qty = row["qty"]
            if row.get("unit_price"):
                mvh.last_price = row["unit_price"]
            elif row.get("price"):
                mvh.last_price = row["price"]
            if row.get("manufacturer"):
                mvh.last_manufacturer = row["manufacturer"]
        else:
            mvh = MaterialVendorHistory(
                material_card_id=card.id,
                vendor_name=norm_vendor,
                vendor_name_normalized=norm_vendor,
                source_type="excess_list" if is_excess_list else "email_auto_import",
                last_qty=row.get("qty"),
                last_price=row.get("unit_price") or row.get("price"),
                last_manufacturer=row.get("manufacturer", ""),
            )
            db.add(mvh)

        imported += 1

    try:
        db.commit()
        list_type = "excess list" if is_excess_list else "stock list"
        logger.info(f"Auto-imported {imported} parts from {list_type} {filename} ({vendor_name})")
    except Exception as e:
        logger.error(f"Stock list commit failed: {e}")
        db.rollback()
        return

    # Teams: check if imported MPNs match any open requirements
    try:
        from sqlalchemy import func as sa_func

        from app.models import Requirement, Requisition
        from app.services.teams import send_stock_match_alert

        imported_mpns = [
            normalize_mpn(r.get("mpn", "")) or r.get("mpn", "").strip().upper() for r in rows if r.get("mpn")
        ]
        if imported_mpns:
            matches = (
                db.query(Requirement.id, Requirement.primary_mpn, Requirement.requisition_id)
                .join(Requisition, Requirement.requisition_id == Requisition.id)
                .filter(
                    Requisition.status.in_(["active", "sourcing", "offers"]),
                    sa_func.upper(Requirement.primary_mpn).in_(imported_mpns),
                )
                .all()
            )
            if matches:
                match_list = [
                    {"mpn": m.primary_mpn, "requirement_id": m.id, "requisition_id": m.requisition_id} for m in matches
                ]
                await send_stock_match_alert(
                    matches=match_list,
                    filename=filename,
                    vendor_name=vendor_name,
                )
    except Exception as e:
        logger.debug(f"Teams stock match check skipped: {e}")


def _parse_stock_file(file_bytes: bytes, filename: str) -> list[dict]:
    """Parse a stock list file (CSV/XLSX) into rows with mpn, qty, price, manufacturer."""
    from ..file_utils import normalize_stock_row, parse_tabular_file

    raw_rows = parse_tabular_file(file_bytes, filename)
    rows = []
    for r in raw_rows:
        parsed = normalize_stock_row(r)
        if parsed:
            rows.append(parsed)
    return rows[:5000]  # Cap at 5000 rows per file
