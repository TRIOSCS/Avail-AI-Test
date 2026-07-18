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

from sqlalchemy import select
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

# The SO pane's writable QP-sales columns (spec §4 SALES section — the workbook
# fields that live on the sales order). Anything else posted is ignored.
QP_SALES_FIELDS: tuple[str, ...] = (
    "sales_condition",
    "sales_quantity",
    "sales_fw_hw_rev",
    "sales_product_commodity",
    "sales_testing_required",
    "sales_testing_option",
    "sales_testing_specifics",
    "sales_test_location",
    "sales_serial_preapproval_required",
    "sales_authorized_ship_early",
    "sales_authorized_ship_partial",
    "sales_routing_prescreening_whs",
    "sales_third_party_pkg_ok",
    "sales_pkg_requirements",
    "sales_bom_matrix_links",
    "sales_notes",
)

_BOOL_FIELDS = frozenset(
    {
        "purchasing_testing_required",
        "purchasing_tpo_ship_complete",
        "purchasing_traceability_verified",
        "purchasing_coc_available",
        "purchasing_sn_previously_received",
        "sales_testing_required",
        "sales_serial_preapproval_required",
        "sales_authorized_ship_early",
        "sales_authorized_ship_partial",
        "sales_third_party_pkg_ok",
    }
)

_INT_FIELDS = frozenset({"sales_quantity"})


def _coerce(field: str, value: Any) -> Any:
    """Normalize one raw form value: booleans from explicit yes/no strings; whole
    numbers for the int fields (non-numeric → ValueError); ''→None."""
    if field in _BOOL_FIELDS:
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        if text in ("yes", "true", "1", "on"):
            return True
        if text in ("no", "false", "0"):
            return False
        return None  # unanswered
    if field in _INT_FIELDS:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return int(text)
        except (TypeError, ValueError) as e:
            raise ValueError(f"{field} must be a whole number.") from e
    if isinstance(value, str):
        value = value.strip()
    return value or None


def _collect_updates(allowed: tuple[str, ...], fields: dict[str, Any]) -> dict[str, Any]:
    """Coerce + filter a form's raw values down to the writable updates dict.

    Shared by :func:`apply_qp_purchasing` / :func:`apply_qp_sales`. A field is skipped
    (existing answer left untouched) when it is absent, when it coerces to None from a
    blank (unanswered), or — for boolean fields — when a NON-empty value fails to parse
    as an explicit yes/no (a forged/garbage value must never null out a stored answer).
    """
    updates: dict[str, Any] = {}
    for field in allowed:
        if field not in fields:
            continue
        value = _coerce(field, fields[field])
        if value is None:
            raw = str(fields[field] or "").strip()
            if raw == "":
                continue  # unanswered blank — never null out an existing answer
            if field in _BOOL_FIELDS:
                continue  # unparseable bool garbage — never null out an existing answer
        updates[field] = value
    return updates


def qp_for_line(db: Session, plan: BuyPlan, line: BuyPlanLine) -> QualityPlan | None:
    """The plan's QualityPlan row for *line*'s vendor — EXACTLY the row
    :func:`apply_qp_purchasing` will write (or None when it would create a fresh one).

    Never falls back to another vendor's row: a vendor-B confirm-PO form must prefill
    empty, not with vendor A's answers (which the save would then silently copy into a
    fresh vendor-B row via diff-against-empty). Read-only lookup.
    """
    vendor_card_id = line.offer.vendor_card_id if line.offer is not None else None
    stmt = select(QualityPlan).where(QualityPlan.buy_plan_id == plan.id)
    if vendor_card_id is not None:
        return db.scalars(stmt.where(QualityPlan.vendor_card_id == vendor_card_id).order_by(QualityPlan.id)).first()
    return db.scalars(stmt.order_by(QualityPlan.id)).first()


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
    updates = _collect_updates(QP_PURCHASING_FIELDS, fields)

    vendor_card_id = line.offer.vendor_card_id if line.offer is not None else None
    qp = None
    stmt = select(QualityPlan).where(QualityPlan.buy_plan_id == plan.id)
    if vendor_card_id is not None:
        qp = db.scalars(stmt.where(QualityPlan.vendor_card_id == vendor_card_id).order_by(QualityPlan.id)).first()
    if qp is None and vendor_card_id is None:
        qp = db.scalars(stmt.order_by(QualityPlan.id)).first()
    if qp is None:
        qp = QualityPlan(buy_plan_id=plan.id, vendor_card_id=vendor_card_id, created_by_id=user.id)
        db.add(qp)
        db.flush()

    edits = diff_fields(qp, updates)
    for edit in edits:
        setattr(qp, edit.field, updates[edit.field])
    db.flush()
    return qp, edits


def can_edit_qp_sales(user: User, plan: BuyPlan) -> bool:
    """Whether *user* may edit the plan's QP-sales answers NOW (spec §7 matrix).

    draft → the owning salesperson OR a manager/admin; pending → MANAGER/ADMIN ONLY
    (sales keeps notes while pending, not fields); everything else → locked (active+
    header is locked; line changes go through the PO stage). Enforced server-side by the
    qp-sales route — the pane hides the editor with the SAME predicate.
    """
    from ..constants import BuyPlanStatus, UserRole

    is_manager = user.role in (UserRole.MANAGER, UserRole.ADMIN)
    if plan.status == BuyPlanStatus.DRAFT.value:
        req = plan.requisition
        return is_manager or bool(req and req.created_by == user.id)
    if plan.status == BuyPlanStatus.PENDING.value:
        return is_manager
    return False


def qp_sales_row(db: Session, plan: BuyPlan) -> QualityPlan | None:
    """The plan's FIRST QualityPlan row — the SALES answers are plan-level (D11: QP rows
    stay keyed per (plan, vendor); the first row carries the sales section).

    Read-only lookup; :func:`apply_qp_sales` find-or-creates on write.
    """
    return db.scalars(select(QualityPlan).where(QualityPlan.buy_plan_id == plan.id).order_by(QualityPlan.id)).first()


def apply_qp_sales(
    db: Session,
    *,
    plan: BuyPlan,
    user: User,
    fields: dict[str, Any],
) -> tuple[QualityPlan, list[FieldEdit]]:
    """Apply the SO pane's QP-sales answers to the plan's first QualityPlan row.

    Mirrors :func:`apply_qp_purchasing`: only whitelisted ``sales_*`` fields apply
    (booleans from explicit yes/no, sales_quantity a whole number, blanks leave the
    column untouched); returns ``(qp, edits)`` — the caller logs the diff and owns the
    commit. Permission is the ROUTE's job (:func:`can_edit_qp_sales`); a save that
    changes nothing returns an empty diff and writes nothing.
    """
    updates = _collect_updates(QP_SALES_FIELDS, fields)

    qp = qp_sales_row(db, plan)
    if qp is None:
        qp = QualityPlan(buy_plan_id=plan.id, created_by_id=user.id)
        db.add(qp)
        db.flush()

    edits = diff_fields(qp, updates)
    for edit in edits:
        setattr(qp, edit.field, updates[edit.field])
    db.flush()
    return qp, edits
