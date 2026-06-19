"""buyplan_hub.py — Buy Plan Deal Hub read models.

Purpose: Role-aware read models for the Buy Plan Deal Hub page.
         Provides the buyer's per-line PO queue (lines the buyer must cut a PO for)
         across all their active deals.

Called by: routers/htmx_views.py (buy plan hub partials)
Depends on: models.buy_plan (BuyPlan, BuyPlanLine), models.auth (User),
            constants (BuyPlanStatus, BuyPlanLineStatus)
"""

from __future__ import annotations

from sqlalchemy.orm import Session, joinedload

from ..constants import BuyPlanLineStatus, BuyPlanStatus
from ..models.buy_plan import BuyPlan, BuyPlanLine


def buyer_line_queue(db: Session, user: object) -> list[dict]:
    """Return one dict per actionable buy-plan line assigned to ``user``.

    "Actionable" means:
    - ``BuyPlanLine.buyer_id == user.id``
    - ``BuyPlanLine.status == AWAITING_PO``
    - parent ``BuyPlan.status == ACTIVE``

    Rows are sorted kicked-back first (``po_rejection_note is not None``), then
    by ``plan.created_at`` ascending so the oldest deal surfaces first.

    Each dict contains:
        line_id, plan_id, customer_name, mpn, description, vendor_name,
        vendor_contact_email, quantity, unit_cost, status, kicked_back,
        po_rejection_note, plan_created_at
    """
    lines = (
        db.query(BuyPlanLine)
        .join(BuyPlan, BuyPlanLine.buy_plan_id == BuyPlan.id)
        .filter(
            BuyPlanLine.buyer_id == user.id,
            BuyPlanLine.status == BuyPlanLineStatus.AWAITING_PO,
            BuyPlan.status == BuyPlanStatus.ACTIVE,
        )
        .options(
            joinedload(BuyPlanLine.buy_plan).joinedload(BuyPlan.quote),
            joinedload(BuyPlanLine.requirement),
            joinedload(BuyPlanLine.offer),
        )
        .all()
    )

    # Sort: kicked-back rows first, then oldest plan first
    lines.sort(key=lambda ln: (ln.po_rejection_note is None, ln.buy_plan.created_at))

    rows = []
    for ln in lines:
        plan = ln.buy_plan

        # Derive customer_name: plan.quote → customer_site → company.name, with site_name fallback
        # Mirrors buy_plans_list_partial in routers/htmx_views.py
        customer_name = None
        if plan.quote and plan.quote.customer_site:
            site = plan.quote.customer_site
            co = site.company if hasattr(site, "company") else None
            customer_name = co.name if co else getattr(site, "site_name", None)

        req = ln.requirement
        offer = ln.offer

        rows.append(
            {
                "line_id": ln.id,
                "plan_id": plan.id,
                "customer_name": customer_name,
                "mpn": req.primary_mpn if req else None,
                "description": req.description if req else None,
                "vendor_name": offer.vendor_name if offer else None,
                # Offer has no direct contact-email column; None until enriched
                "vendor_contact_email": None,
                "quantity": ln.quantity,
                "unit_cost": ln.unit_cost,
                "status": ln.status,
                "kicked_back": ln.po_rejection_note is not None,
                "po_rejection_note": ln.po_rejection_note,
                "plan_created_at": plan.created_at,
            }
        )

    return rows
