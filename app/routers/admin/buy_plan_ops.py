"""Admin router — ops verification group membership management.

Routes:
- POST /api/admin/ops-group/toggle — add or (de/re)activate a user's ops-group membership

The ops verification group gates SO/PO verification and buy-plan completion (see
services/buyplan_workflow.py verify_so / verify_po / check_completion). With no active
members, no buy plan can complete, so this admin surface is how the group is curated.
Membership uses an is_active toggle (not delete) because verification_group_members.user_id
is UNIQUE — toggling preserves added_at and avoids an IntegrityError race on re-add.

Called by: app/routers/admin/__init__.py (included via router)
Depends on: models.buy_plan.VerificationGroupMember, dependencies.require_admin,
            template_env.template_response
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session

from ...database import get_db
from ...dependencies import require_admin
from ...models import User
from ...models.buy_plan import VerificationGroupMember
from ...template_env import template_response

router = APIRouter(tags=["admin"])


def ops_group_context(db: Session) -> dict:
    """Build {rows, active_count} for the ops-group settings partial.

    rows: every active user with their membership state (member row or None). Shared by
    the settings GET tab (htmx_views.settings_ops_group_tab) and the toggle POST below.
    """
    all_users = db.query(User).filter(User.is_active.is_(True)).order_by(User.name).all()
    memberships = {m.user_id: m for m in db.query(VerificationGroupMember).all()}
    rows = []
    for u in all_users:
        member = memberships.get(u.id)
        rows.append({"user": u, "member": member, "is_member": bool(member and member.is_active)})
    active_count = sum(1 for r in rows if r["is_member"])
    return {"rows": rows, "active_count": active_count}


@router.post("/api/admin/ops-group/toggle", response_class=HTMLResponse)
async def toggle_ops_member(
    request: Request,
    user_id: int = Form(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Toggle a user's ops verification-group membership; returns the refreshed partial.

    No row -> create active. Existing row -> flip is_active.
    """
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, "User not found")

    member = db.query(VerificationGroupMember).filter_by(user_id=user_id).first()
    if member is None:
        db.add(VerificationGroupMember(user_id=user_id, is_active=True, added_at=datetime.now(timezone.utc)))
        action = "added"
    else:
        member.is_active = not member.is_active
        action = "activated" if member.is_active else "deactivated"
    db.commit()
    logger.info("Ops group member {} {} by {}", target.email, action, admin.email)

    ctx = {"request": request, "is_admin": True, **ops_group_context(db)}
    return template_response("htmx/partials/settings/ops_group.html", ctx)
