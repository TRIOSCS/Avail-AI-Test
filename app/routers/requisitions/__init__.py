"""Requisitions router package — split from monolithic requisitions.py.

Re-exports a single router that includes all sub-routers so that
app/main.py can continue to do:

    from .routers.requisitions import router as reqs_router

without any changes.

Called by: app/main.py
Depends on: .core, .requirements, .attachments sub-modules
"""

from fastapi import APIRouter

# Re-export names that test files patch at "app.routers.requisitions.X".
# Sub-modules use "from . import X" at call time so patched versions are picked up.
from ...cache.decorators import invalidate_prefix  # noqa: F401
from ...search_service import (  # noqa: F401
    _deduplicate_sightings,
    _get_material_history,
    _history_to_result,
    search_requirement,
    sighting_to_dict,
)
from ...vendor_utils import _enrich_with_vendor_cards  # noqa: F401
from .attachments import router as attachments_router
from .core import router as core_router
from .requirements import router as requirements_router

router = APIRouter()
router.include_router(core_router)
router.include_router(requirements_router)
router.include_router(attachments_router)
