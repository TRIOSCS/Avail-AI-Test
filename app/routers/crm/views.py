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

    Called by: _build_user_scores when no AvailScoreSnapshot exists.
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
            return compute_buyer_avail_score(db, user.id, month)
    except Exception:
        logger.warning("Failed to compute Avail Score for user {}", user.id)
        return {"behavior_total": 0, "outcome_total": 0, "total_score": 0}


def _score_from_snap(snap) -> dict:
    """Extract rounded score dict from an AvailScoreSnapshot."""
    return {
        "behavior_total": round(snap.behavior_total or 0, 1),
        "outcome_total": round(snap.outcome_total or 0, 1),
        "total_score": round(snap.total_score or 0, 1),
    }


def _score_from_data(data: dict) -> dict:
    """Extract rounded score dict from a computed score dict."""
    return {
        "behavior_total": round(data.get("behavior_total", 0), 1),
        "outcome_total": round(data.get("outcome_total", 0), 1),
        "total_score": round(data.get("total_score", 0), 1),
    }


def _build_user_scores(db: Session) -> list[dict]:
    """Build scored user list for the current month.

    Called by: crm_performance (HTML) and performance_metrics_json (JSON).
    Returns list of dicts sorted by total_score descending.
    """
    active_users = db.query(User).filter(User.is_active.is_(True)).order_by(User.name).all()
    month_start = date.today().replace(day=1)
    snapshots = db.query(AvailScoreSnapshot).filter(AvailScoreSnapshot.month == month_start).all()
    snap_by_user = {s.user_id: s for s in snapshots}

    users_scores = []
    for u in active_users:
        snap = snap_by_user.get(u.id)
        if snap:
            scores = _score_from_snap(snap)
        else:
            scores = _score_from_data(_compute_user_score(db, u, month_start))
        users_scores.append({"name": u.name or u.email, **scores})

    users_scores.sort(key=lambda s: s["total_score"], reverse=True)
    return users_scores


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

    ctx = {
        "request": request,
        "user": user,
        "users_scores": _build_user_scores(db),
    }
    return templates.TemplateResponse("htmx/partials/crm/performance_tab.html", ctx)


@router.get("/api/crm/performance-metrics")
async def performance_metrics_json(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return performance scores as JSON for Chart.js rendering."""
    users_scores = _build_user_scores(db)
    return JSONResponse(
        {
            "names": [u["name"] for u in users_scores],
            "scores": [u["total_score"] for u in users_scores],
            "behaviors": [u["behavior_total"] for u in users_scores],
            "outcomes": [u["outcome_total"] for u in users_scores],
        }
    )
