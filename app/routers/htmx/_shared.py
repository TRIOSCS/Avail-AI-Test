"""routers/htmx/_shared.py — shared module-level helpers for the htmx_views split.

Holds cross-cutting helpers/state used by both htmx_views.py and the per-domain
sub-routers under app/routers/htmx/. Single source of truth — htmx_views.py
re-imports these names so its remaining routes keep working unchanged.

Called by: app/routers/htmx_views.py, app/routers/htmx/requisitions.py,
    app/routers/htmx/vendors.py, app/routers/htmx/companies.py
Depends on: app.constants, app.models, app.routers.admin.users (lazy)
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Request
from sqlalchemy.orm import Session

from ...constants import UserRole
from ...models import User, VerificationGroupMember

# Vite manifest for asset fingerprinting — read once at import time.
_MANIFEST_PATH = Path("app/static/dist/.vite/manifest.json")
_vite_manifest: dict = {}
if _MANIFEST_PATH.exists():
    _vite_manifest = json.loads(_MANIFEST_PATH.read_text())


def _vite_assets() -> dict:
    """Return Vite asset URLs for templates.

    Keys: js_file, css_files.
    """
    entry = _vite_manifest.get("htmx_app.js", {})
    js_file = entry.get("file", "assets/htmx_app.js")
    css_files = entry.get("css", [])
    # Also add standalone styles entry if not already in css list
    styles_entry = _vite_manifest.get("styles.css", {})
    if styles_entry.get("file") and styles_entry["file"] not in css_files:
        css_files = [styles_entry["file"]] + css_files
    return {"js_file": js_file, "css_files": css_files}


def _base_ctx(request: Request, user: User, current_view: str = "") -> dict:
    """Shared template context for all views."""
    from ..admin.users import module_access_map

    assets = _vite_assets()
    return {
        "request": request,
        "user_name": user.name if user else "",
        "user_email": user.email if user else "",
        "is_admin": user.role == UserRole.ADMIN if user else False,
        # Bottom-nav gate: {hyphenated-nav-id: bool}. None user → all True (shell never
        # blanked). Module keys don't need db (user_has_access handles admin/role-default).
        "access": module_access_map(user),
        "current_view": current_view,
        "vite_js": assets["js_file"],
        "vite_css": assets["css_files"],
        "now_utc": datetime.now(timezone.utc),
        "build_commit": os.environ.get("BUILD_COMMIT", "dev"),
    }


def _parse_date_safe(val, date_cls):
    """Safely parse an ISO date/datetime string, returning None on failure."""
    if not val:
        return None
    try:
        return date_cls.fromisoformat(val)
    except (ValueError, TypeError):
        return None


_DASH = "\u2014"  # em-dash for template fallbacks

# hx-target / push-url allowlists for the CRM list partials (vendors + customers).
_ALLOWED_HX_TARGETS = {"#main-content", "#crm-tab-content"}
_ALLOWED_PUSH_URL_BASES = {"/v2/vendors", "/v2/customers", "/v2/crm"}


def _sanitize_hx_params(hx_target: str, push_url_base: str, default_push: str) -> tuple[str, str]:
    """Validate hx_target and push_url_base against allowlists."""
    if hx_target not in _ALLOWED_HX_TARGETS:
        hx_target = "#main-content"
    if push_url_base not in _ALLOWED_PUSH_URL_BASES:
        push_url_base = default_push
    return hx_target, push_url_base


def _safe_int(val) -> int | None:
    """Safely convert form value to int."""
    if not val:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _safe_float(val) -> float | None:
    """Safely convert form value to float."""
    if not val:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _is_ops_member(user: User, db: Session) -> bool:
    """Check if user is in the ops verification group."""
    return db.query(VerificationGroupMember).filter_by(user_id=user.id, is_active=True).first() is not None
