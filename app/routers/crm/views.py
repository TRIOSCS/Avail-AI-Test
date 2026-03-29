"""CRM shell views — unified CRM tab with Customers/Vendors sub-tabs.

Called by: app/routers/crm/__init__.py (included via router)
Depends on: app/dependencies (require_user), app/templates
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ...database import get_db
from ...dependencies import require_user
from ...models.auth import User

router = APIRouter(tags=["crm"])


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

    users_scores = []
    for u in active_users:
        users_scores.append(
            {
                "name": u.name or u.email,
                "behavior_total": 0,
                "outcome_total": 0,
                "total_score": 0,
            }
        )

    ctx = {
        "request": request,
        "user": user,
        "users_scores": users_scores,
    }
    return templates.TemplateResponse("htmx/partials/crm/performance_tab.html", ctx)
