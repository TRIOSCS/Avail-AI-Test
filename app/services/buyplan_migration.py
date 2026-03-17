"""buyplan_migration.py — Migrate V1 buy_plans rows to V3 (buy_plans_v3 +
buy_plan_lines).

Purpose: One-way data migration function that reads legacy V1 buy plan rows
         (table buy_plans) and creates corresponding BuyPlan (V3/V4) header +
         BuyPlanLine rows. Designed to be called from an Alembic migration or
         a management script.

Business Rules:
  - Idempotent: tracks migration via migrated_from_v1 flag on V3 and
    migrated_to_v3_id back-reference on V1.
  - Status mapping follows the approved V1→V3 table.
  - Each JSON line_item entry becomes one BuyPlanLine row.
  - Rejection notes are preserved in approval_notes when mapping rejected→draft.

Called by: Alembic migration (future), management scripts
Depends on: models.buy_plan (BuyPlan, BuyPlanLine), SQLAlchemy Session
"""

import json
from datetime import datetime

from loguru import logger
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.buy_plan import BuyPlan as BuyPlanV3
from app.models.buy_plan import BuyPlanLine, BuyPlanLineStatus, BuyPlanStatus

# ── Status Mapping ──────────────────────────────────────────────────

V1_TO_V3_STATUS = {
    "draft": BuyPlanStatus.draft.value,
    "pending_approval": BuyPlanStatus.pending.value,
    "approved": BuyPlanStatus.active.value,
    "po_entered": BuyPlanStatus.active.value,
    "po_confirmed": BuyPlanStatus.active.value,
    "complete": BuyPlanStatus.completed.value,
    "rejected": BuyPlanStatus.draft.value,
    "cancelled": BuyPlanStatus.cancelled.value,
}

# SQL to read all V1 buy plans
_V1_SELECT_SQL = text("""
    SELECT id, requisition_id, quote_id, status, line_items,
           manager_notes, salesperson_notes, rejection_reason,
           sales_order_number, submitted_by_id, approved_by_id,
           submitted_at, approved_at, rejected_at, completed_at,
           completed_by_id, cancelled_at, cancelled_by_id,
           cancellation_reason, approval_token, token_expires_at,
           is_stock_sale, total_cost, created_at, migrated_to_v3_id
    FROM buy_plans
    ORDER BY id
""")

# SQL to mark a V1 plan as migrated
_V1_UPDATE_SQL = text("""
    UPDATE buy_plans SET migrated_to_v3_id = :v3_id WHERE id = :v1_id
""")


def _parse_line_items(raw) -> list[dict]:
    """Safely parse line_items from JSON column (may be string, list, or None)."""
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    if isinstance(raw, list):
        return raw
    return []


def _to_datetime(val):
    """Convert a raw SQL value to a Python datetime, handling strings."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, str):
        # Handle ISO format strings (from SQLite text storage)
        for fmt in (
            "%Y-%m-%d %H:%M:%S.%f%z",
            "%Y-%m-%d %H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                return datetime.fromisoformat(val)
            except (ValueError, TypeError):
                pass
    return val


def _determine_line_status(v1_status: str, item: dict) -> str:
    """Map a V1 plan status + line item data to a V3 line status."""
    if v1_status == "cancelled":
        return BuyPlanLineStatus.cancelled.value
    po_number = item.get("po_number")
    po_verified = item.get("po_verified", False)
    if po_verified:
        return BuyPlanLineStatus.verified.value
    if po_number:
        return BuyPlanLineStatus.pending_verify.value
    return BuyPlanLineStatus.awaiting_po.value


def migrate_v1_to_v3(db: Session) -> dict:
    """Migrate v1 buy plans to v3. Returns summary stats.

    Reads from the legacy buy_plans table (V1) using raw SQL since the
    V1 ORM model has been removed. Creates V3 BuyPlan + BuyPlanLine rows
    using the current ORM models.

    Returns:
        dict with keys: migrated (int), skipped (int), errors (list[str])
    """
    stats = {"migrated": 0, "skipped": 0, "errors": []}

    rows = db.execute(_V1_SELECT_SQL).fetchall()
    logger.info("Found {} V1 buy plans to process", len(rows))

    for row in rows:
        plan = row._mapping

        # Idempotency: skip if already migrated
        if plan["migrated_to_v3_id"] is not None:
            logger.debug(
                "Skipping V1 plan {} — already migrated to V3 id {}",
                plan["id"],
                plan["migrated_to_v3_id"],
            )
            stats["skipped"] += 1
            continue

        try:
            v1_status = plan["status"]
            v3_status = V1_TO_V3_STATUS.get(v1_status, v1_status)

            # Parse line items
            items = _parse_line_items(plan["line_items"])

            # Calculate totals from line items
            total_cost = sum((it.get("cost_price") or 0) * (it.get("plan_qty") or it.get("qty") or 0) for it in items)
            total_revenue = sum(
                (it.get("sell_price") or it.get("cost_price") or 0) * (it.get("plan_qty") or it.get("qty") or 0)
                for it in items
            )

            # Build approval notes: include rejection reason when mapping rejected→draft
            approval_notes = plan["manager_notes"]
            if v1_status == "rejected" and plan["rejection_reason"]:
                prefix = f"[Rejected] {plan['rejection_reason']}"
                approval_notes = f"{prefix}\n{approval_notes}" if approval_notes else prefix

            # Create V3 header
            v3 = BuyPlanV3(
                quote_id=plan["quote_id"],
                requisition_id=plan["requisition_id"],
                status=v3_status,
                sales_order_number=plan["sales_order_number"],
                total_cost=total_cost,
                total_revenue=total_revenue,
                approved_by_id=plan["approved_by_id"],
                approved_at=_to_datetime(plan["approved_at"]),
                approval_notes=approval_notes,
                submitted_by_id=plan["submitted_by_id"],
                submitted_at=_to_datetime(plan["submitted_at"]),
                salesperson_notes=plan["salesperson_notes"],
                completed_at=_to_datetime(plan["completed_at"]),
                cancelled_at=_to_datetime(plan["cancelled_at"]),
                cancelled_by_id=plan["cancelled_by_id"],
                cancellation_reason=plan["cancellation_reason"],
                approval_token=plan["approval_token"],
                token_expires_at=_to_datetime(plan["token_expires_at"]),
                is_stock_sale=plan["is_stock_sale"] or False,
                created_at=_to_datetime(plan["created_at"]),
                migrated_from_v1=True,
            )
            db.add(v3)
            db.flush()  # Get v3.id

            # Create lines from JSON line_items
            for item in items:
                line_status = _determine_line_status(v1_status, item)

                line = BuyPlanLine(
                    buy_plan_id=v3.id,
                    offer_id=item.get("offer_id"),
                    quantity=item.get("plan_qty") or item.get("qty") or 0,
                    unit_cost=item.get("cost_price"),
                    unit_sell=item.get("sell_price"),
                    buyer_id=item.get("entered_by_id"),
                    status=line_status,
                    po_number=item.get("po_number"),
                    po_confirmed_at=_to_datetime(item.get("po_sent_at")),
                    created_at=_to_datetime(plan["created_at"]),
                )
                db.add(line)

            # Mark V1 plan as migrated
            db.execute(_V1_UPDATE_SQL, {"v3_id": v3.id, "v1_id": plan["id"]})
            stats["migrated"] += 1
            logger.info("Migrated V1 plan {} → V3 plan {}", plan["id"], v3.id)

        except Exception as exc:
            db.rollback()
            error_msg = f"Error migrating V1 plan {plan['id']}: {exc}"
            logger.error(error_msg)
            stats["errors"].append(error_msg)

    db.flush()
    logger.info(
        "Migration complete: {} migrated, {} skipped, {} errors",
        stats["migrated"],
        stats["skipped"],
        len(stats["errors"]),
    )
    return stats
