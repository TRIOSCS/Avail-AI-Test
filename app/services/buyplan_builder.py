"""Buy Plan — Plan building, AI summary, AI flags.

Auto-builds draft buy plans from won quotes: scoring, auto-split, buyer assignment.

Called by: routers/htmx_views.py
Depends on: buyplan_scoring, models
"""

from datetime import UTC, datetime

from loguru import logger
from sqlalchemy.orm import Session, joinedload

from ..config import settings
from ..constants import OfferStatus, QuoteStatus
from ..models import (
    Offer,
    Quote,
    QuoteLine,
    Requirement,
    Requisition,
    User,
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

    Returns an unsaved BuyPlan with lines populated (caller saves). The shared
    scoring/assignment/line-building/margin/AI-summary core lives in
    ``_assemble_buy_plan``; this entry point owns only the quote-specific prologue
    (load quote, WON/SENT + duplicate guards, region/sell-price derivation).
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

    # Hard-guard: a combined quote (OQ-02) spans 2+ requisitions, but this builder assembles
    # a plan from ONE requisition (quote.requisition). Building from a combined quote would
    # silently drop every non-primary requisition's lines, so refuse it outright (the router
    # maps ValueError → 400) rather than emit a partial plan that looks complete.
    from .quote_requisitions import requisition_ids_for_quote

    contributing = requisition_ids_for_quote(db, quote_id)
    if len(contributing) > 1:
        raise ValueError(
            f"Quote {quote.quote_number} spans {len(contributing)} requisitions — building a "
            f"buy plan from a combined quote isn't supported yet."
        )

    # Guard: quote must be in actionable state
    if quote.status not in (QuoteStatus.WON.value, QuoteStatus.SENT.value):
        raise ValueError(f"Quote must be won or sent to build a buy plan (current: {quote.status})")

    # Guard: prevent duplicate buy plans for same quote
    existing = (
        db.query(BuyPlan)
        .filter(
            BuyPlan.quote_id == quote_id,
            BuyPlan.status.notin_([BuyPlanStatus.CANCELLED.value]),
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

    # Map requirement_id -> the offer the salesperson CHOSE on the quote, so the buy plan
    # defaults to exactly what was quoted instead of re-scoring from scratch (mirrors the
    # resell CustomerBidLine.selected_offer_id provenance). QuoteLine has no requirement_id,
    # so derive it via the line's offer (Offer.requirement_id) in one join.
    quote_offer_by_req = _quote_chosen_offers(quote_id, db)

    # Quote path carries no per-requirement sell-price overrides — the shared core falls
    # back to each requirement's target_price (current behavior).
    plan = _assemble_buy_plan(quote.requisition, quote_offer_by_req, {}, customer_region, db)
    plan.quote_id = quote_id
    return plan


def find_open_sales_order(db: Session, requisition_id: int) -> BuyPlan | None:
    """Return the open, quote-less Sales Order for a requisition, or None.

    The canonical "open Sales Order" is a quote-less BuyPlan (``quote_id IS NULL``) in a
    non-terminal status (DRAFT/PENDING/ACTIVE) for the requisition. Both the SO-origination
    guard below and the route's duplicate-handling resolve the existing plan through this
    single query, so the definition can never drift between them.
    """
    return (
        db.query(BuyPlan)
        .filter(
            BuyPlan.requisition_id == requisition_id,
            BuyPlan.quote_id.is_(None),
            BuyPlan.status.in_(
                [
                    BuyPlanStatus.DRAFT.value,
                    BuyPlanStatus.PENDING.value,
                    BuyPlanStatus.ACTIVE.value,
                ]
            ),
        )
        .first()
    )


class DuplicateSalesOrderError(ValueError):
    """Raised when a requisition already has an open (quote-less, non-terminal) Sales
    Order.

    Subclasses ``ValueError`` so existing ``pytest.raises(ValueError)`` call sites stay
    green. Carries ``existing_plan_id`` and ``status`` so the route can load and render the
    existing plan without re-running the open-SO query.
    """

    def __init__(self, existing_plan_id: int, status: str) -> None:
        self.existing_plan_id = existing_plan_id
        self.status = status
        super().__init__(f"There is already an open Sales Order (plan #{existing_plan_id}, status: {status})")


def create_sales_order_from_offers(
    requisition_id: int,
    selections: dict[int, int],
    sell_prices: dict[int, float],
    db: Session,
    user: User,
) -> BuyPlan:
    """Originate a DRAFT buy plan (Sales Order) directly from chosen RFQ offers — no
    quote.

    ``selections`` maps ``requirement_id -> chosen offer_id`` and ``sell_prices`` maps
    ``requirement_id -> sell price``. Persists a DRAFT BuyPlan with ``quote_id=None`` and
    raises ``DuplicateSalesOrderError`` (a ValueError subclass) if a non-terminal
    (DRAFT/PENDING/ACTIVE) quote-less plan already exists for the requisition. Shares the
    scoring/assignment/line-building core with the quote path via ``_assemble_buy_plan``.
    """
    requisition = db.get(Requisition, requisition_id)
    if requisition is None:
        raise ValueError(f"Requisition {requisition_id} not found")

    # Guard: only one open Sales Order (quote-less plan) per requisition at a time.
    existing = find_open_sales_order(db, requisition_id)
    if existing:
        raise DuplicateSalesOrderError(existing.id, existing.status)

    customer_region = None
    if requisition.customer_site:
        customer_region = _country_to_region(requisition.customer_site.country or requisition.customer_site.state)

    plan = _assemble_buy_plan(requisition, selections, sell_prices, customer_region, db)
    plan.quote_id = None
    # The originator owns the Sales Order (it is built from their own requisition), so it
    # surfaces on their "Mine" deal board and as needs_my_action while in DRAFT.
    plan.submitted_by_id = getattr(user, "id", None)
    db.add(plan)
    db.commit()
    logger.info(
        "Created Sales Order buy plan #{} from {} selected offer(s) for requisition {} (user {})",
        plan.id,
        len(selections),
        requisition_id,
        getattr(user, "id", None),
    )
    return plan


def _assemble_buy_plan(
    requisition: Requisition,
    chosen_offers: dict[int, int],
    sell_prices: dict[int, float],
    customer_region: str | None,
    db: Session,
) -> BuyPlan:
    """Build (unsaved) BuyPlan + lines from chosen offers — shared by quote and SO
    paths.

    ``chosen_offers`` maps ``requirement_id -> offer_id`` (the seeded default offer per
    requirement). ``sell_prices`` maps ``requirement_id -> float`` and overrides the unit
    sell price; a requirement absent from the map falls back to its ``target_price``.
    ``customer_region`` (optional) drives the geo-mismatch AI flag (None skips it). The
    returned plan has ``requisition_id`` and lines populated but no ``quote_id`` — the
    caller sets that and persists.
    """
    requirements = db.query(Requirement).filter(Requirement.requisition_id == requisition.id).all()
    if not requirements:
        raise ValueError(f"No requirements found for requisition {requisition.id}")

    plan = BuyPlan(
        requisition_id=requisition.id,
        status=BuyPlanStatus.DRAFT.value,
    )

    total_cost = 0.0
    total_revenue = 0.0

    for req in requirements:
        sell_price = sell_prices.get(req.id) if sell_prices else None
        lines = _build_lines_for_requirement(req, customer_region, db, chosen_offers.get(req.id), sell_price)
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

    # Generate AI analysis. Pass customer_region directly so geo flags work before the
    # caller wires quote_id (the quote path no longer round-trips through plan.quote_id).
    plan.ai_summary = generate_ai_summary(plan)
    plan.ai_flags = [f.__dict__ if hasattr(f, "__dict__") else f for f in generate_ai_flags(plan, db, customer_region)]

    return plan


def _quote_chosen_offers(quote_id: int, db: Session) -> dict[int, int]:
    """Map ``requirement_id -> offer_id`` for the offers the salesperson chose on the
    quote.

    A quote line links the offer the salesperson is USING (``QuoteLine.offer_id``).
    ``QuoteLine`` carries no requirement_id, so the requirement is derived through the
    chosen offer (``Offer.requirement_id``) in a single join. Lines without an offer
    (manually priced) are skipped, and a requirement with multiple quote lines keeps the
    first chosen offer (one buy-plan default per requirement). The returned map is the
    DEFAULT seed for ``_build_lines_for_requirement`` — staleness is validated there.
    """
    rows = (
        db.query(Offer.requirement_id, QuoteLine.offer_id)
        .join(Offer, Offer.id == QuoteLine.offer_id)
        .filter(QuoteLine.quote_id == quote_id, QuoteLine.offer_id.isnot(None))
        .all()
    )
    chosen: dict[int, int] = {}
    for requirement_id, offer_id in rows:
        if requirement_id is not None and requirement_id not in chosen:
            chosen[requirement_id] = offer_id
    return chosen


def _build_lines_for_requirement(
    requirement: Requirement,
    customer_region: str | None,
    db: Session,
    quote_offer_id: int | None = None,
    sell_price: float | None = None,
) -> list[BuyPlanLine]:
    """Build buy plan lines for a single requirement.

    Defaults to the offer the salesperson CHOSE on the quote (*quote_offer_id*) when it
    is still loadable + active: that offer leads (single-vendor path), and re-
    score/auto-split only fills the remaining qty. When no chosen offer is given — or it
    is stale/inactive — falls back to selecting the best offer; if no single offer
    covers the full qty, auto-splits across multiple vendors (prefer fewest splits, best
    score). *sell_price* overrides each line's unit sell price; None falls back to the
    requirement's target_price.
    """
    target_qty = requirement.target_qty or 1

    # Fetch all active offers for this requirement
    offers = (
        db.query(Offer)
        .options(joinedload(Offer.vendor_card))
        .filter(
            Offer.requirement_id == requirement.id,
            Offer.status == OfferStatus.ACTIVE.value,
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

    lines: list[BuyPlanLine] = []
    remaining = target_qty
    used_offer_ids: set[int] = set()

    # Seed from the salesperson's chosen offer first when it is still active for this
    # requirement. It leads the plan even when it cannot cover the full qty — the re-score
    # fallback below then fills only the gap.
    if quote_offer_id is not None:
        chosen = next((tup for tup in scored if tup[0].id == quote_offer_id), None)
        if chosen is not None:
            offer, vendor_card, score = chosen
            qty_avail = offer.qty_available or 0
            if qty_avail > 0:
                alloc = min(qty_avail, remaining)
                buyer, reason = assign_buyer(offer, vendor_card, db)
                lines.append(_create_line(requirement, offer, alloc, score, buyer, reason, sell_price))
                remaining -= alloc
                used_offer_ids.add(offer.id)
                if remaining <= 0:
                    return lines

    # Try single-vendor fulfillment first (re-score path) — only when nothing seeded yet.
    if not lines:
        for offer, vendor_card, score in scored:
            if (offer.qty_available or 0) >= target_qty:
                buyer, reason = assign_buyer(offer, vendor_card, db)
                return [_create_line(requirement, offer, target_qty, score, buyer, reason, sell_price)]

    # Auto-split: greedily assign from best-scored offers to fill remaining qty.
    for offer, vendor_card, score in scored:
        if remaining <= 0:
            break
        qty_avail = offer.qty_available or 0
        if qty_avail <= 0 or offer.id in used_offer_ids:
            continue

        alloc = min(qty_avail, remaining)
        buyer, reason = assign_buyer(offer, vendor_card, db)
        line = _create_line(requirement, offer, alloc, score, buyer, reason, sell_price)
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
    unit_sell: float | None = None,
) -> BuyPlanLine:
    """Create a single BuyPlanLine from a scored offer.

    *unit_sell* (when provided) is the explicit per-line sell price; when None it falls
    back to the requirement's target_price (the quote-path default).
    """
    unit_cost = float(offer.unit_price) if offer.unit_price else None
    # Explicit sell price wins; otherwise fall back to the requirement's target_price.
    if unit_sell is None:
        unit_sell = float(requirement.target_price) if requirement.target_price else None
    else:
        unit_sell = float(unit_sell)

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

    # Count unique vendors by name, falling back to offer_id (proxy — unique offers ≈ vendors)
    vendor_names = set()
    vendor_ids = set()
    for line in lines:
        if line.offer and line.offer.vendor_name:
            vendor_names.add(line.offer.vendor_name.lower())
        if line.offer_id:
            vendor_ids.add(line.offer_id)
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


def generate_ai_flags(plan: BuyPlan, db: Session, customer_region: str | None = None) -> list[dict]:
    """Generate AI flags for potential issues in the buy plan.

    Checks:
    - Stale offer (>N days old)
    - Low margin (below threshold)
    - Quantity gap (splits don't cover full requirement qty)
    - Better offer available (cheaper alternative not selected)
    - Geography mismatch (vendor in different region from customer)

    *customer_region* (when provided) drives the geo-mismatch check directly; this lets
    the builder pass the region it already derived (and supports quote-less SO plans). It
    falls back to deriving the region from ``plan.quote_id`` when not supplied.
    """
    flags = []
    now = datetime.now(UTC)
    stale_days = settings.buyplan_stale_offer_days
    min_margin = settings.buyplan_min_margin_pct
    better_pct = settings.buyplan_better_offer_pct

    # Determine customer region for geo mismatch — derive from the quote only when the
    # caller did not already supply it.
    if customer_region is None and plan.quote_id:
        quote = db.get(Quote, plan.quote_id)
        if quote and quote.customer_site:
            customer_region = _country_to_region(quote.customer_site.country or quote.customer_site.state)

    for line in plan.lines or []:
        offer = line.offer or (db.get(Offer, line.offer_id) if line.offer_id else None)

        # ── Stale offer check
        if offer and offer.created_at:
            age = (now - offer.created_at.replace(tzinfo=UTC)).days
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
            Offer.status == OfferStatus.ACTIVE.value,
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
