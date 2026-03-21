"""Proactive matching engine — finds customer matches for new inventory.

Uses customer_part_history (CPH) as the primary matching backbone.
Only confirmed buyer-entered Offers trigger proactive matches.

Scoring: composite of recency (40%) + frequency (30%) + margin potential (30%).

Called by: scheduler.py (background scan), routers/proactive.py (endpoints)
Depends on: models, config, services/proactive_helpers
"""

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy.orm import Session

from ..config import settings
from ..constants import ProactiveMatchStatus
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
from ..utils.normalization import normalize_mpn
from .proactive_helpers import build_batch_dno_set, build_batch_throttle_set

# ── Scoring ──────────────────────────────────────────────────────────────


def _score_recency(last_purchased_at: datetime | None) -> int:
    """Score 0-100 based on how recently the customer bought this part."""
    if not last_purchased_at:
        return 20
    days = (datetime.now(timezone.utc) - last_purchased_at.replace(tzinfo=timezone.utc)).days
    if days <= 180:
        return 100
    if days <= 365:
        return 80
    if days <= 730:
        return 60
    return 40


def _score_frequency(purchase_count: int) -> int:
    """Score 0-100 based on number of purchases."""
    if purchase_count >= 5:
        return 100
    if purchase_count >= 3:
        return 80
    if purchase_count >= 2:
        return 60
    return 40


def _score_margin(customer_avg_price: float | None, our_cost: float | None) -> tuple[int, float | None]:
    """Score 0-100 based on margin potential.

    Returns (score, margin_pct).
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


def compute_match_score(
    last_purchased_at: datetime | None,
    purchase_count: int,
    customer_avg_price: float | None,
    our_cost: float | None,
) -> tuple[int, float | None]:
    """Composite match score (0-100) and margin percentage.

    Weights: recency 40%, frequency 30%, margin 30%.
    """
    recency = _score_recency(last_purchased_at)
    frequency = _score_frequency(purchase_count)
    margin_score, margin_pct = _score_margin(customer_avg_price, our_cost)
    composite = int(recency * 0.4 + frequency * 0.3 + margin_score * 0.3)
    return min(100, max(0, composite)), margin_pct


# ── Per-offer matching ───────────────────────────────────────────────────


def find_matches_for_offer(offer_id: int, db: Session) -> list[ProactiveMatch]:
    """Find customer matches for a single offer via CPH."""
    offer = db.get(Offer, offer_id)
    if not offer or not offer.material_card_id:
        return []
    return _find_matches(
        db,
        material_card_id=offer.material_card_id,
        mpn=offer.mpn or "",
        our_cost=float(offer.unit_price) if offer.unit_price else None,
        source_offer=offer,
    )


_WATERMARK_KEY = "proactive_last_scan"


def _get_watermark(db: Session) -> datetime:
    """Get last scan timestamp from SystemConfig (survives restarts)."""
    row = db.query(SystemConfig).filter(SystemConfig.key == _WATERMARK_KEY).first()
    if row and row.value:
        try:
            ts = datetime.fromisoformat(row.value)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts
        except (ValueError, TypeError):
            pass
    return datetime.now(timezone.utc) - timedelta(hours=settings.proactive_scan_interval_hours)


def _set_watermark(db: Session, ts: datetime):
    """Persist scan watermark to SystemConfig."""
    row = db.query(SystemConfig).filter(SystemConfig.key == _WATERMARK_KEY).first()
    if row:
        row.value = ts.isoformat()
    else:
        db.add(SystemConfig(key=_WATERMARK_KEY, value=ts.isoformat(), description="Proactive scan watermark"))
    db.flush()


def _find_matches(
    db: Session,
    *,
    material_card_id: int,
    mpn: str,
    our_cost: float | None,
    source_offer: Offer | None = None,
) -> list[ProactiveMatch]:
    """Core matching logic — query CPH, score, create ProactiveMatch records.

    Uses batch-loaded lookups to avoid N+1 queries. Tightened dedup: material_card_id +
    company_id only (no offer_id filter). requirement_id and requisition_id are nullable
    — matches without historical requisitions are valid.
    """
    min_margin = settings.proactive_min_margin_pct
    mpn_upper = normalize_mpn(mpn) or mpn.upper().strip()

    # Find all CPH entries for this part
    cph_rows = db.query(CustomerPartHistory).filter(CustomerPartHistory.material_card_id == material_card_id).all()
    if not cph_rows:
        return []

    # ── Batch pre-load all needed data (fixes N+1) ──────────────────
    company_ids = {cph.company_id for cph in cph_rows}

    # 1. Companies (with account_owner_id check)
    companies = {c.id: c for c in db.query(Company).filter(Company.id.in_(company_ids)).all()}

    # 2. First active site per company
    sites: dict[int, CustomerSite] = {}
    for s in (
        db.query(CustomerSite)
        .filter(CustomerSite.company_id.in_(company_ids), CustomerSite.is_active == True)  # noqa: E712
        .all()
    ):
        sites.setdefault(s.company_id, s)

    # 3. Do-not-offer suppression set
    dno_company_ids = build_batch_dno_set(db, mpn_upper, company_ids)

    # 4. Throttled site IDs
    site_ids = {s.id for s in sites.values()}
    throttled_site_ids = build_batch_throttle_set(db, mpn_upper, site_ids)

    # 5. Existing active match company IDs (tightened dedup — no offer_id filter)
    existing_match_company_ids = {
        row[0]
        for row in db.query(ProactiveMatch.company_id)
        .filter(
            ProactiveMatch.material_card_id == material_card_id,
            ProactiveMatch.status.in_([ProactiveMatchStatus.NEW, ProactiveMatchStatus.SENT]),
        )
        .all()
    }

    # 6. Requisition history per site (optional — nullable FKs)
    req_by_site: dict[int, tuple] = {}
    for req_item, requisition in (
        db.query(Requirement, Requisition)
        .join(Requisition, Requirement.requisition_id == Requisition.id)
        .filter(
            Requirement.material_card_id == material_card_id,
            Requisition.customer_site_id.in_(site_ids),
        )
        .order_by(Requisition.created_at.desc())
        .all()
    ):
        req_by_site.setdefault(requisition.customer_site_id, (req_item, requisition))

    # 7. Fallback offer (queried once, not per-row — fixes N+1)
    fallback_offer_id: int | None = None
    if source_offer:
        fallback_offer_id = source_offer.id
    else:
        fallback_offer = (
            db.query(Offer.id)
            .filter(Offer.material_card_id == material_card_id)
            .order_by(Offer.created_at.desc())
            .first()
        )
        if fallback_offer:
            fallback_offer_id = fallback_offer[0]

    if not fallback_offer_id:
        return []

    # ── Loop uses dict lookups instead of queries ────────────────────
    matches = []
    for cph in cph_rows:
        company = companies.get(cph.company_id)
        if not company or not company.account_owner_id:
            continue

        site = sites.get(cph.company_id)
        if not site:
            continue

        # Check do-not-offer
        if cph.company_id in dno_company_ids:
            continue

        # Check throttle
        if site.id in throttled_site_ids:
            continue

        # Dedup: one active match per part per customer
        if cph.company_id in existing_match_company_ids:
            continue

        # Score the match
        avg_price = float(cph.avg_unit_price) if cph.avg_unit_price else None
        score, margin_pct = compute_match_score(
            cph.last_purchased_at,
            cph.purchase_count or 0,
            avg_price,
            our_cost,
        )

        # Filter by minimum margin if we can calculate it
        if margin_pct is not None and margin_pct < min_margin:
            continue

        # Optional: requisition history (nullable FKs)
        req_row = req_by_site.get(site.id)
        requirement_id = req_row[0].id if req_row else None
        requisition_id = req_row[1].id if req_row else None

        last_price = float(cph.last_unit_price) if cph.last_unit_price else None

        match = ProactiveMatch(
            offer_id=fallback_offer_id,
            requirement_id=requirement_id,
            requisition_id=requisition_id,
            customer_site_id=site.id,
            salesperson_id=company.account_owner_id,
            mpn=mpn_upper,
            material_card_id=material_card_id,
            company_id=cph.company_id,
            match_score=score,
            margin_pct=margin_pct,
            customer_purchase_count=cph.purchase_count or 0,
            customer_last_price=last_price,
            customer_last_purchased_at=cph.last_purchased_at,
            our_cost=our_cost,
        )
        db.add(match)
        matches.append(match)

        # Track for dedup within this batch
        existing_match_company_ids.add(cph.company_id)

        # In-app notification
        db.add(
            ActivityLog(
                user_id=company.account_owner_id,
                activity_type="proactive_match",
                channel="system",
                requisition_id=requisition_id,
                contact_name=company.name,
                subject=f"Proactive match: {mpn_upper} — {company.name} (score {score})",
            )
        )

    return matches


# ── Batch scan ───────────────────────────────────────────────────────────


def run_proactive_scan(db: Session) -> dict:
    """Batch scan: find matches for all new offers since last run.

    Called by scheduler. Returns {scanned_offers, matches_created}.
    Watermark is persisted to SystemConfig so it survives restarts.
    """
    since = _get_watermark(db)

    # Oldest-first so the limit processes chronologically
    new_offers = (
        db.query(Offer)
        .filter(
            Offer.created_at > since,
            Offer.material_card_id.isnot(None),
        )
        .order_by(Offer.created_at.asc())
        .limit(5000)
        .all()
    )

    if len(new_offers) >= 5000:
        logger.warning("Proactive scan hit 5000-offer cap — remaining will be picked up next run")

    total_matches = 0

    # Deduplicate: don't scan the same material_card_id twice
    scanned_cards: set[int] = set()

    for offer in new_offers:
        if offer.material_card_id in scanned_cards:
            continue
        scanned_cards.add(offer.material_card_id)
        matches = find_matches_for_offer(offer.id, db)
        total_matches += len(matches)

    # Advance watermark to last processed offer (not now) so capped runs resume correctly
    if new_offers:
        _set_watermark(db, new_offers[-1].created_at)

    try:
        db.commit()
    except Exception as e:
        logger.error("Failed to commit proactive matches: %s", e)
        db.rollback()
        return {
            "scanned_offers": len(new_offers),
            "matches_created": 0,
        }

    logger.info(
        "Proactive scan: %d offers → %d matches",
        len(new_offers),
        total_matches,
    )
    return {
        "scanned_offers": len(new_offers),
        "matches_created": total_matches,
    }


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
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.proactive_match_expiry_days)
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
