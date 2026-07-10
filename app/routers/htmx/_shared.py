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
from datetime import UTC, date, datetime
from pathlib import Path

from fastapi import HTTPException, Request
from loguru import logger
from sqlalchemy.orm import Session
from starlette.responses import Response

from ...constants import UserRole
from ...models import User, VerificationGroupMember

# Vite manifest for asset fingerprinting — read once at import time.
_MANIFEST_PATH = Path("app/static/dist/.vite/manifest.json")
_vite_manifest: dict = {}
if _MANIFEST_PATH.exists():
    _vite_manifest = json.loads(_MANIFEST_PATH.read_text())
else:
    logger.critical(
        "Vite manifest missing ({}) — the frontend will render UNSTYLED with dead JS "
        "(the un-hashed fallback path never exists in dist/). Run `npm run build`, or "
        "deploy via ./deploy.sh which builds it.",
        _MANIFEST_PATH,
    )

_warned_missing_entry = False


def _vite_assets() -> dict:
    """Return Vite asset URLs for templates.

    Keys: js_file, css_files.
    """
    entry = _vite_manifest.get("htmx_app.js", {})
    if not entry:
        # Fail LOUDLY (but keep serving so dev-without-build still shows raw HTML):
        # the fallback path below points at an asset Vite never emits, so every page
        # would otherwise be silently blank/unstyled with a 404'd bundle.
        global _warned_missing_entry
        if not _warned_missing_entry:
            _warned_missing_entry = True
            logger.critical(
                "Vite manifest has no 'htmx_app.js' entry — pages will be unstyled with "
                "dead JS until `npm run build` produces app/static/dist/.vite/manifest.json"
            )
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
        "now_utc": datetime.now(UTC),
        "build_commit": os.environ.get("BUILD_COMMIT", "dev"),
        # The user's stored IANA display timezone (or "" when unset) — the base layout
        # renders it onto <body data-user-tz> so the client only posts the browser zone
        # when it actually differs (see syncDisplayTimezone in htmx_app.js).
        "user_display_tz": (getattr(user, "display_timezone", "") or "") if user else "",
    }


def full_page_shell(request: Request, user: User, partial_url: str, nav_active: str = "") -> Response:
    """Serve the base app shell that HTMX-loads ``partial_url`` into #main-content.

    Content-negotiation companion for routes whose OWN url is pushed into history (hx-
    push-url): a full-page reload / bookmark / share of that url arrives WITHOUT the HX-
    Request header and must receive the app shell (nav + chrome), not a bare fragment.
    The shell's loader then re-requests the same url WITH the HX-Request header and the
    route returns its partial. Mirrors how htmx_views.v2_page builds every /v2/* shell.
    """
    from ...template_env import page_response

    ctx = _base_ctx(request, user, nav_active)
    ctx["partial_url"] = partial_url
    return page_response(ctx)


def _parse_date_safe(val, date_cls):
    """Safely parse an ISO date/datetime string, returning None on failure."""
    if not val:
        return None
    try:
        return date_cls.fromisoformat(val)
    except (ValueError, TypeError):
        return None


def _parse_task_due_date(raw: str | None) -> datetime | None:
    """Parse an HTML ``<input type=date>`` value into an aware UTC datetime.

    Empty → None. A bare date (YYYY-MM-DD) becomes UTC midnight so it binds cleanly to
    the timestamptz ``due_at`` column — never a raw string, which ``UTCDateTime`` passes
    through unnormalized (wrong-TZ instant on PostgreSQL, ``AttributeError`` on SQLite).
    Raises 422 on a malformed non-empty value. Shared by the task create/edit endpoints
    (part comms tab, requisition Tasks tab) so every write path normalizes identically.
    """
    if not raw or not raw.strip():
        return None
    d = _parse_date_safe(raw.strip(), date)
    if d is None:
        raise HTTPException(422, "Invalid due date")
    return datetime.combine(d, datetime.min.time()).replace(tzinfo=UTC)


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
