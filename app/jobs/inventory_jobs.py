"""Inventory background jobs — PO verification, stock auto-complete, stock list parsing.

Called by: app/jobs/__init__.py via register_inventory_jobs()
Depends on: app.database, app.models, app.services.buyplan_workflow
"""

import asyncio
import base64
from datetime import datetime, timedelta, timezone

import sqlalchemy.exc
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from ..constants import RequisitionStatus
from ..scheduler import _traced_job
from ..services.price_snapshot_service import record_price_snapshot


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
    scheduler.add_job(
        _job_buyplan_nudge,
        IntervalTrigger(minutes=30),
        id="buyplan_nudge",
        name="Buy plan unconfirmed-instruction nudge",
    )


@_traced_job
async def _job_po_verification():
    """Verify PO sent status for active buy plans with pending_verify lines."""
    from ..database import SessionLocal
    from ..models.buy_plan import BuyPlan, BuyPlanLineStatus, BuyPlanStatus

    db = SessionLocal()
    try:
        from ..services.buyplan_workflow import verify_po_sent

        # Find active plans that have lines in pending_verify status
        plans = db.query(BuyPlan).filter(BuyPlan.status == BuyPlanStatus.ACTIVE.value).all()
        # Filter to plans with at least one pending_verify line
        plans_to_verify = [
            p for p in plans if any(line.status == BuyPlanLineStatus.PENDING_VERIFY.value for line in p.lines)
        ]

        async def _safe_verify(plan):
            try:
                await verify_po_sent(plan, db)
            except Exception as e:
                logger.exception(f"PO verify error for plan {plan.id}: {e}")

        if plans_to_verify:
            await asyncio.gather(*[_safe_verify(p) for p in plans_to_verify])
            # verify_po_sent flushes (not commits); the job owns the transaction.
            db.commit()
    except Exception as e:
        logger.exception(f"PO verification scan error: {e}")
        db.rollback()
        raise
    finally:
        db.close()


@_traced_job
async def _job_stock_autocomplete():
    """Auto-complete stock sales stuck in active for 1+ hours (safety net)."""
    from ..database import SessionLocal
    from ..models.buy_plan import BuyPlan, BuyPlanStatus

    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        stuck = (
            db.query(BuyPlan)
            .filter(
                BuyPlan.is_stock_sale == True,  # noqa: E712
                BuyPlan.status == BuyPlanStatus.ACTIVE.value,
                BuyPlan.approved_at < cutoff,
            )
            .all()
        )

        from ..services.buyplan_notifications import notify_stock_sale_approved, run_notify_bg
        from ..services.buyplan_workflow import _complete_plan

        completed_ids = []
        for plan in stuck:
            # Route through the shared completion helper so a case report is generated
            # (matches the normal auto-complete path; no silent direct status flip).
            _complete_plan(plan, db)
            logger.info(f"Auto-completed stuck stock sale plan #{plan.id}")
            completed_ids.append(plan.id)

        if completed_ids:
            db.commit()
            logger.info(f"Stock sale auto-complete: {len(completed_ids)} plan(s) completed")
            for plan_id in completed_ids:
                await run_notify_bg(notify_stock_sale_approved, plan_id)
    except Exception as e:
        logger.exception(f"Stock sale auto-complete error: {e}")
        db.rollback()
        raise
    finally:
        db.close()


@_traced_job
async def _job_buyplan_nudge():
    """Nudge buyers and ops when buy-plan lines sit unconfirmed too long.

    Buyer nudge: awaiting_po lines whose plan was approved > buyplan_nudge_buyer_hours ago.
    Ops nudge:   pending_verify lines whose PO was confirmed > buyplan_nudge_ops_hours ago.
    Idempotency: last_nudge_at is stamped per line, so a stuck line is re-nudged at most
    once per threshold window regardless of the 30-min job cadence.
    """
    from ..config import settings
    from ..database import SessionLocal
    from ..models.buy_plan import BuyPlan, BuyPlanLine, BuyPlanLineStatus, BuyPlanStatus
    from ..services.buyplan_notifications import notify_nudge_buyer, notify_nudge_ops

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        buyer_cutoff = now - timedelta(hours=settings.buyplan_nudge_buyer_hours)
        ops_cutoff = now - timedelta(hours=settings.buyplan_nudge_ops_hours)

        buyer_lines = (
            db.query(BuyPlanLine)
            .join(BuyPlan, BuyPlanLine.buy_plan_id == BuyPlan.id)
            .filter(
                BuyPlan.status == BuyPlanStatus.ACTIVE.value,
                BuyPlanLine.status == BuyPlanLineStatus.AWAITING_PO.value,
                BuyPlanLine.buyer_id.isnot(None),
                BuyPlan.approved_at.isnot(None),
                BuyPlan.approved_at < buyer_cutoff,
                (BuyPlanLine.last_nudge_at.is_(None)) | (BuyPlanLine.last_nudge_at < buyer_cutoff),
            )
            .all()
        )
        ops_lines = (
            db.query(BuyPlanLine)
            .join(BuyPlan, BuyPlanLine.buy_plan_id == BuyPlan.id)
            .filter(
                BuyPlan.status == BuyPlanStatus.ACTIVE.value,
                BuyPlanLine.status == BuyPlanLineStatus.PENDING_VERIFY.value,
                BuyPlanLine.po_confirmed_at.isnot(None),
                BuyPlanLine.po_confirmed_at < ops_cutoff,
                (BuyPlanLine.last_nudge_at.is_(None)) | (BuyPlanLine.last_nudge_at < ops_cutoff),
            )
            .all()
        )

        buyer_nudged = 0
        for line in buyer_lines:
            try:
                if await notify_nudge_buyer(line.buy_plan, line, db):
                    line.last_nudge_at = now
                    buyer_nudged += 1
            except Exception as e:
                logger.exception(f"Buyer nudge failed for line {line.id}: {e}")
        ops_nudged = 0
        for line in ops_lines:
            try:
                if await notify_nudge_ops(line.buy_plan, line, db):
                    line.last_nudge_at = now
                    ops_nudged += 1
            except Exception as e:
                logger.exception(f"Ops nudge failed for line {line.id}: {e}")

        if buyer_nudged or ops_nudged:
            db.commit()
            logger.info("Buy plan nudge: {} buyer, {} ops", buyer_nudged, ops_nudged)
    except Exception as e:
        logger.exception(f"Buy plan nudge job error: {e}")
        db.rollback()
        raise
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
            except (OSError, ValueError, KeyError) as e:
                logger.warning(f"Stock list import failed [{att_info.get('filename')}]: {e}")
            except Exception as e:
                logger.exception(f"Stock list import unexpected error [{att_info.get('filename')}]: {e}")


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
    imported_for_matching: list[dict] = []

    # Pre-load existing MaterialCards in one query instead of per-row
    valid_mpn_keys = set()
    for row in rows:
        raw_mpn = (row.get("mpn") or "").strip()
        if len(raw_mpn) < 3:
            continue
        key = normalize_mpn_key(raw_mpn)
        if key:
            valid_mpn_keys.add(key)
    valid_mpns = list(valid_mpn_keys)
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
            except sqlalchemy.exc.IntegrityError as e:
                logger.warning(f"MaterialCard flush conflict for '{norm_key}': {e}")
                db.rollback()
                continue

        # Add/update vendor history (richer fields from Upgrade 2)
        price = row.get("unit_price") or row.get("price")
        mvh = mvh_map.get(card.id)
        if mvh:
            mvh.last_seen = datetime.now(timezone.utc)
            mvh.times_seen = (mvh.times_seen or 0) + 1
            if row.get("qty"):
                mvh.last_qty = row["qty"]
            if price:
                mvh.last_price = price
            if row.get("manufacturer"):
                mvh.last_manufacturer = row["manufacturer"]
        else:
            mvh = MaterialVendorHistory(
                material_card_id=card.id,
                vendor_name=norm_vendor,
                vendor_name_normalized=norm_vendor,
                source_type="excess_list" if is_excess_list else "email_auto_import",
                last_qty=row.get("qty"),
                last_price=price,
                last_manufacturer=row.get("manufacturer", ""),
            )
            db.add(mvh)
        record_price_snapshot(
            db=db, material_card_id=card.id, vendor_name=norm_vendor, price=price, source="email_auto_import"
        )

        imported += 1
        imported_for_matching.append(
            {
                "row": row,
                "norm_key": norm_key,
                "display_mpn": normalize_mpn(raw_mpn) or raw_mpn.strip().upper(),
                "material_card_id": card.id,
            }
        )

    try:
        db.commit()
        list_type = "excess list" if is_excess_list else "stock list"
        logger.info(f"Auto-imported {imported} parts from {list_type} {filename} ({vendor_name})")
    except sqlalchemy.exc.SQLAlchemyError as e:
        logger.error(f"Stock list commit failed: {e}")
        db.rollback()
        return

    # Auto-create actionable sightings for open requirements that match imported stock.
    try:
        from sqlalchemy import func as sa_func

        from app.models import Requirement, Requisition, Sighting
        from app.utils import safe_float, safe_int
        from app.utils.normalization import (
            normalize_condition,
            normalize_date_code,
            normalize_lead_time,
            normalize_packaging,
        )

        source_type = "excess_list" if is_excess_list else "email_auto_import"
        imported_mpns_upper = sorted(
            {item["display_mpn"].upper() for item in imported_for_matching if item["display_mpn"]}
        )
        matches = []
        if imported_mpns_upper:
            matches = (
                db.query(
                    Requirement.id,
                    Requirement.requisition_id,
                    Requirement.primary_mpn,
                    Requirement.material_card_id,
                )
                .join(Requisition, Requirement.requisition_id == Requisition.id)
                .filter(
                    Requisition.status.in_(
                        [RequisitionStatus.ACTIVE, RequisitionStatus.SOURCING, RequisitionStatus.OFFERS]
                    ),
                    sa_func.upper(Requirement.primary_mpn).in_(imported_mpns_upper),
                )
                .all()
            )

        if matches:
            req_ids = [m.id for m in matches]
            req_map: dict[str, list] = {}
            for m in matches:
                req_map.setdefault((m.primary_mpn or "").upper(), []).append(m)

            existing_rows = (
                db.query(
                    Sighting.requirement_id,
                    Sighting.vendor_name_normalized,
                    Sighting.normalized_mpn,
                    Sighting.qty_available,
                    Sighting.unit_price,
                )
                .filter(
                    Sighting.requirement_id.in_(req_ids),
                    Sighting.vendor_name_normalized == norm_vendor,
                    Sighting.source_type == source_type,
                )
                .all()
            )
            existing_keys = {
                (
                    requirement_id,
                    vendor_name_normalized or "",
                    normalized_mpn or "",
                    qty_available,
                    round(float(unit_price), 6) if unit_price is not None else None,
                )
                for requirement_id, vendor_name_normalized, normalized_mpn, qty_available, unit_price in existing_rows
            }

            created_sightings = 0
            created_by_req: dict[int, list[Sighting]] = {}
            now = datetime.now(timezone.utc)
            for item in imported_for_matching:
                reqs = req_map.get(item["display_mpn"].upper(), [])
                if not reqs:
                    continue

                row = item["row"]
                qty = safe_int(row.get("qty"))
                if qty is None or qty <= 0:
                    continue  # Quality gate: only current stock lines become sightings
                price = safe_float(row.get("unit_price") or row.get("price"))
                price_norm = round(price, 6) if price and price > 0 else None

                for req in reqs:
                    dedup_key = (
                        req.id,
                        norm_vendor,
                        item["norm_key"],
                        qty,
                        price_norm,
                    )
                    if dedup_key in existing_keys:
                        continue
                    existing_keys.add(dedup_key)

                    s = Sighting(
                        requirement_id=req.id,
                        material_card_id=req.material_card_id or item["material_card_id"],
                        vendor_name=vendor_name,
                        vendor_name_normalized=norm_vendor,
                        vendor_email=vendor_email or None,
                        mpn_matched=item["display_mpn"],
                        normalized_mpn=item["norm_key"],
                        manufacturer=row.get("manufacturer"),
                        qty_available=qty,
                        unit_price=price_norm,
                        currency=(row.get("currency") or "USD"),
                        source_type=source_type,
                        confidence=0.8,
                        score=55.0,
                        condition=normalize_condition(row.get("condition")),
                        packaging=normalize_packaging(row.get("packaging")),
                        date_code=normalize_date_code(row.get("date_code")),
                        lead_time=row.get("lead_time"),
                        lead_time_days=normalize_lead_time(row.get("lead_time")),
                        source_company_id=source_company_id,
                        raw_data={
                            "filename": filename,
                            "vendor_email": vendor_email,
                            "import_method": "email_attachment",
                        },
                        created_at=now,
                    )
                    db.add(s)
                    created_by_req.setdefault(req.id, []).append(s)
                    created_sightings += 1

            if created_sightings:
                # Re-apply durable vendor+part unavailability knowledge per
                # requirement before the commit — imported stock lines must not
                # resurrect a dead vendor (listing-class rows: O2 restock check,
                # else stamp).
                from app.services.vendor_unavailability import apply_to_fresh_sightings

                req_rows = db.query(Requirement).filter(Requirement.id.in_(created_by_req)).all()
                for req_row in req_rows:
                    apply_to_fresh_sightings(db, req_row, created_by_req[req_row.id])
                db.commit()
                logger.info(
                    "Auto-created {} stock sightings from {} ({})",
                    created_sightings,
                    filename,
                    vendor_name,
                )
    except sqlalchemy.exc.SQLAlchemyError as e:
        logger.error(f"Auto-create sightings from stock list failed: {e}")
        db.rollback()
    except Exception as e:
        logger.exception(f"Auto-create sightings from stock list failed: {e}")
        db.rollback()


def _parse_stock_file(file_bytes: bytes, filename: str) -> list[dict]:
    """Parse a stock list file (CSV/XLSX) into rows with mpn, qty, price,
    manufacturer."""
    from ..file_utils import normalize_stock_row, parse_tabular_file

    raw_rows = parse_tabular_file(file_bytes, filename)
    rows = []
    for r in raw_rows:
        parsed = normalize_stock_row(r)
        if parsed:
            rows.append(parsed)
    return rows[:5000]  # Cap at 5000 rows per file
