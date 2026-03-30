"""CRM shell views — unified CRM tab with Customers/Vendors sub-tabs.

Called by: app/routers/crm/__init__.py (included via router)
Depends on: app/dependencies (require_user), app/templates
"""

from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger
from sqlalchemy.orm import Session

from ...constants import UserRole
from ...database import get_db
from ...dependencies import require_user
from ...models.auth import User
from ...models.performance import AvailScoreSnapshot

router = APIRouter(tags=["crm"])


def _compute_user_score(db: Session, user: User, month: date) -> dict:
    """Compute Avail Score on-demand for a user based on their role.

    Called by: crm_performance route when no AvailScoreSnapshot exists.
    Returns dict with behavior_total, outcome_total, total_score.
    """
    from ...services.avail_score_service import (
        compute_buyer_avail_score,
        compute_sales_avail_score,
    )

    try:
        if user.role == UserRole.SALES:
            return compute_sales_avail_score(db, user.id, month)
        else:
            # Default to buyer score for buyer, trader, manager, admin
            return compute_buyer_avail_score(db, user.id, month)
    except Exception:
        logger.warning("Failed to compute Avail Score for user {}", user.id)
        return {"behavior_total": 0, "outcome_total": 0, "total_score": 0}


@router.get("/v2/partials/crm/shell", response_class=HTMLResponse)
async def crm_shell(
    request: Request,
    user: User = Depends(require_user),
):
    """Render the CRM shell with Customers/Vendors tab bar."""
    from ...template_env import templates

    ctx = {
        "request": request,
        "user": user,
        "default_tab": "customers",
    }
    return templates.TemplateResponse("htmx/partials/crm/shell.html", ctx)


@router.get("/v2/partials/crm/performance", response_class=HTMLResponse)
async def crm_performance(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the team performance dashboard."""
    from ...template_env import templates

    active_users = db.query(User).filter(User.is_active.is_(True)).order_by(User.name).all()

    # Read pre-computed snapshots for the current month (populated by daily scheduler).
    # Fall back to on-demand compute for users without a snapshot.
    month_start = date.today().replace(day=1)
    snapshots = db.query(AvailScoreSnapshot).filter(AvailScoreSnapshot.month == month_start).all()
    snap_by_user = {s.user_id: s for s in snapshots}

    users_scores = []
    for u in active_users:
        snap = snap_by_user.get(u.id)
        if snap:
            users_scores.append(
                {
                    "name": u.name or u.email,
                    "behavior_total": round(snap.behavior_total or 0, 1),
                    "outcome_total": round(snap.outcome_total or 0, 1),
                    "total_score": round(snap.total_score or 0, 1),
                }
            )
        else:
            # No snapshot yet — compute on-demand (fine for small teams)
            score_data = _compute_user_score(db, u, month_start)
            users_scores.append(
                {
                    "name": u.name or u.email,
                    "behavior_total": round(score_data.get("behavior_total", 0), 1),
                    "outcome_total": round(score_data.get("outcome_total", 0), 1),
                    "total_score": round(score_data.get("total_score", 0), 1),
                }
            )

    # Sort by total_score descending for leaderboard ordering
    users_scores.sort(key=lambda s: s["total_score"], reverse=True)

    ctx = {
        "request": request,
        "user": user,
        "users_scores": users_scores,
    }
    return templates.TemplateResponse("htmx/partials/crm/performance_tab.html", ctx)


@router.get("/api/crm/performance-metrics")
async def performance_metrics_json(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return performance scores as JSON for Chart.js rendering."""
    active_users = db.query(User).filter(User.is_active.is_(True)).order_by(User.name).all()

    month_start = date.today().replace(day=1)
    snapshots = db.query(AvailScoreSnapshot).filter(AvailScoreSnapshot.month == month_start).all()
    snap_by_user = {s.user_id: s for s in snapshots}

    names: list[str] = []
    scores: list[float] = []
    behaviors: list[float] = []
    outcomes: list[float] = []

    for u in active_users:
        snap = snap_by_user.get(u.id)
        if snap:
            names.append(u.name or u.email)
            scores.append(round(snap.total_score or 0, 1))
            behaviors.append(round(snap.behavior_total or 0, 1))
            outcomes.append(round(snap.outcome_total or 0, 1))
        else:
            score_data = _compute_user_score(db, u, month_start)
            names.append(u.name or u.email)
            scores.append(round(score_data.get("total_score", 0), 1))
            behaviors.append(round(score_data.get("behavior_total", 0), 1))
            outcomes.append(round(score_data.get("outcome_total", 0), 1))

    return JSONResponse(
        {
            "names": names,
            "scores": scores,
            "behaviors": behaviors,
            "outcomes": outcomes,
        }
    )
