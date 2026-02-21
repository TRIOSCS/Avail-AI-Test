"""Performance Tracking API — Vendor Scorecards & Buyer Leaderboard."""

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
