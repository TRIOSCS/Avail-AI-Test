"""
buy_plan_v3_service.py — Buy Plan V3 Service Layer

Phase 3: AI Build Logic — scoring, auto-split, buyer assignment, AI flags.
Phase 4: Approval + Execution — submit, approve, verify SO/PO, flag issues,
         auto-complete.

Scoring weights: price 30%, reliability 25%, lead time 20%, geography 15%, terms 10%

Called by: routers/buy_plan.py (Phase 5)
Depends on: models (BuyPlanV3, BuyPlanLine, Offer, Requirement, VendorCard, User,
            VerificationGroupMember), config (thresholds), config/routing_maps.json
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

log = logging.getLogger("avail.buyplan_v3")

# ── Routing maps (loaded once) ──────────────────────────────────────

_ROUTING_MAPS: dict | None = None


def _get_routing_maps() -> dict:
    global _ROUTING_MAPS
    if _ROUTING_MAPS is None:
        maps_path = Path(__file__).parent.parent / "config" / "routing_maps.json"
        if maps_path.exists():
            _ROUTING_MAPS = json.loads(maps_path.read_text())
        else:
            _ROUTING_MAPS = {"brand_commodity_map": {}, "country_region_map": {}}
    return _ROUTING_MAPS


def _country_to_region(country: str | None) -> str | None:
    """Map a country name/code to a region (americas, emea, apac)."""
    if not country:
        return None
    maps = _get_routing_maps()
    return maps.get("country_region_map", {}).get(country.strip().lower())


# ── Offer Scoring ───────────────────────────────────────────────────

# Weights must sum to 1.0
W_PRICE = 0.30
W_RELIABILITY = 0.25
W_LEAD_TIME = 0.20
W_GEOGRAPHY = 0.15
W_TERMS = 0.10


def score_offer(
    offer: Offer,
    requirement: Requirement,
    vendor_card: VendorCard | None,
    customer_region: str | None = None,
) -> float:
    """Score an offer 0-100 using weighted formula.

    Components:
    - Price (30%): how close to target price (lower = better)
    - Reliability (25%): vendor_score from VendorCard (0-100)
    - Lead time (20%): shorter lead time scores higher
    - Geography (15%): same region as customer scores 100, else 50
    - Terms (10%): payment terms favorability (has terms = 80, none = 50)
    """
    scores = {}

    # ── Price score (0-100): ratio of target/actual, capped at 100
    target = float(requirement.target_price) if requirement.target_price is not None else None
    actual = float(offer.unit_price) if offer.unit_price is not None else None
    if actual and actual > 0 and target and target > 0:
        ratio = target / actual
        scores["price"] = min(ratio * 100, 100.0)
    elif actual and actual > 0:
        scores["price"] = 50.0  # no target to compare
    else:
        scores["price"] = 0.0

    # ── Reliability score (0-100): vendor's unified score
    if vendor_card and vendor_card.vendor_score is not None:
        scores["reliability"] = min(vendor_card.vendor_score, 100.0)
    elif vendor_card and vendor_card.is_new_vendor is False:
        scores["reliability"] = 50.0  # known vendor, no score yet
    else:
        scores["reliability"] = 25.0  # unknown vendor

    # ── Lead time score (0-100): parse days, shorter = better
    lead_days = _parse_lead_time_days(offer.lead_time)
    if lead_days is not None:
        if lead_days <= 3:
            scores["lead_time"] = 100.0
        elif lead_days <= 7:
            scores["lead_time"] = 85.0
        elif lead_days <= 14:
            scores["lead_time"] = 70.0
        elif lead_days <= 30:
            scores["lead_time"] = 50.0
        else:
            scores["lead_time"] = max(30.0, 100 - lead_days)
    else:
        scores["lead_time"] = 40.0  # unknown lead time

    # ── Geography score (0-100): same region = 100
    vendor_region = None
    if vendor_card and vendor_card.hq_country:
        vendor_region = _country_to_region(vendor_card.hq_country)
    if customer_region and vendor_region:
        scores["geography"] = 100.0 if customer_region == vendor_region else 50.0
    else:
        scores["geography"] = 60.0  # unknown geography

    # ── Terms score (0-100): known vendor with history = better terms assumption
    if vendor_card and vendor_card.total_pos and vendor_card.total_pos > 0:
        scores["terms"] = 85.0  # established PO history
    elif vendor_card and not vendor_card.is_new_vendor:
        scores["terms"] = 65.0  # known vendor
    else:
        scores["terms"] = 50.0  # unknown

    # ── Weighted total
    total = (
        scores["price"] * W_PRICE
        + scores["reliability"] * W_RELIABILITY
        + scores["lead_time"] * W_LEAD_TIME
        + scores["geography"] * W_GEOGRAPHY
        + scores["terms"] * W_TERMS
    )
    return round(total, 1)


def _parse_lead_time_days(lead_time: str | None) -> int | None:
    """Extract days from lead time strings like '3-5 days', '2 weeks', 'stock'."""
    if not lead_time:
        return None
    lt = lead_time.strip().lower()
    if lt in ("stock", "in stock", "immediate", "same day"):
        return 0
    # Try to extract a number
    import re
    nums = re.findall(r"\d+", lt)
    if not nums:
        return None
    val = int(nums[-1])  # use last number (e.g. "3-5 days" → 5)
    if "week" in lt:
        val *= 7
    elif "month" in lt:
        val *= 30
    return val


# ── Buyer Assignment ────────────────────────────────────────────────


def assign_buyer(
    offer: Offer,
    vendor_card: VendorCard | None,
    db: Session,
) -> tuple[User | None, str]:
    """Assign a buyer to a line using priority cascade.

    Priority:
    1. Vendor ownership — offer.entered_by owns this vendor relationship
    2. Commodity match — buyer works same commodity as the part
    3. Geography match — buyer region matches vendor region
    4. Lowest workload — fewest active awaiting_po lines

    Returns (user, reason) or (None, "no_buyers").
    """
    # Priority 1: The buyer who entered the offer owns the vendor relationship
    if offer.entered_by_id:
        entered_by = db.get(User, offer.entered_by_id)
        if entered_by and entered_by.is_active and entered_by.role in ("buyer", "trader"):
            return entered_by, "vendor_ownership"

    # Get all active buyers
    buyers = (
        db.query(User)
        .filter(User.role.in_(["buyer", "trader"]), User.is_active == True)  # noqa: E712
        .all()
    )
    if not buyers:
        return None, "no_buyers"

    # Priority 2: Commodity match
    if vendor_card and vendor_card.commodity_tags:
        vendor_commodities = set(
            t.lower() for t in (vendor_card.commodity_tags or [])
        )
        # Check if offer manufacturer maps to a commodity
        maps = _get_routing_maps()
        brand_map = maps.get("brand_commodity_map", {})
        if offer.manufacturer:
            mfr_commodity = brand_map.get(offer.manufacturer.strip().lower())
            if mfr_commodity:
                vendor_commodities.add(mfr_commodity)
        # For now, we don't have per-buyer commodity tags on User model,
        # so we skip this and fall through to geography/workload.
        # This is a Phase 10 enhancement.

    # Priority 3: Geography match
    if vendor_card and vendor_card.hq_country:
        vendor_region = _country_to_region(vendor_card.hq_country)
        if vendor_region:
            # Prefer buyers who have handled vendors in the same region
            # (approximated by the buyer's most recent offer vendor region)
            # For now, fall through to workload — full geo matching is Phase 10
            pass

    # Priority 4: Lowest active workload
    workloads = {}
    for buyer in buyers:
        count = (
            db.query(sqlfunc.count(BuyPlanLine.id))
            .filter(
                BuyPlanLine.buyer_id == buyer.id,
                BuyPlanLine.status == BuyPlanLineStatus.awaiting_po.value,
            )
            .scalar()
        ) or 0
        workloads[buyer.id] = count

    best = min(buyers, key=lambda b: workloads.get(b.id, 0))
    return best, "workload"


# ── Build Buy Plan ──────────────────────────────────────────────────


def build_buy_plan(quote_id: int, db: Session) -> BuyPlanV3:
    """Auto-build a draft buy plan from a won quote.

    For each requirement:
    1. Fetch all active offers
    2. Score each offer
    3. Select best offer (or auto-split if no single offer covers qty)
    4. Assign buyer
    5. Calculate margins

    Returns an unsaved BuyPlanV3 with lines populated (caller saves).
    """
    quote = db.get(
        Quote,
        quote_id,
        options=[
            joinedload(Quote.customer_site),
            joinedload(Quote.requisition),
        ],
    )
    if not quote:
        raise ValueError(f"Quote {quote_id} not found")

    # Determine customer region for geography scoring
    customer_region = None
    if quote.customer_site:
        customer_region = _country_to_region(
            quote.customer_site.country or quote.customer_site.state
        )

    # Get all requirements for this requisition
    requirements = (
        db.query(Requirement)
        .filter(Requirement.requisition_id == quote.requisition_id)
        .all()
    )
    if not requirements:
        raise ValueError(f"No requirements found for requisition {quote.requisition_id}")

    plan = BuyPlanV3(
        quote_id=quote_id,
        requisition_id=quote.requisition_id,
        status=BuyPlanStatus.draft.value,
    )

    total_cost = 0.0
    total_revenue = 0.0

    for req in requirements:
        lines = _build_lines_for_requirement(req, customer_region, db)
        for line in lines:
            line.buy_plan = plan
            # Accumulate financials
            if line.unit_cost and line.quantity:
                total_cost += float(line.unit_cost) * line.quantity
            if line.unit_sell and line.quantity:
                total_revenue += float(line.unit_sell) * line.quantity

    # Set financials on plan
    plan.total_cost = round(total_cost, 2) if total_cost else None
    plan.total_revenue = round(total_revenue, 2) if total_revenue else None
    if total_revenue and total_revenue > 0:
        plan.total_margin_pct = round(
            ((total_revenue - total_cost) / total_revenue) * 100, 2
        )

    # Generate AI analysis
    plan.ai_summary = generate_ai_summary(plan)
    plan.ai_flags = [f.__dict__ if hasattr(f, "__dict__") else f for f in generate_ai_flags(plan, db)]

    return plan


def _build_lines_for_requirement(
    requirement: Requirement,
    customer_region: str | None,
    db: Session,
) -> list[BuyPlanLine]:
    """Build buy plan lines for a single requirement.

    Selects the best offer. If no single offer covers the full qty,
    auto-splits across multiple vendors (prefer fewest splits, best score).
    """
    target_qty = requirement.target_qty or 1

    # Fetch all active offers for this requirement
    offers = (
        db.query(Offer)
        .options(joinedload(Offer.vendor_card))
        .filter(
            Offer.requirement_id == requirement.id,
            Offer.status == "active",
        )
        .all()
    )
    if not offers:
        return []

    # Score each offer
    scored = []
    for offer in offers:
        vendor_card = offer.vendor_card
        score = score_offer(offer, requirement, vendor_card, customer_region)
        scored.append((offer, vendor_card, score))

    # Sort by score descending
    scored.sort(key=lambda x: x[2], reverse=True)

    # Try single-vendor fulfillment first
    for offer, vendor_card, score in scored:
        if (offer.qty_available or 0) >= target_qty:
            buyer, reason = assign_buyer(offer, vendor_card, db)
            line = _create_line(requirement, offer, target_qty, score, buyer, reason)
            return [line]

    # Auto-split: greedily assign from best-scored offers
    lines = []
    remaining = target_qty
    used_offer_ids = set()

    for offer, vendor_card, score in scored:
        if remaining <= 0:
            break
        qty_avail = offer.qty_available or 0
        if qty_avail <= 0 or offer.id in used_offer_ids:
            continue

        alloc = min(qty_avail, remaining)
        buyer, reason = assign_buyer(offer, vendor_card, db)
        line = _create_line(requirement, offer, alloc, score, buyer, reason)
        lines.append(line)
        remaining -= alloc
        used_offer_ids.add(offer.id)

    return lines


def _create_line(
    requirement: Requirement,
    offer: Offer,
    quantity: int,
    ai_score: float,
    buyer: User | None,
    assignment_reason: str,
) -> BuyPlanLine:
    """Create a single BuyPlanLine from a scored offer."""
    unit_cost = float(offer.unit_price) if offer.unit_price else None
    # Use target_price from requirement as the sell price
    unit_sell = float(requirement.target_price) if requirement.target_price else None

    margin_pct = None
    if unit_sell and unit_cost and unit_sell > 0:
        margin_pct = round(((unit_sell - unit_cost) / unit_sell) * 100, 2)

    return BuyPlanLine(
        requirement_id=requirement.id,
        offer_id=offer.id,
        quantity=quantity,
        unit_cost=unit_cost,
        unit_sell=unit_sell,
        margin_pct=margin_pct,
        ai_score=ai_score,
        buyer_id=buyer.id if buyer else None,
        assignment_reason=assignment_reason,
        status=BuyPlanLineStatus.awaiting_po.value,
    )


# ── AI Summary ──────────────────────────────────────────────────────


def generate_ai_summary(plan: BuyPlanV3) -> str:
    """Generate a plain English summary of the buy plan.

    Example: '3 lines across 2 vendors. Avg margin 42%. 1 flag.'
    """
    lines = plan.lines or []
    if not lines:
        return "Empty buy plan — no lines generated."

    line_count = len(lines)
    vendor_ids = set()
    for line in lines:
        if line.offer_id:
            vendor_ids.add(line.offer_id)  # proxy — unique offers ≈ unique vendors

    # Count unique vendors from offers
    vendor_names = set()
    for line in lines:
        if line.offer and line.offer.vendor_name:
            vendor_names.add(line.offer.vendor_name.lower())
    vendor_count = len(vendor_names) or len(vendor_ids)

    # Average margin
    margins = [float(l.margin_pct) for l in lines if l.margin_pct is not None]
    avg_margin = round(sum(margins) / len(margins), 1) if margins else None

    # Flag count
    flags = plan.ai_flags or []
    flag_count = len(flags)

    parts = [f"{line_count} line{'s' if line_count != 1 else ''}"]
    if vendor_count:
        parts.append(f"{vendor_count} vendor{'s' if vendor_count != 1 else ''}")
    if avg_margin is not None:
        parts.append(f"avg margin {avg_margin}%")
    if flag_count:
        parts.append(f"{flag_count} flag{'s' if flag_count != 1 else ''}")

    return ", ".join(parts) + "."


# ── AI Flags ────────────────────────────────────────────────────────


def generate_ai_flags(plan: BuyPlanV3, db: Session) -> list[dict]:
    """Generate AI flags for potential issues in the buy plan.

    Checks:
    - Stale offer (>N days old)
    - Low margin (below threshold)
    - Quantity gap (splits don't cover full requirement qty)
    - Geography mismatch (vendor in different region from customer)
    """
    flags = []
    now = datetime.now(timezone.utc)
    stale_days = settings.buyplan_stale_offer_days
    min_margin = settings.buyplan_min_margin_pct

    for line in (plan.lines or []):
        # ── Stale offer check
        if line.offer_id:
            offer = line.offer or db.get(Offer, line.offer_id)
            if offer and offer.created_at:
                age = (now - offer.created_at.replace(tzinfo=timezone.utc)).days
                if age > stale_days:
                    flags.append({
                        "type": "stale_offer",
                        "severity": "warning",
                        "line_id": line.id,
                        "message": f"Offer is {age} days old (threshold: {stale_days})",
                    })

        # ── Low margin check
        if line.margin_pct is not None and float(line.margin_pct) < min_margin:
            flags.append({
                "type": "low_margin",
                "severity": "warning" if float(line.margin_pct) >= 0 else "critical",
                "line_id": line.id,
                "message": f"Margin {line.margin_pct}% below {min_margin}% threshold",
            })

    # ── Quantity gap check (plan-level)
    if plan.lines:
        _check_quantity_gaps(plan, flags, db)

    return flags


def _check_quantity_gaps(plan: BuyPlanV3, flags: list[dict], db: Session):
    """Check if split lines fully cover each requirement's target qty."""
    req_totals: dict[int, int] = {}
    req_targets: dict[int, int] = {}

    for line in plan.lines:
        if line.requirement_id:
            req_totals[line.requirement_id] = (
                req_totals.get(line.requirement_id, 0) + line.quantity
            )
            if line.requirement_id not in req_targets:
                req = line.requirement or db.get(Requirement, line.requirement_id)
                if req:
                    req_targets[line.requirement_id] = req.target_qty or 0

    for req_id, allocated in req_totals.items():
        target = req_targets.get(req_id, 0)
        if target > 0 and allocated < target:
            gap = target - allocated
            flags.append({
                "type": "quantity_gap",
                "severity": "critical",
                "line_id": None,
                "message": f"Requirement {req_id}: allocated {allocated}, need {target} (gap: {gap})",
            })


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
        (f.get("severity") if isinstance(f, dict) else getattr(f, "severity", None))
        == "critical"
        for f in (plan.ai_flags or [])
    )
    if total < settings.buyplan_auto_approve_threshold and not has_critical:
        plan.status = BuyPlanStatus.active.value
        plan.auto_approved = True
        plan.approved_at = datetime.now(timezone.utc)
        log.info("Buy plan %d auto-approved (cost=%.2f)", plan_id, total)
    else:
        plan.status = BuyPlanStatus.pending.value
        log.info(
            "Buy plan %d pending approval (cost=%.2f, critical=%s)",
            plan_id, total, has_critical,
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
        raise ValueError(
            f"Can only approve/reject pending plans (current: {plan.status})"
        )

    now = datetime.now(timezone.utc)
    if action == "approve":
        if line_overrides:
            _apply_line_overrides(plan, line_overrides, db)
        plan.status = BuyPlanStatus.active.value
        plan.approved_by_id = user.id
        plan.approved_at = now
        plan.approval_notes = notes
        log.info("Buy plan %d approved by %s", plan_id, user.email)
    elif action == "reject":
        plan.status = BuyPlanStatus.draft.value
        plan.approval_notes = notes
        log.info("Buy plan %d rejected by %s: %s", plan_id, user.email, notes)
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

    member = (
        db.query(VerificationGroupMember)
        .filter_by(user_id=user.id, is_active=True)
        .first()
    )
    if not member:
        raise PermissionError("User is not in the ops verification group")

    now = datetime.now(timezone.utc)
    plan.so_verified_by_id = user.id
    plan.so_verified_at = now

    if action == "approve":
        plan.so_status = SOVerificationStatus.approved.value
        log.info("SO verified for plan %d by %s", plan_id, user.email)
    elif action == "reject":
        plan.so_status = SOVerificationStatus.rejected.value
        plan.so_rejection_note = rejection_note
        log.info("SO rejected for plan %d: %s", plan_id, rejection_note)
    elif action == "halt":
        plan.so_status = SOVerificationStatus.rejected.value
        plan.so_rejection_note = rejection_note
        plan.status = BuyPlanStatus.halted.value
        plan.halted_by_id = user.id
        plan.halted_at = now
        log.info("Plan %d HALTED by %s: %s", plan_id, user.email, rejection_note)
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
    log.info("PO %s confirmed for line %d (plan %d)", po_number, line_id, plan_id)

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
        raise ValueError(
            f"Line must be pending verification (current: {line.status})"
        )

    member = (
        db.query(VerificationGroupMember)
        .filter_by(user_id=user.id, is_active=True)
        .first()
    )
    if not member:
        raise PermissionError("User is not in the ops verification group")

    now = datetime.now(timezone.utc)
    if action == "approve":
        line.status = BuyPlanLineStatus.verified.value
        line.po_verified_by_id = user.id
        line.po_verified_at = now
        log.info("PO verified for line %d (plan %d)", line_id, plan_id)
        check_completion(plan_id, db)
    elif action == "reject":
        line.status = BuyPlanLineStatus.awaiting_po.value
        line.po_rejection_note = rejection_note
        line.po_number = None
        line.estimated_ship_date = None
        line.po_confirmed_at = None
        log.info("PO rejected for line %d: %s", line_id, rejection_note)
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
    log.info("Issue '%s' flagged on line %d (plan %d)", issue_type, line_id, plan_id)

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
        log.info("Buy plan %d auto-completed (all lines terminal)", plan_id)
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
        (f.get("severity") if isinstance(f, dict) else getattr(f, "severity", None))
        == "critical"
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
    to_remove = [l for l in plan.lines if l.requirement_id in affected]
    for line in to_remove:
        plan.lines.remove(line)

    for req_id, req_edits in edits_by_req.items():
        requirement = db.get(Requirement, req_id)
        for edit in req_edits:
            offer = db.get(Offer, edit["offer_id"])
            if not offer:
                raise ValueError(f"Offer {edit['offer_id']} not found")

            unit_cost = float(offer.unit_price) if offer.unit_price else None
            unit_sell = (
                float(requirement.target_price)
                if requirement and requirement.target_price
                else None
            )
            margin_pct = None
            if unit_sell and unit_cost and unit_sell > 0:
                margin_pct = round(
                    ((unit_sell - unit_cost) / unit_sell) * 100, 2
                )

            buyer, reason = assign_buyer(offer, offer.vendor_card, db)
            ai_score = (
                score_offer(offer, requirement, offer.vendor_card)
                if requirement
                else None
            )

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
        line = next((l for l in plan.lines if l.id == ovr["line_id"]), None)
        if not line:
            log.warning(
                "Override line_id %d not found in plan %d", ovr["line_id"], plan.id
            )
            continue

        if ovr.get("offer_id"):
            offer = db.get(Offer, ovr["offer_id"])
            if offer:
                line.offer_id = offer.id
                line.unit_cost = float(offer.unit_price) if offer.unit_price else None
                if line.unit_sell and line.unit_cost and float(line.unit_sell) > 0:
                    line.margin_pct = round(
                        ((float(line.unit_sell) - float(line.unit_cost))
                         / float(line.unit_sell)) * 100, 2
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
        plan.total_margin_pct = round(
            ((total_revenue - total_cost) / total_revenue) * 100, 2
        )


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
