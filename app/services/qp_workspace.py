"""qp_workspace.py — Quality-plan writes for the Approvals Workspace panes.

Purpose: apply_qp_purchasing folds the PO pane's QP-purchasing answers (incl. the
         AS9120B counterfeit-avoidance fields from migration 192) onto the plan's
         QualityPlan row for the line's VENDOR — QP rows stay keyed per
         (buy_plan, vendor_card) (design D11); the row is found-or-created here.
         Only whitelisted purchasing_* columns are writable; boolean answers arrive
         as explicit yes/no strings ('' = unanswered → untouched). Returns the QP
         plus the FieldEdit diff so the calling route can audit the save
         (field_audit.log_field_edits) without re-diffing.

Called by: routers/htmx/buy_plans.py (confirm-po route), Phase 2 QP-sales route.
Depends on: app.models.quality_plan (QualityPlan), app.models.buy_plan,
            app.services.field_audit (diff_fields).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..models.auth import User
from ..models.buy_plan import BuyPlan, BuyPlanLine
from ..models.quality_plan import QualityPlan
from .field_audit import FieldEdit, diff_fields

# The PO pane's writable QP-purchasing columns (spec §4 PURCHASING section — the
# workbook fields plus the AS9120B additions). Anything else posted is ignored.
QP_PURCHASING_FIELDS: tuple[str, ...] = (
    "purchasing_po_number",
    "purchasing_condition",
    "purchasing_fw_hw_rev",
    "purchasing_product_commodity",
    "purchasing_testing_required",
    "purchasing_testing_option",
    "purchasing_routing_prescreening_whs",
    "purchasing_packaging",
    "purchasing_tpo_ship_complete",
    "purchasing_tpo_notes",
    "purchasing_traceability_verified",
    "purchasing_counterfeit_risk",
    "purchasing_risk_level",
    "purchasing_coc_available",
    "purchasing_vendor_rating",
    "purchasing_sn_previously_received",
    "purchasing_serial_numbers",
)

_BOOL_FIELDS = frozenset(
    {
        "purchasing_testing_required",
        "purchasing_tpo_ship_complete",
        "purchasing_traceability_verified",
        "purchasing_coc_available",
        "purchasing_sn_previously_received",
    }
)


def _coerce(field: str, value: Any) -> Any:
    """Normalize one raw form value: booleans from explicit yes/no strings; ''→None."""
    if field in _BOOL_FIELDS:
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        if text in ("yes", "true", "1", "on"):
            return True
        if text in ("no", "false", "0"):
            return False
        return None  # unanswered
    if isinstance(value, str):
        value = value.strip()
    return value or None


def qp_for_line(db: Session, plan: BuyPlan, line: BuyPlanLine) -> QualityPlan | None:
    """The plan's QualityPlan row for *line*'s vendor (or the plan's first row when the
    line has no vendor card / no vendor-specific row exists). Read-only lookup."""
    vendor_card_id = line.offer.vendor_card_id if line.offer is not None else None
    query = db.query(QualityPlan).filter(QualityPlan.buy_plan_id == plan.id)
    if vendor_card_id is not None:
        vendor_qp = query.filter(QualityPlan.vendor_card_id == vendor_card_id).order_by(QualityPlan.id).first()
        if vendor_qp is not None:
            return vendor_qp
    return query.order_by(QualityPlan.id).first()


def apply_qp_purchasing(
    db: Session,
    *,
    plan: BuyPlan,
    line: BuyPlanLine,
    user: User,
    fields: dict[str, Any],
) -> tuple[QualityPlan, list[FieldEdit]]:
    """Apply the confirm-PO form's QP-purchasing answers to the line's vendor QP row.

    Finds (or creates) the QualityPlan for (plan, line's vendor_card) per D11, applies
    only whitelisted ``purchasing_*`` fields (booleans from explicit yes/no; blanks
    leave the column untouched), and returns ``(qp, edits)`` where *edits* is the
    field-audit diff of what actually changed — the caller logs it and owns the
    flush/commit. A save that changes nothing returns an empty diff and writes nothing.
    """
    updates: dict[str, Any] = {}
    for field in QP_PURCHASING_FIELDS:
        if field not in fields:
            continue
        value = _coerce(field, fields[field])
        if value is None and str(fields[field] or "").strip() == "":
            continue  # unanswered blank — never null out an existing answer
        updates[field] = value

    vendor_card_id = line.offer.vendor_card_id if line.offer is not None else None
    qp = None
    query = db.query(QualityPlan).filter(QualityPlan.buy_plan_id == plan.id)
    if vendor_card_id is not None:
        qp = query.filter(QualityPlan.vendor_card_id == vendor_card_id).order_by(QualityPlan.id).first()
    if qp is None and vendor_card_id is None:
        qp = query.order_by(QualityPlan.id).first()
    if qp is None:
        qp = QualityPlan(buy_plan_id=plan.id, vendor_card_id=vendor_card_id, created_by_id=user.id)
        db.add(qp)
        db.flush()

    edits = diff_fields(qp, updates)
    for edit in edits:
        setattr(qp, edit.field, updates[edit.field])
    db.flush()
    return qp, edits
