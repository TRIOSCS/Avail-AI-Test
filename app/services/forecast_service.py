"""Pipeline / forecast service — requisition-as-opportunity rollups.

The Requisition IS the opportunity (locked CRM decision — no separate deal
object). Forecast = sum(deal_value * stage win-probability) over open
requisitions.

Forecast dollars reuse _resolve_deal_value so they reconcile with what the
requisition list shows per row.

Called by: app/routers/htmx_views.py parts_workspace_partial (the Sales Hub /
           parts workspace) — pipeline_summary feeds the pipeline chip there.
Depends on: app.models (Requisition, Requirement)
"""

from __future__ import annotations

from sqlalchemy import case
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from app.constants import RequisitionStatus
from app.models import Requirement, Requisition


def _resolve_deal_value(
    opportunity_value: float | None,
    priced_sum: float,
    priced_count: int,
    requirement_count: int,
) -> tuple[float | None, str]:
    """Pick displayed deal value; tag provenance (entered / computed / partial / none).

    Priority (per 2026-04-21 merged spec §Backend contract additions):
      1. opportunity_value > 0            → 'entered'   (broker-entered wins)
      2. priced_sum > 0 and all priced    → 'computed'  (target prices complete)
      3. priced_sum > 0 and some unpriced → 'partial'   (floor estimate)
      4. otherwise                         → 'none'     (no useful signal)

    Zero-priced requirements count as priced (target_price explicitly 0 means
    "free/sample," not "unknown"). priced_count reflects NOT-NULL target_price.
    """
    if opportunity_value and opportunity_value > 0:
        return opportunity_value, "entered"
    if priced_sum and priced_sum > 0:
        if priced_count >= requirement_count:
            return priced_sum, "computed"
        return priced_sum, "partial"
    return None, "none"


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
