"""CRM shell views — unified CRM tab with Customers/Vendors sub-tabs.

Called by: app/routers/crm/__init__.py (included via router)
Depends on: app/dependencies (require_user), app/templates
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

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
