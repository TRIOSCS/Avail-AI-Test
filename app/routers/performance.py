"""Performance Tracking API — Vendor Scorecards, Buyer Leaderboard, Avail Scores & Multipliers."""

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..cache.decorators import cached_endpoint, invalidate_prefix
from ..database import get_db
from ..dependencies import is_admin as _is_admin
from ..dependencies import require_user
from ..models import User
from ..schemas.responses import BuyerLeaderboardResponse, VendorScorecardListResponse

router = APIRouter(tags=["performance"])


@router.get("/api/performance/vendors", response_model=VendorScorecardListResponse, response_model_exclude_none=True)
def list_vendor_scorecards(
    sort_by: str = Query("composite_score"),
    order: str = Query("desc"),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
    search: str = Query(None),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from ..services.performance_service import get_vendor_scorecard_list

    @cached_endpoint(prefix="perf_vendors", ttl_hours=4, key_params=["sort_by", "order", "limit", "offset", "search"])
    def _fetch(sort_by, order, limit, offset, search, db):
        return get_vendor_scorecard_list(db, sort_by, order, limit, offset, search)

    return _fetch(sort_by=sort_by, order=order, limit=limit, offset=offset, search=search, db=db)


@router.get("/api/performance/vendors/{vendor_card_id}")
def get_vendor_scorecard(
    vendor_card_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from ..services.performance_service import get_vendor_scorecard_detail

    @cached_endpoint(prefix="perf_vendor_detail", ttl_hours=4, key_params=["vendor_card_id"])
    def _fetch(vendor_card_id, db):
        return get_vendor_scorecard_detail(db, vendor_card_id)

    result = _fetch(vendor_card_id=vendor_card_id, db=db)
    if not result:
        raise HTTPException(404, "Vendor not found")
    return result


@router.post("/api/performance/vendors/refresh")
def refresh_vendor_scorecards(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if not _is_admin(user):
        raise HTTPException(403, "Admin required")
    from ..services.performance_service import compute_all_vendor_scorecards

    result = compute_all_vendor_scorecards(db)
    invalidate_prefix("perf_vendors")
    invalidate_prefix("perf_vendor_detail")
    return {"status": "ok", **result}


@router.get("/api/performance/buyers", response_model=BuyerLeaderboardResponse, response_model_exclude_none=True)
def list_buyer_leaderboard(
    month: str = Query(None, description="YYYY-MM format"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from ..services.performance_service import get_buyer_leaderboard

    if month:
        try:
            m = datetime.strptime(month, "%Y-%m").date()
        except ValueError:
            raise HTTPException(400, "Invalid month format — use YYYY-MM")
    else:
        m = date.today().replace(day=1)

    @cached_endpoint(prefix="perf_buyers", ttl_hours=4, key_params=["month"])
    def _fetch(month, db):
        return {"month": month.isoformat(), "entries": get_buyer_leaderboard(db, month)}

    return _fetch(month=m, db=db)


@router.get("/api/performance/buyers/months")
def list_leaderboard_months(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from ..services.performance_service import get_buyer_leaderboard_months

    return {"months": get_buyer_leaderboard_months(db)}


@router.get("/api/performance/salespeople")
def list_salesperson_scorecard(
    month: str = Query(None, description="YYYY-MM format"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from ..services.performance_service import get_salesperson_scorecard

    if month:
        try:
            m = datetime.strptime(month, "%Y-%m").date()
        except ValueError:
            raise HTTPException(400, "Invalid month format — use YYYY-MM")
    else:
        m = date.today().replace(day=1)
    return get_salesperson_scorecard(db, m)


@router.post("/api/performance/buyers/refresh")
def refresh_buyer_leaderboard(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if not _is_admin(user):
        raise HTTPException(403, "Admin required")
    from ..services.performance_service import compute_buyer_leaderboard

    m = date.today().replace(day=1)
    result = compute_buyer_leaderboard(db, m)
    invalidate_prefix("perf_buyers")
    return {"status": "ok", **result}


# ── Avail Scores ─────────────────────────────────────────────────────


@router.get("/api/performance/avail-scores")
def get_avail_scores_endpoint(
    role: str = Query(..., pattern="^(buyer|sales)$"),
    month: str = Query(None, description="YYYY-MM format"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return ranked Avail Scores for buyer or sales team."""
    from ..services.avail_score_service import get_avail_scores

    if month:
        try:
            m = datetime.strptime(month, "%Y-%m").date()
        except ValueError:
            raise HTTPException(400, "Invalid month format — use YYYY-MM")
    else:
        m = date.today().replace(day=1)

    return {"month": m.isoformat(), "role": role, "entries": get_avail_scores(db, role, m)}


@router.post("/api/performance/avail-scores/refresh")
def refresh_avail_scores(
    month: str = Query(None, description="YYYY-MM format"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Recompute Avail Scores for all users. Admin only."""
    if not _is_admin(user):
        raise HTTPException(403, "Admin required")
    from ..services.avail_score_service import compute_all_avail_scores

    if month:
        try:
            m = datetime.strptime(month, "%Y-%m").date()
        except ValueError:
            raise HTTPException(400, "Invalid month format — use YYYY-MM")
    else:
        m = date.today().replace(day=1)

    result = compute_all_avail_scores(db, m)
    invalidate_prefix("perf_avail")
    return {"status": "ok", **result}


# ── Multiplier Scores ────────────────────────────────────────────────


@router.get("/api/performance/multiplier-scores")
def get_multiplier_scores_endpoint(
    role: str = Query(..., pattern="^(buyer|sales)$"),
    month: str = Query(None, description="YYYY-MM format"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return ranked Multiplier Scores for buyer or sales team."""
    from ..services.multiplier_score_service import get_multiplier_scores

    if month:
        try:
            m = datetime.strptime(month, "%Y-%m").date()
        except ValueError:
            raise HTTPException(400, "Invalid month format — use YYYY-MM")
    else:
        m = date.today().replace(day=1)

    return {"month": m.isoformat(), "role": role, "entries": get_multiplier_scores(db, role, m)}


@router.get("/api/performance/bonus-winners")
def get_bonus_winners_endpoint(
    role: str = Query(..., pattern="^(buyer|sales)$"),
    month: str = Query(..., description="YYYY-MM format"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return bonus winners for a role+month."""
    from ..services.multiplier_score_service import determine_bonus_winners

    try:
        m = datetime.strptime(month, "%Y-%m").date()
    except ValueError:
        raise HTTPException(400, "Invalid month format — use YYYY-MM")

    return {"month": m.isoformat(), "role": role, "winners": determine_bonus_winners(db, role, m)}


@router.post("/api/performance/multiplier-scores/refresh")
def refresh_multiplier_scores(
    month: str = Query(None, description="YYYY-MM format"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Recompute Multiplier Scores for all users. Admin only."""
    if not _is_admin(user):
        raise HTTPException(403, "Admin required")
    from ..services.multiplier_score_service import compute_all_multiplier_scores

    if month:
        try:
            m = datetime.strptime(month, "%Y-%m").date()
        except ValueError:
            raise HTTPException(400, "Invalid month format — use YYYY-MM")
    else:
        m = date.today().replace(day=1)

    result = compute_all_multiplier_scores(db, m)
    invalidate_prefix("perf_multiplier")
    return {"status": "ok", **result}


# ── Unified Scores ──────────────────────────────────────────────────


@router.post("/api/performance/unified-scores/refresh")
def refresh_unified_scores(
    month: str = Query(None, description="YYYY-MM format"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Recompute Unified Scores for all users. Admin only."""
    if not _is_admin(user):
        raise HTTPException(403, "Admin required")
    from ..services.unified_score_service import compute_all_unified_scores

    if month:
        try:
            m = datetime.strptime(month, "%Y-%m").date()
        except ValueError:
            raise HTTPException(400, "Invalid month format — use YYYY-MM")
    else:
        m = date.today().replace(day=1)

    result = compute_all_unified_scores(db, m)
    invalidate_prefix("unified_lb")
    return {"status": "ok", **result}
