"""Admin user management endpoints -- CRUD for users, roles, and activation.

Business rules:
- Only admin users can create, update, or delete other users.
- Valid roles are defined in admin_service.VALID_ROLES.
- Admins cannot delete themselves.
- User emails are normalized to lowercase and stripped.

Called by: app/routers/admin/__init__.py (included via router)
Depends on: app/services/admin_service.py, app/models, app/dependencies
"""


from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ...database import get_db
from ...dependencies import require_admin
from ...models import User
from ...rate_limit import limiter
from ...services.admin_service import VALID_ROLES, list_users, update_user

router = APIRouter(tags=["admin"])


# -- Schemas ---------------------------------------------------------------


class CreateUserRequest(BaseModel):
    name: str
    email: str
    role: str = "buyer"


class UserUpdateRequest(BaseModel):
    name: str | None = None
    role: str | None = None
    is_active: bool | None = None


# -- User Management (admin only) -----------------------------------------


@router.get("/api/admin/users")
@limiter.limit("30/minute")
def api_list_users(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    return list_users(db)


@router.post("/api/admin/users")
@limiter.limit("5/minute")
def api_create_user(
    request: Request,
    body: CreateUserRequest,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if body.role not in VALID_ROLES:
        raise HTTPException(400, f"Role must be one of: {', '.join(VALID_ROLES)}")

    # Validate name
    name = body.name.strip() if body.name else ""
    if not name:
        raise HTTPException(400, "Name is required")
    if len(name) > 100:
        raise HTTPException(400, "Name must be 100 characters or fewer")

    # Validate email
    email = body.email.strip().lower() if body.email else ""
    if not email:
        raise HTTPException(400, "A valid email address is required")
    if " " in body.email.strip():
        raise HTTPException(400, "A valid email address is required")
    if "@" not in email:
        raise HTTPException(400, "A valid email address is required")
    local, _, domain = email.partition("@")
    if not local or not domain or "." not in domain:
        raise HTTPException(400, "A valid email address is required")

    existing = db.query(User).filter(User.email == email).first()
    if existing:
        raise HTTPException(409, "User with this email already exists")
    new_user = User(
        name=name,
        email=email,
        role=body.role,
    )
    db.add(new_user)
    db.commit()
    return {
        "id": new_user.id,
        "name": new_user.name,
        "email": new_user.email,
        "role": new_user.role,
    }


@router.put("/api/admin/users/{user_id}")
@limiter.limit("10/minute")
def api_update_user(
    user_id: int,
    request: Request,
    body: UserUpdateRequest,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    result = update_user(db, user_id, body.model_dump(exclude_none=False), user)
    if "error" in result:
        raise HTTPException(result.get("status", 400), result["error"])
    return result


@router.delete("/api/admin/users/{user_id}")
@limiter.limit("5/minute")
def api_delete_user(
    user_id: int,
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, "User not found")
    if target.id == user.id:
        raise HTTPException(400, "Cannot delete yourself")
    db.delete(target)
    db.commit()
    return {"status": "deleted"}
