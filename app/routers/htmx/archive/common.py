"""Shared state for the archive package — router + task-form helpers.

W4.8 split of the 969-line app/routers/htmx/archive.py — pure structural move: URLs
and behavior unchanged; every route attaches to the shared router imported from
.common (registration assembled in __init__).

Called by: app/main.py (router mount via the package __init__); sibling submodules
    (tasks_crud uses _coerce_task_priority + _active_users).
Depends on: app.models (User)
"""

from fastapi import APIRouter
from sqlalchemy.orm import Session

from ....models import User

router = APIRouter(tags=["htmx-views"])


def _coerce_task_priority(raw: str | None) -> int:
    """Map a submitted priority ('1'|'2'|'3') to a valid int, defaulting to 2
    (medium)."""
    try:
        p = int(raw) if raw not in (None, "") else 2
    except (TypeError, ValueError):
        return 2
    return p if p in (1, 2, 3) else 2


def _active_users(db: Session) -> list[User]:
    """Active users for the task-create assignee picker, ordered by name."""
    return db.query(User).filter(User.is_active.is_(True)).order_by(User.name).all()
