"""Migrate V1 buy_plans data to V3 buy_plans_v3 + buy_plan_lines.

Reads each V1 BuyPlan record, creates a V3 header and one BuyPlanLine
per JSON line_item entry. Idempotent — skips plans already migrated
(detected by matching quote_id + requisition_id + submitted_at).

Revision ID: 076
Revises: 075
Create Date: 2026-03-13
"""

import json

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision = "076"
down_revision = "075"
branch_labels = None
depends_on = None

# V1 → V3 status mapping
_STATUS_MAP = {
    "draft": "draft",
    "pending_approval": "pending",
    "approved": "active",
    "po_entered": "active",
    "po_confirmed": "active",
    "complete": "completed",
    "rejected": "draft",
    "cancelled": "cancelled",
}


def upgrade():
    conn = op.get_bind()

    # Read all V1 buy plans
    v1_plans = conn.execute(
        text("""
            SELECT id, requisition_id, quote_id, status, line_items,
                   manager_notes, salesperson_notes, rejection_reason,
                   sales_order_number, submitted_by_id, approved_by_id,
                   submitted_at, approved_at, rejected_at, completed_at,
                   completed_by_id, cancelled_at, cancelled_by_id,
                   cancellation_reason, approval_token, token_expires_at,
                   is_stock_sale, created_at
            FROM buy_plans
            ORDER BY id
        """)
    ).fetchall()

    for plan in v1_plans:
        # Idempotency check: skip if V3 plan already exists for same quote+req+submitted_at
        existing = conn.execute(
            text("""
                SELECT id FROM buy_plans_v3
                WHERE quote_id = :qid AND requisition_id = :rid
                  AND submitted_at IS NOT DISTINCT FROM :sat
            """),
            {"qid": plan.quote_id, "rid": plan.requisition_id, "sat": plan.submitted_at},
        ).fetchone()
        if existing:
            continue

        v3_status = _STATUS_MAP.get(plan.status, plan.status)

        # Parse line_items JSON
        line_items = plan.line_items
        if isinstance(line_items, str):
            try:
                line_items = json.loads(line_items)
            except (json.JSONDecodeError, TypeError):
                line_items = []
        if not line_items:
            line_items = []

        # Calculate totals from line items
        total_cost = sum(
            (item.get("cost_price") or 0) * (item.get("plan_qty") or item.get("qty") or 0)
            for item in line_items
        )
        total_revenue = sum(
            (item.get("sell_price") or item.get("cost_price") or 0) * (item.get("plan_qty") or item.get("qty") or 0)
            for item in line_items
        )

        # Insert V3 header
        result = conn.execute(
            text("""
                INSERT INTO buy_plans_v3 (
                    quote_id, requisition_id, status, sales_order_number,
                    total_cost, total_revenue,
                    approved_by_id, approved_at, approval_notes,
                    submitted_by_id, submitted_at, salesperson_notes,
                    completed_at, cancelled_at, cancelled_by_id,
                    cancellation_reason, approval_token, token_expires_at,
                    is_stock_sale, created_at, updated_at
                ) VALUES (
                    :quote_id, :requisition_id, :status, :sales_order_number,
                    :total_cost, :total_revenue,
                    :approved_by_id, :approved_at, :approval_notes,
                    :submitted_by_id, :submitted_at, :salesperson_notes,
                    :completed_at, :cancelled_at, :cancelled_by_id,
                    :cancellation_reason, :approval_token, :token_expires_at,
                    :is_stock_sale, :created_at, :updated_at
                )
                RETURNING id
            """),
            {
                "quote_id": plan.quote_id,
                "requisition_id": plan.requisition_id,
                "status": v3_status,
                "sales_order_number": plan.sales_order_number,
                "total_cost": total_cost,
                "total_revenue": total_revenue,
                "approved_by_id": plan.approved_by_id,
                "approved_at": plan.approved_at,
                "approval_notes": plan.manager_notes,
                "submitted_by_id": plan.submitted_by_id,
                "submitted_at": plan.submitted_at,
                "salesperson_notes": plan.salesperson_notes,
                "completed_at": plan.completed_at,
                "cancelled_at": plan.cancelled_at,
                "cancelled_by_id": plan.cancelled_by_id,
                "cancellation_reason": plan.cancellation_reason or plan.rejection_reason,
                "approval_token": plan.approval_token,
                "token_expires_at": plan.token_expires_at,
                "is_stock_sale": plan.is_stock_sale or False,
                "created_at": plan.created_at,
                "updated_at": plan.created_at,
            },
        )
        v3_id = result.fetchone()[0]

        # Insert V3 lines from JSON line_items
        for item in line_items:
            po_number = item.get("po_number")
            po_verified = item.get("po_verified", False)

            # Determine line status
            if plan.status == "cancelled":
                line_status = "cancelled"
            elif po_verified:
                line_status = "verified"
            elif po_number:
                line_status = "pending_verify"
            else:
                line_status = "awaiting_po"

            # Try to find requirement_id via offer
            offer_id = item.get("offer_id")
            requirement_id = None
            if offer_id:
                req_row = conn.execute(
                    text("SELECT requisition_id FROM offers WHERE id = :oid"),
                    {"oid": offer_id},
                ).fetchone()
                if req_row:
                    # Find matching requirement
                    mpn = item.get("mpn")
                    if mpn:
                        req_match = conn.execute(
                            text("""
                                SELECT id FROM requirements
                                WHERE requisition_id = :rid AND primary_mpn = :mpn
                                LIMIT 1
                            """),
                            {"rid": plan.requisition_id, "mpn": mpn},
                        ).fetchone()
                        if req_match:
                            requirement_id = req_match[0]

            conn.execute(
                text("""
                    INSERT INTO buy_plan_lines (
                        buy_plan_id, requirement_id, offer_id, quantity,
                        unit_cost, unit_sell, buyer_id, status,
                        po_number, po_confirmed_at, created_at, updated_at
                    ) VALUES (
                        :buy_plan_id, :requirement_id, :offer_id, :quantity,
                        :unit_cost, :unit_sell, :buyer_id, :status,
                        :po_number, :po_confirmed_at, :created_at, :updated_at
                    )
                """),
                {
                    "buy_plan_id": v3_id,
                    "requirement_id": requirement_id,
                    "offer_id": offer_id,
                    "quantity": item.get("plan_qty") or item.get("qty") or 0,
                    "unit_cost": item.get("cost_price"),
                    "unit_sell": item.get("sell_price"),
                    "buyer_id": item.get("entered_by_id"),
                    "status": line_status,
                    "po_number": po_number,
                    "po_confirmed_at": item.get("po_sent_at"),
                    "created_at": plan.created_at,
                    "updated_at": plan.created_at,
                },
            )


def downgrade():
    """Remove migrated V3 records. V1 buy_plans table is untouched."""
    conn = op.get_bind()
    # Delete lines first (FK cascade would handle this, but be explicit)
    conn.execute(
        text("""
            DELETE FROM buy_plan_lines
            WHERE buy_plan_id IN (
                SELECT v3.id FROM buy_plans_v3 v3
                INNER JOIN buy_plans v1
                    ON v1.quote_id = v3.quote_id
                   AND v1.requisition_id = v3.requisition_id
                   AND v1.submitted_at IS NOT DISTINCT FROM v3.submitted_at
            )
        """)
    )
    # Delete V3 headers that were migrated from V1
    conn.execute(
        text("""
            DELETE FROM buy_plans_v3
            WHERE id IN (
                SELECT v3.id FROM buy_plans_v3 v3
                INNER JOIN buy_plans v1
                    ON v1.quote_id = v3.quote_id
                   AND v1.requisition_id = v3.requisition_id
                   AND v1.submitted_at IS NOT DISTINCT FROM v3.submitted_at
            )
        """)
    )
