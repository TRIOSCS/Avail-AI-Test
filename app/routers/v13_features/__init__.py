"""
v13_features — v1.3.0 Feature Routes (package)

Graph webhooks, activity logging, sales dashboard (account ownership & open pool),
and prospecting pool (site-level ownership).

Re-exports a single `router` that merges activity, sales, and prospecting sub-routers.

Called by: main.py (router mount)
Depends on: activity, sales, prospecting sub-modules
"""

from fastapi import APIRouter

from sqlalchemy.orm import Session  # noqa: F401 — test patches app.routers.v13_features.Session

from ...config import settings  # noqa: F401 — test patches app.routers.v13_features.settings

from .activity import _activity_to_dict  # noqa: F401 — tests import this
from .activity import router as _activity_router
from .prospecting import SITE_CAP_PER_USER  # noqa: F401 — tests import this
from .prospecting import router as _prospecting_router
from .sales import _NOTIFICATION_TYPES  # noqa: F401 — tests import this
from .sales import router as _sales_router

router = APIRouter()
router.include_router(_activity_router)
router.include_router(_sales_router)
router.include_router(_prospecting_router)
