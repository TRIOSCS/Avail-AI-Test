"""Buy Plan — Reporting: favoritism detection and case-report generation.

Split from the former monolithic `buyplan_workflow.py` (P4.3) along the "reporting"
seam: buyer-assignment favoritism analytics and the post-completion case report.

Called by: services/buyplan_workflow/buyplan_approval.py (generate_case_report on
    completion), services/buyplan_service.py (re-export barrel), tests
Depends on: models (BuyPlan, BuyPlanLine, Offer, Quote, User)
"""

from datetime import UTC, datetime

from sqlalchemy.orm import Session, joinedload

from ...config import settings
from ...models import Offer, Quote, User
from ...models.buy_plan import BuyPlan, BuyPlanStatus

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
        db.query(BuyPlan)
        .filter(
            BuyPlan.submitted_by_id == salesperson_id,
            BuyPlan.status.in_(
                [
                    BuyPlanStatus.ACTIVE.value,
                    BuyPlanStatus.COMPLETED.value,
                    BuyPlanStatus.PENDING.value,
                ]
            ),
        )
        .options(joinedload(BuyPlan.lines))
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

    buyers_by_id = {u.id: u for u in db.query(User).filter(User.id.in_(buyer_counts.keys())).all()}
    findings = []
    for buyer_id, count in buyer_counts.items():
        pct = round(count / total_lines * 100, 1)
        if pct >= threshold:
            buyer = buyers_by_id.get(buyer_id)
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


def generate_case_report(plan: BuyPlan, db: Session) -> str:
    """Generate a structured case report when a buy plan completes.

    Captures: deal metadata, margin analysis, vendor selection, timeline,
    issue tracking. Stored in plan.case_report for post-deal analysis.
    """
    now = datetime.now(UTC)
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
    elif plan.requisition and plan.requisition.customer_name:
        customer = plan.requisition.customer_name
    elif plan.requisition and plan.requisition.customer_site and plan.requisition.customer_site.company:
        customer = plan.requisition.customer_site.company.name

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
            return dt.replace(tzinfo=UTC)
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
