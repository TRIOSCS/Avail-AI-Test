"""Buy Plan V3 — Workflow: submit, approve, verify, complete, intelligence.

Phase 4: Approval + Execution — submit, approve, verify SO/PO, flag issues,
         auto-complete, favoritism detection, case reports.

Called by: routers/buy_plans_v3.py
Depends on: buyplan_scoring, buyplan_builder, models, config
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload

from ..config import settings
from ..models import (
    Offer,
    Quote,
    Requirement,
    User,
    VendorCard,
)
from ..models.buy_plan import (
    BuyPlanLine,
    BuyPlanLineStatus,
    BuyPlanStatus,
    BuyPlanV3,
    SOVerificationStatus,
    VerificationGroupMember,
)
from .buyplan_scoring import assign_buyer, score_offer


# ── Workflow: Submit ─────────────────────────────────────────────────


def submit_buy_plan(
    plan_id: int,
    sales_order_number: str,
    user: User,
    db: Session,
    *,
    customer_po_number: str | None = None,
    line_edits: list[dict] | None = None,
    salesperson_notes: str | None = None,
) -> BuyPlanV3:
    """Submit a draft buy plan with SO# and optional line edits.

    Flow: draft → pending (needs manager) OR draft → active (auto-approved).
    Auto-approve when total cost < threshold AND no critical AI flags.
    """
    plan = db.get(BuyPlanV3, plan_id, options=[joinedload(BuyPlanV3.lines)])
    if not plan:
        raise ValueError(f"Buy plan {plan_id} not found")
    if plan.status != BuyPlanStatus.draft.value:
        raise ValueError(f"Can only submit draft plans (current: {plan.status})")

    plan.sales_order_number = sales_order_number
    plan.customer_po_number = customer_po_number
    plan.submitted_by_id = user.id
    plan.submitted_at = datetime.now(timezone.utc)
    plan.salesperson_notes = salesperson_notes

    if line_edits:
        _apply_line_edits(plan, line_edits, db)

    plan.is_stock_sale = _is_stock_sale(plan, db)

    # Auto-approve decision
    total = float(plan.total_cost or 0)
    has_critical = any(
        (f.get("severity") if isinstance(f, dict) else getattr(f, "severity", None)) == "critical"
        for f in (plan.ai_flags or [])
    )
    if total < settings.buyplan_auto_approve_threshold and not has_critical:
        plan.status = BuyPlanStatus.active.value
        plan.auto_approved = True
        plan.approved_at = datetime.now(timezone.utc)
        logger.info("Buy plan %d auto-approved (cost=%.2f)", plan_id, total)
    else:
        plan.status = BuyPlanStatus.pending.value
        logger.info(
            "Buy plan %d pending approval (cost=%.2f, critical=%s)",
            plan_id,
            total,
            has_critical,
        )

    db.flush()
    return plan


# ── Workflow: Approval ───────────────────────────────────────────────


def approve_buy_plan(
    plan_id: int,
    action: str,
    user: User,
    db: Session,
    *,
    line_overrides: list[dict] | None = None,
    notes: str | None = None,
) -> BuyPlanV3:
    """Manager approves or rejects a pending buy plan.

    Approve → active (lines go to buyers). Reject → draft (back to salesperson).
    Line overrides let manager swap vendors on specific lines.
    """
    plan = db.get(BuyPlanV3, plan_id, options=[joinedload(BuyPlanV3.lines)])
    if not plan:
        raise ValueError(f"Buy plan {plan_id} not found")
    if plan.status != BuyPlanStatus.pending.value:
        raise ValueError(f"Can only approve/reject pending plans (current: {plan.status})")

    now = datetime.now(timezone.utc)
    if action == "approve":
        if line_overrides:
            _apply_line_overrides(plan, line_overrides, db)
        plan.status = BuyPlanStatus.active.value
        plan.approved_by_id = user.id
        plan.approved_at = now
        plan.approval_notes = notes
        logger.info("Buy plan %d approved by %s", plan_id, user.email)
    elif action == "reject":
        plan.status = BuyPlanStatus.draft.value
        plan.approval_notes = notes
        logger.info("Buy plan %d rejected by %s: %s", plan_id, user.email, notes)
    else:
        raise ValueError(f"Invalid action: {action}")

    db.flush()
    return plan


# ── Workflow: SO Verification ────────────────────────────────────────


def verify_so(
    plan_id: int,
    action: str,
    user: User,
    db: Session,
    *,
    rejection_note: str | None = None,
) -> BuyPlanV3:
    """Ops verifies (or rejects/halts) the Sales Order in Acctivate.

    Approve → so_status=approved. Reject → so_status=rejected.
    Halt → plan.status=halted (stops everything).
    """
    plan = db.get(BuyPlanV3, plan_id)
    if not plan:
        raise ValueError(f"Buy plan {plan_id} not found")
    if plan.so_status != SOVerificationStatus.pending.value:
        raise ValueError(f"SO already verified (status: {plan.so_status})")
    if plan.status == BuyPlanStatus.halted.value:
        raise ValueError("Plan is halted")

    member = db.query(VerificationGroupMember).filter_by(user_id=user.id, is_active=True).first()
    if not member:
        raise PermissionError("User is not in the ops verification group")

    now = datetime.now(timezone.utc)
    plan.so_verified_by_id = user.id
    plan.so_verified_at = now

    if action == "approve":
        plan.so_status = SOVerificationStatus.approved.value
        logger.info("SO verified for plan %d by %s", plan_id, user.email)
    elif action == "reject":
        plan.so_status = SOVerificationStatus.rejected.value
        plan.so_rejection_note = rejection_note
        logger.info("SO rejected for plan %d: %s", plan_id, rejection_note)
    elif action == "halt":
        plan.so_status = SOVerificationStatus.rejected.value
        plan.so_rejection_note = rejection_note
        plan.status = BuyPlanStatus.halted.value
        plan.halted_by_id = user.id
        plan.halted_at = now
        logger.info("Plan %d HALTED by %s: %s", plan_id, user.email, rejection_note)
    else:
        raise ValueError(f"Invalid SO verification action: {action}")

    db.flush()
    return plan


# ── Workflow: PO Execution ───────────────────────────────────────────


def confirm_po(
    plan_id: int,
    line_id: int,
    po_number: str,
    estimated_ship_date: datetime,
    user: User,
    db: Session,
) -> BuyPlanLine:
    """Buyer confirms PO was cut for a line in Acctivate.

    Line status: awaiting_po → pending_verify.
    """
    plan = db.get(BuyPlanV3, plan_id)
    if not plan:
        raise ValueError(f"Buy plan {plan_id} not found")
    if plan.status != BuyPlanStatus.active.value:
        raise ValueError(f"Plan must be active (current: {plan.status})")

    line = db.get(BuyPlanLine, line_id)
    if not line or line.buy_plan_id != plan_id:
        raise ValueError(f"Line {line_id} not found in plan {plan_id}")
    if line.status != BuyPlanLineStatus.awaiting_po.value:
        raise ValueError(f"Line must be awaiting PO (current: {line.status})")

    line.po_number = po_number
    line.estimated_ship_date = estimated_ship_date
    line.po_confirmed_at = datetime.now(timezone.utc)
    line.status = BuyPlanLineStatus.pending_verify.value
    logger.info("PO %s confirmed for line %d (plan %d)", po_number, line_id, plan_id)

    db.flush()
    return line


def verify_po(
    plan_id: int,
    line_id: int,
    action: str,
    user: User,
    db: Session,
    *,
    rejection_note: str | None = None,
) -> BuyPlanLine:
    """Ops verifies a PO was properly entered.

    Approve → line verified. Reject → back to awaiting_po.
    After approval, checks if all lines are done → auto-complete.
    """
    plan = db.get(BuyPlanV3, plan_id)
    if not plan:
        raise ValueError(f"Buy plan {plan_id} not found")

    line = db.get(BuyPlanLine, line_id)
    if not line or line.buy_plan_id != plan_id:
        raise ValueError(f"Line {line_id} not found in plan {plan_id}")
    if line.status != BuyPlanLineStatus.pending_verify.value:
        raise ValueError(f"Line must be pending verification (current: {line.status})")

    member = db.query(VerificationGroupMember).filter_by(user_id=user.id, is_active=True).first()
    if not member:
        raise PermissionError("User is not in the ops verification group")

    now = datetime.now(timezone.utc)
    if action == "approve":
        line.status = BuyPlanLineStatus.verified.value
        line.po_verified_by_id = user.id
        line.po_verified_at = now
        logger.info("PO verified for line %d (plan %d)", line_id, plan_id)
        check_completion(plan_id, db)
    elif action == "reject":
        line.status = BuyPlanLineStatus.awaiting_po.value
        line.po_rejection_note = rejection_note
        line.po_number = None
        line.estimated_ship_date = None
        line.po_confirmed_at = None
        logger.info("PO rejected for line %d: %s", line_id, rejection_note)
    else:
        raise ValueError(f"Invalid PO verification action: {action}")

    db.flush()
    return line


# ── Workflow: Issue Flagging ─────────────────────────────────────────


def flag_line_issue(
    plan_id: int,
    line_id: int,
    issue_type: str,
    user: User,
    db: Session,
    *,
    note: str | None = None,
) -> BuyPlanLine:
    """Buyer flags an issue on a line (sold out, price change, etc.).

    Line status → issue. Manager/salesperson needs to resolve.
    """
    plan = db.get(BuyPlanV3, plan_id)
    if not plan:
        raise ValueError(f"Buy plan {plan_id} not found")
    if plan.status != BuyPlanStatus.active.value:
        raise ValueError(f"Plan must be active (current: {plan.status})")

    line = db.get(BuyPlanLine, line_id)
    if not line or line.buy_plan_id != plan_id:
        raise ValueError(f"Line {line_id} not found in plan {plan_id}")

    flaggable = {BuyPlanLineStatus.awaiting_po.value, BuyPlanLineStatus.pending_verify.value}
    if line.status not in flaggable:
        raise ValueError(f"Cannot flag issue on line with status: {line.status}")

    line.status = BuyPlanLineStatus.issue.value
    line.issue_type = issue_type
    line.issue_note = note
    logger.info("Issue '%s' flagged on line %d (plan %d)", issue_type, line_id, plan_id)

    db.flush()
    return line


# ── Workflow: Completion ─────────────────────────────────────────────


def check_completion(plan_id: int, db: Session) -> BuyPlanV3:
    """Auto-complete the buy plan if all lines are in terminal state.

    Completion requires:
    - Plan is active
    - All lines are verified or cancelled
    - SO is verified (so_status = approved)
    """
    plan = db.get(BuyPlanV3, plan_id, options=[joinedload(BuyPlanV3.lines)])
    if not plan or plan.status != BuyPlanStatus.active.value:
        return plan

    if not plan.lines:
        return plan

    terminal = {BuyPlanLineStatus.verified.value, BuyPlanLineStatus.cancelled.value}
    all_terminal = all(line.status in terminal for line in plan.lines)

    if all_terminal and plan.so_status == SOVerificationStatus.approved.value:
        plan.status = BuyPlanStatus.completed.value
        plan.completed_at = datetime.now(timezone.utc)
        plan.case_report = generate_case_report(plan, db)
        logger.info("Buy plan %d auto-completed (all lines terminal)", plan_id)
        db.flush()

    return plan


def resubmit_buy_plan(
    plan_id: int,
    sales_order_number: str,
    user: User,
    db: Session,
    *,
    customer_po_number: str | None = None,
    salesperson_notes: str | None = None,
) -> BuyPlanV3:
    """Resubmit a rejected buy plan. Resets SO verification and approval.

    Used after manager rejection (plan back in draft).
    """
    plan = db.get(BuyPlanV3, plan_id, options=[joinedload(BuyPlanV3.lines)])
    if not plan:
        raise ValueError(f"Buy plan {plan_id} not found")
    if plan.status != BuyPlanStatus.draft.value:
        raise ValueError(f"Can only resubmit draft plans (current: {plan.status})")

    # Reset SO verification
    plan.so_status = SOVerificationStatus.pending.value
    plan.so_verified_by_id = None
    plan.so_verified_at = None
    plan.so_rejection_note = None

    # Reset approval
    plan.auto_approved = False
    plan.approved_by_id = None
    plan.approved_at = None
    plan.approval_notes = None

    # Update references
    plan.sales_order_number = sales_order_number
    plan.customer_po_number = customer_po_number
    plan.submitted_by_id = user.id
    plan.submitted_at = datetime.now(timezone.utc)
    plan.salesperson_notes = salesperson_notes

    # Auto-approve decision (same logic as initial submit)
    total = float(plan.total_cost or 0)
    has_critical = any(
        (f.get("severity") if isinstance(f, dict) else getattr(f, "severity", None)) == "critical"
        for f in (plan.ai_flags or [])
    )
    if total < settings.buyplan_auto_approve_threshold and not has_critical:
        plan.status = BuyPlanStatus.active.value
        plan.auto_approved = True
        plan.approved_at = datetime.now(timezone.utc)
    else:
        plan.status = BuyPlanStatus.pending.value

    db.flush()
    return plan


# ── Helpers: Line Edits ──────────────────────────────────────────────


def _apply_line_edits(plan: BuyPlanV3, edits: list[dict], db: Session):
    """Replace AI-generated lines with salesperson's vendor swaps/splits."""
    edits_by_req: dict[int, list[dict]] = {}
    for edit in edits:
        edits_by_req.setdefault(edit["requirement_id"], []).append(edit)

    affected = set(edits_by_req.keys())
    to_remove = [ln for ln in plan.lines if ln.requirement_id in affected]
    for line in to_remove:
        plan.lines.remove(line)

    for req_id, req_edits in edits_by_req.items():
        requirement = db.get(Requirement, req_id)
        for edit in req_edits:
            offer = db.get(Offer, edit["offer_id"])
            if not offer:
                raise ValueError(f"Offer {edit['offer_id']} not found")

            unit_cost = float(offer.unit_price) if offer.unit_price else None
            unit_sell = float(requirement.target_price) if requirement and requirement.target_price else None
            margin_pct = None
            if unit_sell and unit_cost and unit_sell > 0:
                margin_pct = round(((unit_sell - unit_cost) / unit_sell) * 100, 2)

            buyer, reason = assign_buyer(offer, offer.vendor_card, db)
            ai_score = score_offer(offer, requirement, offer.vendor_card) if requirement else None

            new_line = BuyPlanLine(
                requirement_id=req_id,
                offer_id=offer.id,
                quantity=edit["quantity"],
                unit_cost=unit_cost,
                unit_sell=unit_sell,
                margin_pct=margin_pct,
                ai_score=ai_score,
                buyer_id=buyer.id if buyer else None,
                assignment_reason=reason,
                status=BuyPlanLineStatus.awaiting_po.value,
                sales_note=edit.get("sales_note"),
            )
            plan.lines.append(new_line)

    _recalculate_financials(plan)


def _apply_line_overrides(plan: BuyPlanV3, overrides: list[dict], db: Session):
    """Apply manager's line-level overrides (vendor swap, quantity, notes)."""
    for ovr in overrides:
        line = next((ln for ln in plan.lines if ln.id == ovr["line_id"]), None)
        if not line:
            logger.warning("Override line_id %d not found in plan %d", ovr["line_id"], plan.id)
            continue

        if ovr.get("offer_id"):
            offer = db.get(Offer, ovr["offer_id"])
            if offer:
                line.offer_id = offer.id
                line.unit_cost = float(offer.unit_price) if offer.unit_price else None
                if line.unit_sell and line.unit_cost and float(line.unit_sell) > 0:
                    line.margin_pct = round(
                        ((float(line.unit_sell) - float(line.unit_cost)) / float(line.unit_sell)) * 100, 2
                    )

        if ovr.get("quantity"):
            line.quantity = ovr["quantity"]

        if ovr.get("manager_note"):
            line.manager_note = ovr["manager_note"]

    _recalculate_financials(plan)


def _recalculate_financials(plan: BuyPlanV3):
    """Recompute plan-level cost, revenue, margin from lines."""
    total_cost = 0.0
    total_revenue = 0.0
    for line in plan.lines:
        if line.unit_cost and line.quantity:
            total_cost += float(line.unit_cost) * line.quantity
        if line.unit_sell and line.quantity:
            total_revenue += float(line.unit_sell) * line.quantity

    plan.total_cost = round(total_cost, 2) if total_cost else None
    plan.total_revenue = round(total_revenue, 2) if total_revenue else None
    if total_revenue > 0:
        plan.total_margin_pct = round(((total_revenue - total_cost) / total_revenue) * 100, 2)


def _is_stock_sale(plan: BuyPlanV3, db: Session) -> bool:
    """Detect stock/internal sales by vendor name match against config."""
    stock_names = settings.stock_sale_vendor_names
    if not plan.lines:
        return False
    for line in plan.lines:
        if not line.offer_id:
            return False
        offer = db.get(Offer, line.offer_id)
        if not offer:
            return False
        vendor = (offer.vendor_name or "").strip().lower()
        if vendor not in stock_names:
            return False
    return True


# ── Intelligence: Favoritism Detection ─────────────────────────────


def detect_favoritism(salesperson_id: int, db: Session) -> list[dict]:
    """Detect if a salesperson disproportionately routes work to specific buyers.

    Looks at all completed/active V3 buy plans submitted by this salesperson
    and calculates buyer assignment distribution. Flags if any buyer receives
    more than the configured threshold percentage.

    Returns list of findings: [{buyer_id, buyer_name, pct, plan_count, severity}]
    """
    threshold = settings.buyplan_favoritism_threshold_pct

    # Get all plans by this salesperson
    plans = (
        db.query(BuyPlanV3)
        .filter(
            BuyPlanV3.submitted_by_id == salesperson_id,
            BuyPlanV3.status.in_(["active", "completed", "pending"]),
        )
        .options(joinedload(BuyPlanV3.lines))
        .all()
    )
    if len(plans) < 3:
        return []  # not enough data to detect patterns

    # Count lines per buyer
    buyer_counts: dict[int, int] = {}
    total_lines = 0
    for plan in plans:
        for line in plan.lines or []:
            if line.buyer_id:
                buyer_counts[line.buyer_id] = buyer_counts.get(line.buyer_id, 0) + 1
                total_lines += 1

    if total_lines == 0:
        return []

    findings = []
    for buyer_id, count in buyer_counts.items():
        pct = round(count / total_lines * 100, 1)
        if pct >= threshold:
            buyer = db.get(User, buyer_id)
            findings.append(
                {
                    "buyer_id": buyer_id,
                    "buyer_name": buyer.name if buyer else "Unknown",
                    "pct": pct,
                    "line_count": count,
                    "total_lines": total_lines,
                    "plan_count": len(plans),
                    "severity": "warning",
                    "message": (
                        f"{buyer.name if buyer else 'Unknown'} receives {pct}% of "
                        f"line assignments ({count}/{total_lines} lines across "
                        f"{len(plans)} plans)"
                    ),
                }
            )

    return findings


# ── Intelligence: Case Report ──────────────────────────────────────


def generate_case_report(plan: BuyPlanV3, db: Session) -> str:
    """Generate a structured case report when a buy plan completes.

    Captures: deal metadata, margin analysis, vendor selection, timeline,
    issue tracking. Stored in plan.case_report for post-deal analysis.
    """
    now = datetime.now(timezone.utc)
    lines = plan.lines or []
    quote = db.get(Quote, plan.quote_id) if plan.quote_id else None

    # ── Customer info
    customer = "Unknown"
    quote_number = "—"
    if quote:
        quote_number = quote.quote_number or "—"
        if quote.customer_site:
            site = quote.customer_site
            co = site.company if hasattr(site, "company") and site.company else None
            customer = co.name if co else (site.site_name or "Unknown")

    # ── Financials
    total_cost = float(plan.total_cost or 0)
    total_revenue = float(plan.total_revenue or 0)
    margin_pct = float(plan.total_margin_pct or 0)

    # ── Vendor breakdown
    vendor_lines: dict[str, list] = {}
    for line in lines:
        offer = line.offer or (db.get(Offer, line.offer_id) if line.offer_id else None)
        vendor = offer.vendor_name if offer else "Unknown"
        vendor_lines.setdefault(vendor, []).append(line)

    vendor_summary = []
    for vendor, vlines in vendor_lines.items():
        v_cost = sum(float(ln.unit_cost or 0) * (ln.quantity or 0) for ln in vlines)
        v_qty = sum(ln.quantity or 0 for ln in vlines)
        vendor_summary.append(f"  - {vendor}: {len(vlines)} lines, {v_qty:,} pcs, ${v_cost:,.2f}")

    # ── Timeline
    def _tz_aware(dt):
        """Ensure datetime is UTC-aware for safe subtraction."""
        if dt and dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    timeline = []
    created = _tz_aware(plan.created_at)
    submitted = _tz_aware(plan.submitted_at)
    approved = _tz_aware(plan.approved_at)
    completed = _tz_aware(plan.completed_at)

    if created and submitted:
        days = (submitted - created).days
        timeline.append(f"  Build → Submit: {days} day{'s' if days != 1 else ''}")
    if submitted and approved:
        days = (approved - submitted).days
        hrs = int((approved - submitted).total_seconds() / 3600)
        timeline.append(f"  Submit → Approve: {hrs}h ({days}d)")
    if approved and completed:
        days = (approved - completed).days if completed > approved else (completed - approved).days
        timeline.append(f"  Approve → Complete: {abs(days)} day{'s' if abs(days) != 1 else ''}")
    if created and completed:
        total_days = (completed - created).days
        timeline.append(f"  Total cycle: {total_days} day{'s' if total_days != 1 else ''}")

    # ── PO timing (avg days from approval to PO confirm)
    po_times = []
    for line in lines:
        if line.po_confirmed_at and approved:
            delta = (_tz_aware(line.po_confirmed_at) - approved).total_seconds() / 3600
            po_times.append(delta)
    avg_po_hrs = round(sum(po_times) / len(po_times), 1) if po_times else None

    # ── Issues encountered
    issues = []
    for line in lines:
        if line.issue_type:
            offer = line.offer
            mpn = offer.mpn if offer else "—"
            issues.append(f"  - {mpn}: {line.issue_type} — {line.issue_note or 'no note'}")

    # ── AI flags summary
    flag_lines = []
    for f in plan.ai_flags or []:
        if isinstance(f, dict):
            flag_lines.append(f"  - [{f.get('severity', '?')}] {f.get('type', '?')}: {f.get('message', '')}")

    # ── Rejections
    rejections = []
    if plan.so_rejection_note:
        rejections.append(f"  - SO rejected: {plan.so_rejection_note}")
    for line in lines:
        if line.po_rejection_note:
            rejections.append(f"  - PO rejected (line {line.id}): {line.po_rejection_note}")

    # ── Build report
    submitter = db.get(User, plan.submitted_by_id) if plan.submitted_by_id else None
    approver = db.get(User, plan.approved_by_id) if plan.approved_by_id else None

    report = f"""CASE REPORT — Buy Plan #{plan.id}
{"=" * 50}

DEAL OVERVIEW
  Customer: {customer}
  Quote: {quote_number}
  SO#: {plan.sales_order_number or "—"}
  Salesperson: {submitter.name if submitter else "—"}
  Approver: {approver.name if approver else ("Auto-approved" if plan.auto_approved else "—")}

FINANCIALS
  Total Cost: ${total_cost:,.2f}
  Total Revenue: ${total_revenue:,.2f}
  Margin: {margin_pct:.1f}%
  Lines: {len(lines)}

VENDORS ({len(vendor_lines)} total)
{chr(10).join(vendor_summary) if vendor_summary else "  None"}

TIMELINE
{chr(10).join(timeline) if timeline else "  No timeline data"}
  Avg PO turnaround: {f"{avg_po_hrs}h" if avg_po_hrs is not None else "—"}

AI FLAGS ({len(flag_lines)})
{chr(10).join(flag_lines) if flag_lines else "  None"}

ISSUES ({len(issues)})
{chr(10).join(issues) if issues else "  None"}

REJECTIONS ({len(rejections)})
{chr(10).join(rejections) if rejections else "  None"}

Generated: {now.strftime("%Y-%m-%d %H:%M UTC")}
"""
    return report.strip()
