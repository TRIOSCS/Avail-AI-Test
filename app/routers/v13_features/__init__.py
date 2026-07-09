"""v13_features — v1.3.0 Feature Routes (package)

Graph webhooks and activity logging.

Re-exports a single `router` from the activity sub-router.

Called by: main.py (router mount)
Depends on: activity sub-module
"""

from fastapi import APIRouter

from .activity import _activity_to_dict  # noqa: F401
from .activity import router as _activity_router

router = APIRouter()
router.include_router(_activity_router)
