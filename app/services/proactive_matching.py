"""Proactive matching engine — matches live vendor supply to customer demand history.

Seeding (2026-08-06 rework): a customer's requirement history inside the
requirement window (any requisition status — a won/lost/archived ask is exactly
the dormant demand this feature exists to catch) plus HOTLIST requisitions
(standing monitors, no window). Purchase history (CPH) no longer seeds matches;
it enriches them as a context signal ("bought Nx, last paid $X").

Part identity (2026-08-08): matching joins on the normalized key
(normalize_mpn_key), so formatting variants like "LTSR15-NP" / "LTSR 15-NP"
pool automatically, and stored AI/human 'same' verdicts (part_equivalences)
pool ordering-suffix variants — color-coded in the UI as guesses to
double-check; 'different'/'uncertain'/absent never pool. part_key() remains
the DISPLAY spelling. Supply side aggregates per class: available qty = SUM
across live offers in the offer window, low cost = MIN positive unit price
(see compute_offer_rollups).

Active/approved Offers (including mined inbound) seed proactive matches;
unverified (pending_review) and terminal (rejected/sold/won/expired) offers
are excluded so the tab only surfaces live stock.

Scoring: composite of ask recency (25%) + repeat demand (20%) + quote-vs-cost
spread (25%) + recent win (15%) + quantity fit (15%).

Called by: scheduler.py (background scan), routers/htmx/proactive.py (endpoints)
Depends on: models, config, services/proactive_helpers
"""

from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import settings
from ..constants import OfferStatus, ProactiveMatchSource, ProactiveMatchStatus, RequisitionStatus
from ..models import (
    ActivityLog,
    Company,
    CustomerSite,
    Offer,
    ProactiveMatch,
    Requirement,
    Requisition,
)
from ..models.config import SystemConfig
from ..models.purchase_history import CustomerPartHistory
from .proactive_helpers import build_batch_dno_set_multi, build_batch_throttle_set_multi

# Live-supply gate shared by the batch scan and the per-part rollup.
_LIVE_STATUSES = [OfferStatus.ACTIVE.value, OfferStatus.APPROVED.value]

# Per-user top-picks cache namespace (proactive_service.get_top_picks) — busted
# here whenever new matches land so a 24h-cached strip never hides fresh work.
# QC 2026-08-08: no trailing colon — invalidate_prefix appends ":*", so a
# trailing colon here produced "intel:proactive_picks::*" which matched
# nothing and left the 24h picks cache never invalidated.
PICKS_CACHE_PREFIX = "proactive_picks"


def _bust_picks_cache() -> None:
    """Invalidate every user's cached picks strip; tolerant of cache outages."""
    try:
        from ..cache.decorators import invalidate_prefix

        invalidate_prefix(PICKS_CACHE_PREFIX)
    except Exception as exc:  # cache down ≠ matching broken
        logger.warning("Picks cache invalidation failed: {}", exc)


# ── Part identity + windows ──────────────────────────────────────────────


def part_key(raw: str | None) -> str | None:
    """DISPLAY identity: verbatim string, trimmed + uppercased.

    Interior spacing/punctuation preserved so the user always sees the exact
    spelling on file. Pooling identity is the normalized key + the stored
    equivalence verdicts (see services/part_equivalence) — never a silent
    string merge; the source records keep their spellings, and pooled AI
    variants are flagged for a human to double-check.
    """
    if raw is None:
        return None
    s = str(raw).strip().upper()
    return s or None


def _requirement_window_start() -> datetime:
    """Start of the demand window — asks older than this do not seed matches."""
    return datetime.now(UTC) - timedelta(days=round(settings.proactive_requirement_window_months * 30.44))


def _offer_window_start() -> datetime:
    """Start of the supply window — offers older than this drop out of rollups."""
    return datetime.now(UTC) - timedelta(days=settings.proactive_offer_window_days)


# ── Supply rollup ────────────────────────────────────────────────────────


def compute_offer_rollups(db: Session, *, parts: set[str]) -> dict[str, dict]:
    """Batch rollup: aggregate live supply for many parts, equivalence-aware.

    Each part's rollup pools its whole equivalence class (2026-08-08):
    spellings sharing the same normalized key pool automatically (formatting
    variants), and keys the stored AI/human verdicts call 'same' pool as
    flagged AI variants — the UI color-codes those so a human double-checks.
    available_qty sums every live in-window offer including zero-priced ones;
    low_cost is the MIN over POSITIVE unit prices only ($0.00 means "price
    not provided", not free stock). Offers come back newest-first for
    drill-down display. ``variants`` maps each pooled non-canonical spelling
    to {qty, kind: formatting|ai, reason}.
    """
    from .part_equivalence import expand_parts, norm_key

    out: dict[str, dict] = {
        p: {
            "offers": [],
            "offer_count": 0,
            "available_qty": 0,
            "low_cost": None,
            "variants": {},
            "has_ai_variants": False,
        }
        for p in parts
    }
    if not parts:
        return out

    class_info = expand_parts(db, parts)
    all_keys = {k for info in class_info.values() for k in info["keys"]}
    if not all_keys:
        return out
    offers = (
        db.query(Offer)
        .filter(
            Offer.normalized_mpn.in_(all_keys),
            Offer.status.in_(_LIVE_STATUSES),
            Offer.created_at >= _offer_window_start(),
        )
        .order_by(Offer.created_at.desc())
        .all()
    )
    by_key: dict[str, list] = {}
    for o in offers:
        by_key.setdefault(o.normalized_mpn or norm_key(o.mpn), []).append(o)

    for p in parts:
        info = class_info[p]
        rollup = out[p]
        base_key = norm_key(p)
        for k in info["keys"]:
            for o in by_key.get(k, []):
                rollup["offers"].append(o)
                rollup["offer_count"] += 1
                rollup["available_qty"] += o.qty_available or 0
                if o.unit_price is not None and float(o.unit_price) > 0:
                    price = float(o.unit_price)
                    if rollup["low_cost"] is None or price < rollup["low_cost"]:
                        rollup["low_cost"] = price
                spelling = part_key(o.mpn) or ""
                if spelling and spelling != p:
                    kind = "formatting" if k == base_key else "ai"
                    variant = rollup["variants"].setdefault(
                        spelling,
                        {"qty": 0, "kind": kind, "reason": info["ai_variants"].get(k, "formatting variant")},
                    )
                    variant["qty"] += o.qty_available or 0
                    if kind == "ai":
                        rollup["has_ai_variants"] = True
    return out


def compute_offer_rollup(db: Session, *, part: str) -> dict:
    """Single-part convenience form of compute_offer_rollups."""
    return compute_offer_rollups(db, parts={part})[part]


# ── Scoring ──────────────────────────────────────────────────────────────


def _score_ask_recency(last_asked_at: datetime | None) -> int:
    """Newer asks score higher, but old asks FLOOR at 40 — an archived requirement that
    suddenly has supply is interesting, not stale."""
    if not last_asked_at:
        return 40
    days = (datetime.now(UTC) - last_asked_at.replace(tzinfo=UTC)).days
    if days <= 30:
        return 100
    if days <= 90:
        return 80
    if days <= 365:
        return 60
    return 40


def _score_repeat_demand(requirement_count: int) -> int:
    """A customer who asked seven times is a different conversation than one who asked
    once."""
    if requirement_count >= 5:
        return 100
    if requirement_count >= 3:
        return 80
    if requirement_count == 2:
        return 60
    return 40


def _score_quote_spread(quote_spread_pct: float | None) -> int:
    """Spread between our last quote and today's low cost — thin or negative ranks
    down."""
    if quote_spread_pct is None:
        return 50  # no quote on record = neutral
    if quote_spread_pct >= 30:
        return 100
    if quote_spread_pct >= 20:
        return 80
    if quote_spread_pct >= 10:
        return 60
    if quote_spread_pct > 0:
        return 40
    return 10


def _score_recent_win(recent_win: str | None) -> int:
    """recent_win: 'same_customer' | 'other_customer' | None (inside the price lookback)."""
    if recent_win == "same_customer":
        return 100
    if recent_win == "other_customer":
        return 80
    return 40


def _score_qty_fit(available_qty: int | None, last_asked_qty: int | None) -> int:
    """How much of the customer's last ask today's supply covers."""
    if not last_asked_qty or last_asked_qty <= 0:
        return 70  # ask size unknown = mild positive, supply exists
    covered = (available_qty or 0) / last_asked_qty
    if covered >= 1:
        return 100
    if covered >= 0.5:
        return 70
    return 40


def compute_match_score(
    *,
    last_asked_at: datetime | None,
    requirement_count: int,
    available_qty: int | None,
    last_asked_qty: int | None,
    quote_spread_pct: float | None = None,
    recent_win: str | None = None,
) -> int:
    """Composite match score (0-100).

    Weights: ask recency 25%, repeat demand 20%, quote-vs-cost spread 25%,
    recent win 15%, quantity fit 15%. Spread and win default to neutral until
    the caller supplies price-history anchors.
    """
    composite = int(
        _score_ask_recency(last_asked_at) * 0.25
        + _score_repeat_demand(requirement_count) * 0.20
        + _score_quote_spread(quote_spread_pct) * 0.25
        + _score_recent_win(recent_win) * 0.15
        + _score_qty_fit(available_qty, last_asked_qty) * 0.15
    )
    return min(100, max(0, composite))


def _score_margin(customer_avg_price: float | None, our_cost: float | None) -> tuple[int, float | None]:
    """Margin context vs the customer's historical average price (CPH).

    Returns (score, margin_pct). Retained for the margin display + the min-margin gate,
    which only applies when real purchase history exists — a requirement's target price
    is aspirational and never suppresses a match.
    """
    if not customer_avg_price or not our_cost or our_cost <= 0:
        return 50, None  # Unknown margin = neutral score
    margin_pct = (customer_avg_price - our_cost) / customer_avg_price * 100
    margin_pct = max(-100.0, min(1000.0, margin_pct))
    if margin_pct >= 30:
        return 100, round(margin_pct, 1)
    if margin_pct >= 20:
        return 80, round(margin_pct, 1)
    if margin_pct >= 10:
        return 60, round(margin_pct, 1)
    if margin_pct > 0:
        return 40, round(margin_pct, 1)
    return 10, round(margin_pct, 1)


# ── CPH aggregation helper (context signal, no longer the seed) ──────────


def _aggregate_cph_by_company(cph_rows: list) -> dict[int, dict]:
    """Collapse multiple CPH rows per company (different sources) into one record:

    summed purchase_count, max last_purchased_at, count-weighted avg price, and the
    most-recent row's last_unit_price.
    """
    by_company: dict[int, dict] = {}
    for cph in cph_rows:
        agg = by_company.get(cph.company_id)
        cnt = cph.purchase_count or 0
        if agg is None:
            by_company[cph.company_id] = {
                "count": cnt,
                "last_purchased_at": cph.last_purchased_at,
                "avg_price_num": (float(cph.avg_unit_price) * cnt) if cph.avg_unit_price else 0.0,
                "avg_price_den": cnt if cph.avg_unit_price else 0,
                "last_unit_price": float(cph.last_unit_price) if cph.last_unit_price else None,
            }
            continue
        agg["count"] += cnt
        if cph.avg_unit_price:
            agg["avg_price_num"] += float(cph.avg_unit_price) * cnt
            agg["avg_price_den"] += cnt
        if cph.last_purchased_at and (
            agg["last_purchased_at"] is None or cph.last_purchased_at > agg["last_purchased_at"]
        ):
            agg["last_purchased_at"] = cph.last_purchased_at
            if cph.last_unit_price:
                agg["last_unit_price"] = float(cph.last_unit_price)
    for agg in by_company.values():
        agg["avg_price"] = (agg["avg_price_num"] / agg["avg_price_den"]) if agg["avg_price_den"] else None
    return by_company


# ── Per-offer matching ───────────────────────────────────────────────────


def find_matches_for_offer(offer_id: int, db: Session) -> list[ProactiveMatch]:
    """Find customer matches for a single offer.

    Two seeding sources:
      1. Requirement history inside the requirement window (any requisition
         status) via ``_find_requirement_matches`` — the primary backbone.
      2. Active HOTLIST requisitions via ``_find_hotlist_matches`` — surfaces a
         part the customer never asked for recently but a salesperson
         explicitly monitors.

    Dedup stays one active match per (part, company). The requirement pass
    ``db.add()``s rows that are still uncommitted when the hotlist pass runs, so
    the hotlist pass's DB query can't see them — we pass the companies in via
    ``skip_company_ids`` so the same customer never gets two matches in one call.
    """
    offer = db.get(Offer, offer_id)
    part = part_key(offer.mpn) if offer else None
    if not offer or not part:
        return []
    req_matches = _find_requirement_matches(db, part=part, source_offer=offer)
    hot_matches = _find_hotlist_matches(
        db,
        part=part,
        our_cost=float(offer.unit_price) if offer.unit_price else None,
        source_offer=offer,
        skip_company_ids={m.company_id for m in req_matches if m.company_id},
    )
    return req_matches + hot_matches


def _existing_match_company_ids(db: Session, spellings: set[str]) -> set[int | None]:
    """Company ids (None = back-order lines) holding an active match under ANY spelling
    of the part's equivalence class."""
    if not spellings:
        return set()
    return {
        row[0]
        for row in db.query(ProactiveMatch.company_id)
        .filter(
            ProactiveMatch.mpn.in_(spellings),
            ProactiveMatch.status.in_([ProactiveMatchStatus.NEW, ProactiveMatchStatus.SENT]),
        )
        .all()
    }


def _find_requirement_matches(
    db: Session,
    *,
    part: str,
    source_offer: Offer,
) -> list[ProactiveMatch]:
    """Core seeding: one match per customer that asked for this part inside the window.

    Groups the customer's requirement rows into ONE line carrying
    requirement_count / last_asked_at / last_asked_qty; supply aggregates come
    from compute_offer_rollup at read time. Requirements on scratch requisitions
    are excluded (one-off search homes, not customer asks); HOTLIST requisitions
    are excluded here because _find_hotlist_matches owns them. Requirements with
    no customer account become back-order lines routed to the requisition owner
    (company_id NULL, deduped per owner).
    """
    # Lazy import (avoids an import-time cycle with activity_service); resolved once per call.
    from .activity_service import _update_last_activity
    from .part_equivalence import expand_part, observed_spellings
    from .pricing_history import last_quote_for_part, last_win_for_part

    rollup = compute_offer_rollup(db, part=part)
    if not rollup["offer_count"]:
        return []
    our_cost = rollup["low_cost"] or (float(source_offer.unit_price) if source_offer.unit_price else None)

    # Equivalence class: pooled keys for demand joins + every observed spelling
    # for suppression/dedup (throttle, do-not-offer, active-match) so a variant
    # spelling can never sidestep a suppression.
    eq = expand_part(db, part)
    class_keys = set(eq["keys"])
    class_spellings = {part} | {s for group in observed_spellings(db, class_keys).values() for s in group}

    # Price anchors (D5): computed once per part; spread vs today's low cost feeds
    # the score, same-customer wins resolved per company inside the loop.
    quote_anchor = last_quote_for_part(db, part=part)
    win_anchor = last_win_for_part(db, part=part)
    quote_spread_pct: float | None = None
    if quote_anchor and our_cost and quote_anchor["price"]:
        quote_spread_pct = (quote_anchor["price"] - our_cost) / quote_anchor["price"] * 100

    rows = (
        db.query(Requirement, Requisition, CustomerSite)
        .join(Requisition, Requirement.requisition_id == Requisition.id)
        .outerjoin(CustomerSite, CustomerSite.id == Requisition.customer_site_id)
        .filter(
            Requirement.normalized_mpn.in_(class_keys),
            Requirement.created_at >= _requirement_window_start(),
            Requisition.is_scratch.is_(False),
            Requisition.status != RequisitionStatus.HOTLIST.value,
        )
        .order_by(Requirement.created_at.desc())
        .all()
    )
    if not rows:
        return []

    # ── Group asks per customer (newest first). Customer identity comes from
    # requisition.company_id, else the requisition's site. Key: company_id, or
    # ("backorder", owner_id) for requirements with no customer account. ──
    groups: dict[object, dict] = {}
    for req_item, requisition, ask_site in rows:
        row_company_id = requisition.company_id or (ask_site.company_id if ask_site else None)
        if row_company_id:
            key: object = row_company_id
        elif requisition.created_by:
            key = ("backorder", requisition.created_by)
        else:
            continue  # no customer AND no owner — nowhere to route
        grp = groups.setdefault(
            key,
            {
                "company_id": row_company_id,
                "owner_id": requisition.created_by,
                "count": 0,
                "newest_req": req_item,
                "newest_requisition": requisition,
                "newest_site": ask_site,
            },
        )
        grp["count"] += 1

    company_ids = {g["company_id"] for g in groups.values() if g["company_id"]}
    companies = {c.id: c for c in db.query(Company).filter(Company.id.in_(company_ids)).all()} if company_ids else {}

    # First active site per company (throttle scope + navigation), preferring
    # the newest ask's own site.
    sites: dict[int, CustomerSite] = {}
    if company_ids:
        for s in (
            db.query(CustomerSite)
            .filter(CustomerSite.company_id.in_(company_ids), CustomerSite.is_active.is_(True))
            .all()
        ):
            sites.setdefault(s.company_id, s)

    dno_company_ids = build_batch_dno_set_multi(db, class_spellings, company_ids) if company_ids else set()
    throttled_site_ids = (
        build_batch_throttle_set_multi(db, class_spellings, {s.id for s in sites.values()}) if sites else set()
    )
    existing = _existing_match_company_ids(db, class_spellings)

    # CPH context (purchase history as a signal, not the seed).
    card_ids = {source_offer.material_card_id} | {g["newest_req"].material_card_id for g in groups.values()}
    card_ids.discard(None)
    cph_by_company: dict[int, dict] = {}
    if company_ids and card_ids:
        cph_rows = (
            db.query(CustomerPartHistory)
            .filter(
                CustomerPartHistory.company_id.in_(company_ids),
                CustomerPartHistory.material_card_id.in_(card_ids),
            )
            .all()
        )
        cph_by_company = _aggregate_cph_by_company(cph_rows)

    min_margin = settings.proactive_min_margin_pct
    matches: list[ProactiveMatch] = []
    backorder_owners_matched = {
        g["owner_id"]
        for k, g in groups.items()
        if isinstance(k, tuple)
        and db.query(ProactiveMatch.id)
        .filter(
            ProactiveMatch.mpn.in_(class_spellings),
            ProactiveMatch.company_id.is_(None),
            ProactiveMatch.salesperson_id == g["owner_id"],
            ProactiveMatch.status.in_([ProactiveMatchStatus.NEW, ProactiveMatchStatus.SENT]),
        )
        .first()
        is not None
    }

    for key, grp in groups.items():
        company_id = grp["company_id"]
        newest_req = grp["newest_req"]
        newest_requisition = grp["newest_requisition"]

        if company_id:
            company = companies.get(company_id)
            if not company:
                continue
            # Salesperson: account owner, else the newest ask's requisition owner.
            salesperson_id = company.account_owner_id or grp["owner_id"]
            if not salesperson_id:
                continue
            if company_id in existing or company_id in dno_company_ids:
                continue
            site = grp["newest_site"] or sites.get(company_id)
            if site and site.id in throttled_site_ids:
                continue
        else:
            salesperson_id = grp["owner_id"]
            if salesperson_id in backorder_owners_matched:
                continue
            company = None
            site = None

        cph = cph_by_company.get(company_id) if company_id else None
        margin_score_unused, margin_pct = _score_margin(cph["avg_price"] if cph else None, our_cost)
        # Min-margin gate only when real purchase history prices the customer.
        if margin_pct is not None and margin_pct < min_margin:
            continue

        recent_win = None
        if win_anchor:
            recent_win = "same_customer" if company_id and win_anchor["company_id"] == company_id else "other_customer"
        score = compute_match_score(
            last_asked_at=newest_req.created_at,
            requirement_count=grp["count"],
            available_qty=rollup["available_qty"],
            last_asked_qty=newest_req.target_qty,
            quote_spread_pct=quote_spread_pct,
            recent_win=recent_win,
        )

        match = ProactiveMatch(
            offer_id=source_offer.id,
            requirement_id=newest_req.id,
            requisition_id=newest_requisition.id,
            customer_site_id=site.id if site else None,
            salesperson_id=salesperson_id,
            # Display identity = the customer's own newest spelling; equivalence
            # classes make the variants reachable from it.
            mpn=part_key(newest_req.primary_mpn) or part,
            material_card_id=source_offer.material_card_id or newest_req.material_card_id,
            company_id=company_id,
            match_score=score,
            margin_pct=margin_pct,
            customer_purchase_count=cph["count"] if cph else 0,
            customer_last_price=cph["last_unit_price"] if cph else None,
            customer_last_purchased_at=cph["last_purchased_at"] if cph else None,
            our_cost=our_cost,
            match_source=ProactiveMatchSource.REQUIREMENT,
            requirement_count=grp["count"],
            last_asked_at=newest_req.created_at,
            last_asked_qty=newest_req.target_qty,
        )
        db.add(match)
        matches.append(match)
        if company_id:
            existing.add(company_id)
        else:
            backorder_owners_matched.add(salesperson_id)

        db.add(
            ActivityLog(
                user_id=salesperson_id,
                activity_type="proactive_match",
                channel="system",
                requisition_id=newest_requisition.id,
                company_id=company_id,
                contact_name=company.name if company else "Trio Back Order",
                subject=f"Proactive match: {part} — {company.name if company else 'Trio Back Order'} (score {score})",
            )
        )
        if company_id:
            _update_last_activity({"type": "company", "id": company_id}, db)

    return matches


def _find_hotlist_matches(
    db: Session,
    *,
    part: str,
    our_cost: float | None,
    source_offer: Offer | None,
    skip_company_ids: set[int] | None = None,
) -> list[ProactiveMatch]:
    """Seed ProactiveMatch rows from active HOTLIST requisitions for this part.

    Unlike the requirement path this has NO time window — a hotlist is an
    explicit standing request to monitor a part for a customer. Reuses the
    same suppression + dedup + surface pipeline. Salesperson falls back to the
    hotlist requisition's owner when the company has no account owner.

    ``skip_company_ids`` carries the company_ids the requirement pass already
    produced in THIS call (their ``db.add()``s are uncommitted, so a fresh DB
    query won't see them) — union them into the existing-match set so dedup
    holds across both passes.
    """
    from .part_equivalence import expand_part, observed_spellings

    fallback_offer_id = source_offer.id if source_offer else None
    if not fallback_offer_id:
        return []

    eq = expand_part(db, part)
    class_keys = set(eq["keys"])
    class_spellings = {part} | {s for group in observed_spellings(db, class_keys).values() for s in group}

    rows = (
        db.query(Requisition, CustomerSite, Company)
        .join(Requirement, Requirement.requisition_id == Requisition.id)
        .join(CustomerSite, CustomerSite.id == Requisition.customer_site_id)
        .join(Company, Company.id == func.coalesce(Requisition.company_id, CustomerSite.company_id))
        .filter(
            Requisition.status == RequisitionStatus.HOTLIST.value,
            Requirement.normalized_mpn.in_(class_keys),
            CustomerSite.is_active.is_(True),
        )
        .all()
    )
    if not rows:
        return []

    existing = _existing_match_company_ids(db, class_spellings)
    existing |= skip_company_ids or set()

    dno = build_batch_dno_set_multi(db, class_spellings, {c.id for _, _, c in rows})

    out: list[ProactiveMatch] = []
    for req, site, company in rows:
        salesperson_id = company.account_owner_id or req.created_by
        if not salesperson_id:
            continue
        if company.id in existing or company.id in dno:
            continue
        match = ProactiveMatch(
            offer_id=fallback_offer_id,
            requirement_id=None,
            requisition_id=req.id,
            customer_site_id=site.id,
            salesperson_id=salesperson_id,
            mpn=part,
            material_card_id=source_offer.material_card_id if source_offer else None,
            company_id=company.id,
            match_score=60,  # baseline — explicit monitor request, no ask history to weight
            margin_pct=None,
            customer_purchase_count=0,
            our_cost=our_cost,
            match_source=ProactiveMatchSource.HOTLIST,
            requirement_count=0,
        )
        db.add(match)
        out.append(match)
        existing.add(company.id)
        db.add(
            ActivityLog(
                user_id=salesperson_id,
                activity_type="proactive_match",
                channel="system",
                requisition_id=req.id,
                company_id=company.id,
                contact_name=company.name,
                subject=f"Hotlist match: {part} — {company.name}",
            )
        )
    return out


_WATERMARK_KEY = "proactive_last_scan"


def _get_watermark(db: Session) -> datetime:
    """Get last scan timestamp from SystemConfig (survives restarts)."""
    row = db.query(SystemConfig).filter(SystemConfig.key == _WATERMARK_KEY).first()
    if row and row.value:
        try:
            ts = datetime.fromisoformat(row.value)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            return ts
        except (ValueError, TypeError):
            pass
    return datetime.now(UTC) - timedelta(hours=settings.proactive_scan_interval_hours)


def _set_watermark(db: Session, ts: datetime):
    """Persist scan watermark to SystemConfig."""
    row = db.query(SystemConfig).filter(SystemConfig.key == _WATERMARK_KEY).first()
    if row:
        row.value = ts.isoformat()
    else:
        db.add(SystemConfig(key=_WATERMARK_KEY, value=ts.isoformat(), description="Proactive scan watermark"))
    db.flush()


# ── Batch scan ───────────────────────────────────────────────────────────


def run_proactive_scan(db: Session) -> dict:
    """Batch scan: find matches for all new offers since last run.

    Called by scheduler. Returns {scanned_offers, matches_created}.
    Watermark is persisted to SystemConfig so it survives restarts.
    """
    since = _get_watermark(db)

    # Oldest-first so the limit processes chronologically.
    # Gate to live offers only: pending_review/rejected/sold/won/expired are excluded.
    # An offer created as pending_review (excluded here) that is later approved would
    # never be picked up by this batch scan once the watermark advances past its
    # created_at — trigger_rematch_on_offer_approval() below closes that gap with a
    # targeted single-offer re-match, called from the offer-approval routers.
    new_offers = (
        db.query(Offer)
        .filter(
            Offer.created_at > since,
            Offer.status.in_(_LIVE_STATUSES),
        )
        .order_by(Offer.created_at.asc())
        .limit(5000)
        .all()
    )

    if len(new_offers) >= 5000:
        logger.warning("Proactive scan hit 5000-offer cap — remaining will be picked up next run")

    total_matches = 0

    # Deduplicate: don't scan the same part twice in one run
    from .part_equivalence import norm_key

    scanned_parts: set[str] = set()

    for offer in new_offers:
        part = part_key(offer.mpn)
        scan_key = norm_key(offer.mpn)
        if not part or not scan_key or scan_key in scanned_parts:
            continue
        scanned_parts.add(scan_key)
        matches = find_matches_for_offer(offer.id, db)
        total_matches += len(matches)

    # Advance watermark to last processed offer (not now) so capped runs resume correctly
    if new_offers:
        _set_watermark(db, new_offers[-1].created_at)

    try:
        db.commit()
    except Exception as e:
        logger.error("Failed to commit proactive matches: {}", e)
        db.rollback()
        return {
            "scanned_offers": len(new_offers),
            "matches_created": 0,
        }

    if total_matches:
        _bust_picks_cache()
    logger.info(
        "Proactive scan: {} offers → {} matches",
        len(new_offers),
        total_matches,
    )
    return {
        "scanned_offers": len(new_offers),
        "matches_created": total_matches,
    }


def trigger_rematch_on_offer_approval(db: Session, offer: Offer) -> int:
    """Targeted single-offer re-match, called by offer-approval routers.

    Closes the watermark gap documented in run_proactive_scan(): a batch scan only ever
    sees an offer once, at Offer.created_at. An offer created as pending_review is
    excluded from the scan's live-status filter; if it's approved after the watermark
    has advanced past its created_at, it's invisible to every future batch scan too.
    This runs find_matches_for_offer() for that one offer instead of waiting for (or
    forcing) a full rescan.

    Uses its own commit so a re-match failure never blocks the caller's approval
    transaction — the offer status change should succeed even if matching fails. No-op
    for offers without a part number.
    """
    if not part_key(offer.mpn):
        return 0
    try:
        matches = find_matches_for_offer(offer.id, db)
        db.commit()
    except Exception as exc:
        logger.error("Targeted re-match failed for offer {}: {}", offer.id, exc)
        db.rollback()
        return 0
    if matches:
        _bust_picks_cache()
        logger.info("Targeted re-match: offer {} approval → {} matches", offer.id, len(matches))
    return len(matches)


# ── Match actions ────────────────────────────────────────────────────────


def dismiss_match(match_id: int, user_id: int, reason: str, db: Session) -> None:
    """Dismiss a proactive match — salesperson says 'not interested'."""
    match = db.get(ProactiveMatch, match_id)
    if not match:
        raise ValueError("Match not found")
    if match.salesperson_id != user_id:
        raise ValueError("Not your match")
    match.status = ProactiveMatchStatus.DISMISSED
    match.dismiss_reason = reason
    db.commit()


def mark_match_sent(match_id: int, user_id: int, db: Session) -> None:
    """Mark a match as sent after email delivery."""
    match = db.get(ProactiveMatch, match_id)
    if not match:
        raise ValueError("Match not found")
    if match.salesperson_id != user_id:
        raise ValueError("Not your match")
    match.status = ProactiveMatchStatus.SENT
    db.commit()


def expire_old_matches(db: Session) -> int:
    """Expire matches older than proactive_match_expiry_days.

    Uses single UPDATE instead of load-then-loop. Returns count expired.
    """
    cutoff = datetime.now(UTC) - timedelta(days=settings.proactive_match_expiry_days)
    count = (
        db.query(ProactiveMatch)
        .filter(
            ProactiveMatch.status == ProactiveMatchStatus.NEW,
            ProactiveMatch.created_at < cutoff,
        )
        .update({"status": ProactiveMatchStatus.EXPIRED}, synchronize_session=False)
    )
    if count:
        db.commit()
    return count
