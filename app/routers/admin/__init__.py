"""Admin router package -- split from monolithic admin.py.

Re-exports a single router that includes all sub-routers so that
app/main.py can continue to do:

    from .routers.admin import router as admin_router

without any changes.

Called by: app/main.py
Depends on: .users, .system, .data_ops sub-modules
"""

from fastapi import APIRouter

from .data_ops import router as data_ops_router
from .system import router as system_router
from .users import router as users_router

# Re-export credential helpers so that existing test patches targeting
# "app.routers.admin.decrypt_value" (etc.) continue to resolve correctly.
# The sub-modules import these via "from . import decrypt_value" at call
# time so that the patched version is picked up.
from ...services.credential_service import decrypt_value, encrypt_value, mask_value  # noqa: F401

router = APIRouter()
router.include_router(users_router)
router.include_router(system_router)
router.include_router(data_ops_router)
