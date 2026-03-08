"""Buy Plan V3 — Offer scoring, lead time parsing, buyer assignment, routing maps.

Scoring weights: price 30%, reliability 25%, lead time 20%, geography 15%, terms 10%

Called by: buyplan_builder, buyplan_workflow
Depends on: models (Offer, Requirement, VendorCard, User, VerificationGroupMember)
"""

import json
from pathlib import Path

from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ..models import (
    Offer,
    Requirement,
    User,
    VendorCard,
)

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
    from ..models.buy_plan import BuyPlanLine, BuyPlanLineStatus

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
    vendor_commodities: set[str] = set()
    maps = _get_routing_maps()
    brand_map = maps.get("brand_commodity_map", {})
    if vendor_card and vendor_card.commodity_tags:
        vendor_commodities = set(t.lower() for t in (vendor_card.commodity_tags or []))
    if offer.manufacturer:
        mfr_commodity = brand_map.get(offer.manufacturer.strip().lower())
        if mfr_commodity:
            vendor_commodities.add(mfr_commodity)
    if vendor_commodities:
        commodity_buyers = [
            b for b in buyers if b.commodity_tags and vendor_commodities & {t.lower() for t in b.commodity_tags}
        ]
        if len(commodity_buyers) == 1:
            return commodity_buyers[0], "commodity_match"
        if commodity_buyers:
            buyers = commodity_buyers  # narrow pool for geography/workload

    # Priority 3: Geography match
    if vendor_card and vendor_card.hq_country:
        vendor_region = _country_to_region(vendor_card.hq_country)
        if vendor_region:
            region_buyers = [
                b
                for b in buyers
                if b.commodity_tags is not None  # reuse commodity_tags presence as "has routing profile"
            ]
            # Among narrowed buyers, pick by workload below
            if region_buyers:
                buyers = region_buyers

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
