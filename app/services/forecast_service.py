"""Pipeline / forecast service — requisition-as-opportunity rollups.

The Requisition IS the opportunity (locked CRM decision — no separate deal
object). Forecast = sum(deal_value * stage win-probability) over open
requisitions. Account- and owner-level rollups plus an
interactions -> RFQs -> quotes -> orders conversion funnel.

Forecast dollars reuse the canonical _resolve_deal_value so they reconcile with
what the requisition list shows per row.

Called by: app/routers/htmx_views.py parts_workspace_partial (the Sales Hub /
           parts workspace) — pipeline_summary feeds the pipeline chip there.
Depends on: app.models (Requisition, Requirement, Quote, Company, User),
            app.services.requisition_list_service._resolve_deal_value
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import case
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from app.constants import RequisitionStatus
from app.models import Company, Quote, Requirement, Requisition, User
from app.services.requisition_list_service import _resolve_deal_value

# Stage -> win-probability, keyed on the canonical RequisitionStatus pipeline
# (Sales Hub: DRAFT -> OPEN -> RFQS_SENT -> OFFERS -> QUOTED -> WON/LOST).
# Standard CRM stage-weighting; tune to match real close rates. Terminal stages:
# won=1.0 (realized), lost/cancelled=0.0 (dead). HOTLIST is an off-pipeline
# *monitor* state (RequisitionStatus.MONITOR) with NO win probability — it is
# deliberately absent here so it never enters OPEN_STATUSES, the open-deal count,
# the open value, or the weighted forecast. This constant is the single
# forecasting lever — adjust it (not the call sites) if observed close rates differ.
STAGE_WIN_PROBABILITY: dict[str, float] = {
    RequisitionStatus.DRAFT: 0.05,
    RequisitionStatus.OPEN: 0.10,
    RequisitionStatus.RFQS_SENT: 0.25,
    RequisitionStatus.OFFERS: 0.40,
    RequisitionStatus.QUOTED: 0.75,
    RequisitionStatus.WON: 1.00,
    RequisitionStatus.LOST: 0.0,
    RequisitionStatus.CANCELLED: 0.0,
}

# Open = non-terminal = statuses with a live (0 < p < 1) probability, kept in
# lifecycle order for stable display. HOTLIST (monitor) and the terminal stages
# are excluded by construction.
_OPEN_ORDER: list[str] = [
    RequisitionStatus.DRAFT,
    RequisitionStatus.OPEN,
    RequisitionStatus.RFQS_SENT,
    RequisitionStatus.OFFERS,
    RequisitionStatus.QUOTED,
]
OPEN_STATUSES: frozenset[str] = frozenset(s for s, p in STAGE_WIN_PROBABILITY.items() if 0.0 < p < 1.0)

_STAGE_LABELS: dict[str, str] = {
    RequisitionStatus.DRAFT: "Draft",
    RequisitionStatus.OPEN: "Open",
    RequisitionStatus.RFQS_SENT: "RFQs Sent",
    RequisitionStatus.OFFERS: "Offers",
    RequisitionStatus.QUOTED: "Quoted",
}


def stage_probability(status: str | None) -> float:
    """Win-probability for a requisition status (unknown/None -> 0.0)."""
    return STAGE_WIN_PROBABILITY.get(status or "", 0.0)


def bulk_deal_values(db: Session, req_ids: list[int]) -> dict[int, float]:
    """Resolved deal value per requisition id, reusing _resolve_deal_value.

    Computes opportunity_value, requirement count, priced sum and priced count in BULK
    (grouped queries over Requirement) so forecast dollars reconcile with the
    requisition list. priced_sum mirrors the list service exactly: sum(target_price *
    target_qty) over requirements with a non-null target_price. Returns 0.0 where
    _resolve_deal_value yields None.
    """
    if not req_ids:
        return {}

    opp_by_req: dict[int, float | None] = {
        rid: (float(ov) if ov else None)
        for rid, ov in db.query(Requisition.id, Requisition.opportunity_value).filter(Requisition.id.in_(req_ids)).all()
    }

    agg_by_req: dict[int, tuple[int, float, int]] = {}
    rows = (
        db.query(
            Requirement.requisition_id,
            sqlfunc.count(Requirement.id),
            sqlfunc.coalesce(
                sqlfunc.sum(
                    case(
                        (
                            Requirement.target_price.isnot(None),
                            Requirement.target_price * Requirement.target_qty,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
            sqlfunc.count(Requirement.target_price),
        )
        .filter(Requirement.requisition_id.in_(req_ids))
        .group_by(Requirement.requisition_id)
        .all()
    )
    for rid, req_cnt, priced_sum, priced_count in rows:
        agg_by_req[rid] = (int(req_cnt), float(priced_sum or 0), int(priced_count))

    out: dict[int, float] = {}
    for rid in req_ids:
        req_cnt, priced_sum, priced_count = agg_by_req.get(rid, (0, 0.0, 0))
        value, _src = _resolve_deal_value(opp_by_req.get(rid), priced_sum, priced_count, req_cnt)
        out[rid] = float(value) if value else 0.0
    return out


def pipeline_summary(db: Session, *, owner_id: int | None = None) -> dict:
    """Open pipeline, weighted forecast, won/lost and win-rate for the period.

    owner_id, when given, scopes every figure to requisitions claimed by that user.
    by_stage covers OPEN stages only, in lifecycle order.
    """
    base = db.query(Requisition)
    if owner_id is not None:
        base = base.filter(Requisition.claimed_by_id == owner_id)

    open_reqs = base.filter(Requisition.status.in_(OPEN_STATUSES)).all()
    deal_values = bulk_deal_values(db, [r.id for r in open_reqs])

    by_stage_acc: dict[str, dict] = {
        s: {"status": s, "label": _STAGE_LABELS[s], "count": 0, "value": 0.0, "weighted": 0.0} for s in _OPEN_ORDER
    }
    open_value = 0.0
    weighted_value = 0.0
    for r in open_reqs:
        val = deal_values.get(r.id, 0.0)
        prob = stage_probability(r.status)
        weighted = val * prob
        open_value += val
        weighted_value += weighted
        bucket = by_stage_acc.get(r.status)
        if bucket is not None:
            bucket["count"] += 1
            bucket["value"] += val
            bucket["weighted"] += weighted

    won_reqs = base.filter(Requisition.status == RequisitionStatus.WON).all()
    won_count = len(won_reqs)
    lost_count = base.filter(Requisition.status == RequisitionStatus.LOST).count()
    won_values = bulk_deal_values(db, [r.id for r in won_reqs])
    won_value = sum(won_values.values())
    decided = won_count + lost_count
    win_rate = (won_count / decided) if decided else 0.0

    return {
        "open_value": open_value,
        "weighted_value": weighted_value,
        "open_count": len(open_reqs),
        "won_value": won_value,
        "won_count": won_count,
        "lost_count": lost_count,
        "win_rate": win_rate,
        "by_stage": [by_stage_acc[s] for s in _OPEN_ORDER],
    }


def pipeline_by_account(db: Session, *, limit: int = 10) -> list[dict]:
    """Top accounts by weighted open pipeline.

    Requisitions with no company_id are skipped (they have no account to roll up under).
    Sorted by weighted_value desc, capped at limit.
    """
    open_reqs = (
        db.query(Requisition).filter(Requisition.status.in_(OPEN_STATUSES), Requisition.company_id.isnot(None)).all()
    )
    deal_values = bulk_deal_values(db, [r.id for r in open_reqs])
    by_company: dict[int, dict] = {}
    for r in open_reqs:
        val = deal_values.get(r.id, 0.0)
        acc = by_company.setdefault(
            r.company_id,
            {"company_id": r.company_id, "company_name": "", "open_count": 0, "open_value": 0.0, "weighted_value": 0.0},
        )
        acc["open_count"] += 1
        acc["open_value"] += val
        acc["weighted_value"] += val * stage_probability(r.status)

    if by_company:
        names = dict(db.query(Company.id, Company.name).filter(Company.id.in_(list(by_company.keys()))).all())
        for cid, acc in by_company.items():
            acc["company_name"] = names.get(cid) or f"Account #{cid}"

    ranked = sorted(by_company.values(), key=lambda a: a["weighted_value"], reverse=True)
    return ranked[:limit]


def pipeline_by_owner(db: Session) -> list[dict]:
    """Owner leaderboard by weighted open pipeline.

    Owner = claimed_by_id; requisitions with no claimer roll up under a single
    "Unassigned" bucket (owner_id=None). Sorted by weighted_value desc.
    """
    open_reqs = db.query(Requisition).filter(Requisition.status.in_(OPEN_STATUSES)).all()
    open_values = bulk_deal_values(db, [r.id for r in open_reqs])
    won_reqs = db.query(Requisition).filter(Requisition.status == RequisitionStatus.WON).all()
    won_values = bulk_deal_values(db, [r.id for r in won_reqs])

    by_owner: dict[int | None, dict] = {}

    def _bucket(owner_id: int | None) -> dict:
        return by_owner.setdefault(
            owner_id,
            {
                "owner_id": owner_id,
                "owner_name": "Unassigned",
                "open_count": 0,
                "open_value": 0.0,
                "weighted_value": 0.0,
                "won_value": 0.0,
            },
        )

    for r in open_reqs:
        val = open_values.get(r.id, 0.0)
        acc = _bucket(r.claimed_by_id)
        acc["open_count"] += 1
        acc["open_value"] += val
        acc["weighted_value"] += val * stage_probability(r.status)
    for r in won_reqs:
        _bucket(r.claimed_by_id)["won_value"] += won_values.get(r.id, 0.0)

    real_ids = [oid for oid in by_owner if oid is not None]
    if real_ids:
        owner_names = {
            uid: (name or email)
            for uid, name, email in db.query(User.id, User.name, User.email).filter(User.id.in_(real_ids)).all()
        }
        for oid in real_ids:
            by_owner[oid]["owner_name"] = owner_names.get(oid) or f"User #{oid}"

    return sorted(by_owner.values(), key=lambda a: a["weighted_value"], reverse=True)


def conversion_funnel(db: Session, *, days: int = 90) -> dict:
    """Interactions -> RFQs -> quotes -> orders funnel over a recent window.

    Counts requisitions created within `days`:
      - opportunities: all reqs created in window
      - sourcing:      progressed past entry (status not in {draft, open}; OPEN is
                       the entry stage so it does not yet count as "reached sourcing+")
      - quoted:        has >=1 Quote OR status in {quoted, won}
      - won:           status == won
    Each later stage is a subset of the prior, so the funnel is monotonic.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    reqs = db.query(Requisition).filter(Requisition.created_at >= cutoff).all()
    req_ids = [r.id for r in reqs]

    quoted_req_ids: set[int] = set()
    if req_ids:
        quoted_req_ids = {
            rid for (rid,) in db.query(Quote.requisition_id).filter(Quote.requisition_id.in_(req_ids)).distinct().all()
        }

    opportunities = len(reqs)
    sourcing = sum(1 for r in reqs if r.status not in (RequisitionStatus.DRAFT, RequisitionStatus.OPEN))
    quoted = sum(
        1 for r in reqs if r.id in quoted_req_ids or r.status in (RequisitionStatus.QUOTED, RequisitionStatus.WON)
    )
    won = sum(1 for r in reqs if r.status == RequisitionStatus.WON)

    return {
        "window_days": days,
        "opportunities": opportunities,
        "sourcing": sourcing,
        "quoted": quoted,
        "won": won,
    }
