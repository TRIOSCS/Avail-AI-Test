"""Buy Plan — Plan building, AI summary, AI flags.

Auto-builds draft buy plans from won quotes: scoring, auto-split, buyer assignment.

Called by: routers/crm/buy_plans.py
Depends on: buyplan_scoring, models
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session, joinedload

from ..config import settings
from ..models import (
    Offer,
    Quote,
    Requirement,
    VendorCard,
)
from ..models.buy_plan import (
    BuyPlan,
    BuyPlanLine,
    BuyPlanLineStatus,
    BuyPlanStatus,
)
from .buyplan_scoring import (
    _country_to_region,
    assign_buyer,
    score_offer,
)


def build_buy_plan(quote_id: int, db: Session) -> BuyPlan:
    """Auto-build a draft buy plan from a won quote.

    For each requirement:
    1. Fetch all active offers
    2. Score each offer
    3. Select best offer (or auto-split if no single offer covers qty)
    4. Assign buyer
    5. Calculate margins

    Returns an unsaved BuyPlan with lines populated (caller saves).
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

    # Guard: quote must be in actionable state
    if quote.status not in ("won", "sent"):
        raise ValueError(f"Quote must be won or sent to build a buy plan (current: {quote.status})")

    # Guard: prevent duplicate buy plans for same quote
    existing = (
        db.query(BuyPlan)
        .filter(
            BuyPlan.quote_id == quote_id,
            BuyPlan.status.notin_(["cancelled"]),
        )
        .first()
    )
    if existing:
        raise ValueError(
            f"A buy plan already exists for quote {quote_id} (plan #{existing.id}, status: {existing.status})"
        )

    # Determine customer region for geography scoring
    customer_region = None
    if quote.customer_site:
        customer_region = _country_to_region(quote.customer_site.country or quote.customer_site.state)

    # Get all requirements for this requisition
    requirements = db.query(Requirement).filter(Requirement.requisition_id == quote.requisition_id).all()
    if not requirements:
        raise ValueError(f"No requirements found for requisition {quote.requisition_id}")

    plan = BuyPlan(
        quote_id=quote_id,
        requisition_id=quote.requisition_id,
        status=BuyPlanStatus.DRAFT.value,
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
        plan.total_margin_pct = round(((total_revenue - total_cost) / total_revenue) * 100, 2)

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

    Selects the best offer. If no single offer covers the full qty, auto-splits across
    multiple vendors (prefer fewest splits, best score).
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

    # Sort by score descending, then by lowest price, then newest offer (deterministic)
    scored.sort(key=lambda x: (-x[2], float(x[0].unit_price or 9999999), -(x[0].id or 0)))

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
    buyer,
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
        status=BuyPlanLineStatus.AWAITING_PO.value,
    )


# ── AI Summary ──────────────────────────────────────────────────────


def generate_ai_summary(plan: BuyPlan) -> str:
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
    margins = [float(ln.margin_pct) for ln in lines if ln.margin_pct is not None]
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


def generate_ai_flags(plan: BuyPlan, db: Session) -> list[dict]:
    """Generate AI flags for potential issues in the buy plan.

    Checks:
    - Stale offer (>N days old)
    - Low margin (below threshold)
    - Quantity gap (splits don't cover full requirement qty)
    - Better offer available (cheaper alternative not selected)
    - Geography mismatch (vendor in different region from customer)
    """
    flags = []
    now = datetime.now(timezone.utc)
    stale_days = settings.buyplan_stale_offer_days
    min_margin = settings.buyplan_min_margin_pct
    better_pct = settings.buyplan_better_offer_pct

    # Determine customer region for geo mismatch
    customer_region = None
    if plan.quote_id:
        quote = db.get(Quote, plan.quote_id)
        if quote and quote.customer_site:
            customer_region = _country_to_region(quote.customer_site.country or quote.customer_site.state)

    for line in plan.lines or []:
        offer = line.offer or (db.get(Offer, line.offer_id) if line.offer_id else None)

        # ── Stale offer check
        if offer and offer.created_at:
            age = (now - offer.created_at.replace(tzinfo=timezone.utc)).days
            if age > stale_days:
                flags.append(
                    {
                        "type": "stale_offer",
                        "severity": "warning",
                        "line_id": line.id,
                        "message": f"Offer is {age} days old (threshold: {stale_days})",
                    }
                )

        # ── Low margin check
        if line.margin_pct is not None and float(line.margin_pct) < min_margin:
            flags.append(
                {
                    "type": "low_margin",
                    "severity": "warning" if float(line.margin_pct) >= 0 else "critical",
                    "line_id": line.id,
                    "message": f"Margin {line.margin_pct}% below {min_margin}% threshold",
                }
            )

        # ── Better offer available check
        if offer and line.requirement_id:
            _check_better_offer(line, offer, better_pct, flags, db)

        # ── Geography mismatch check
        if offer and customer_region:
            _check_geo_mismatch(line, offer, customer_region, flags, db)

        # ── No buyer assigned check
        if not getattr(line, "buyer_id", None):
            flags.append(
                {
                    "type": "no_buyer",
                    "severity": "critical",
                    "line_id": line.id,
                    "message": (
                        f"No buyer assigned for line (reason: {getattr(line, 'assignment_reason', None) or 'unknown'})"
                    ),
                }
            )

    # ── Quantity gap check (plan-level)
    if plan.lines:
        _check_quantity_gaps(plan, flags, db)

    return flags


def _check_better_offer(
    line: BuyPlanLine,
    selected: Offer,
    threshold_pct: float,
    flags: list[dict],
    db: Session,
):
    """Flag if a cheaper offer exists for the same requirement."""
    if not selected.unit_price or float(selected.unit_price) <= 0:
        return
    selected_price = float(selected.unit_price)
    threshold = selected_price * (1 - threshold_pct / 100)

    alternatives = (
        db.query(Offer)
        .filter(
            Offer.requirement_id == line.requirement_id,
            Offer.status == "active",
            Offer.id != selected.id,
        )
        .all()
    )
    for alt in alternatives:
        if not alt.unit_price or float(alt.unit_price) <= 0:
            continue
        if float(alt.unit_price) <= threshold:
            savings_pct = round((1 - float(alt.unit_price) / selected_price) * 100, 1)
            flags.append(
                {
                    "type": "better_offer",
                    "severity": "info",
                    "line_id": line.id,
                    "message": (
                        f"{alt.vendor_name} offers ${float(alt.unit_price):.4f} "
                        f"({savings_pct}% cheaper than selected ${selected_price:.4f})"
                    ),
                }
            )
            break  # one flag per line is enough


def _check_geo_mismatch(
    line: BuyPlanLine,
    offer: Offer,
    customer_region: str,
    flags: list[dict],
    db: Session,
):
    """Flag if selected vendor is in a different region from the customer."""
    vendor_card = offer.vendor_card or (
        db.query(VendorCard).filter_by(normalized_name=(offer.vendor_name or "").strip().lower()).first()
        if offer.vendor_name
        else None
    )
    if not vendor_card or not vendor_card.hq_country:
        return
    vendor_region = _country_to_region(vendor_card.hq_country)
    if vendor_region and vendor_region != customer_region:
        flags.append(
            {
                "type": "geo_mismatch",
                "severity": "info",
                "line_id": line.id,
                "message": (f"Vendor {offer.vendor_name} is in {vendor_region}, customer is in {customer_region}"),
            }
        )


def _check_quantity_gaps(plan: BuyPlan, flags: list[dict], db: Session):
    """Check if split lines fully cover each requirement's target qty."""
    req_totals: dict[int, int] = {}
    req_targets: dict[int, int] = {}

    for line in plan.lines:
        if line.requirement_id:
            req_totals[line.requirement_id] = req_totals.get(line.requirement_id, 0) + line.quantity
            if line.requirement_id not in req_targets:
                req = line.requirement or db.get(Requirement, line.requirement_id)
                if req:
                    req_targets[line.requirement_id] = req.target_qty or 0

    for req_id, allocated in req_totals.items():
        target = req_targets.get(req_id, 0)
        if target > 0 and allocated < target:
            gap = target - allocated
            flags.append(
                {
                    "type": "quantity_gap",
                    "severity": "critical",
                    "line_id": None,
                    "message": f"Requirement {req_id}: allocated {allocated}, need {target} (gap: {gap})",
                }
            )
