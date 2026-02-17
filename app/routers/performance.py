"""Performance Tracking API — Vendor Scorecards & Buyer Leaderboard."""

import logging
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_user, is_admin as _is_admin
from ..models import User

router = APIRouter(tags=["performance"])
log = logging.getLogger(__name__)


@router.get("/api/performance/vendors")
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

    return get_vendor_scorecard_list(db, sort_by, order, limit, offset, search)


@router.get("/api/performance/vendors/{vendor_card_id}")
def get_vendor_scorecard(
    vendor_card_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from ..services.performance_service import get_vendor_scorecard_detail

    result = get_vendor_scorecard_detail(db, vendor_card_id)
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
    return {"status": "ok", **result}


@router.get("/api/performance/buyers")
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
    return {"month": m.isoformat(), "entries": get_buyer_leaderboard(db, m)}


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
    return {"status": "ok", **result}
