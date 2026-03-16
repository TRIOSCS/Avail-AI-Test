"""
routers/htmx/_helpers.py — Shared helpers for HTMX view routers.

Provides the common router, template engine, Vite asset resolution,
and utility functions used by every domain-specific HTMX router module.

Called by: htmx domain routers (requisitions, vendors, companies, etc.)
Depends on: models.User, utils.sql_helpers, Jinja2Templates
"""

import json
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from ...models import User
from ...utils.sql_helpers import escape_like  # noqa: F401  — re-exported

router = APIRouter(tags=["htmx-views"])
_DASH = "\u2014"  # em-dash for template fallbacks
templates = Jinja2Templates(directory="app/templates")

# Vite manifest for asset fingerprinting — read once at import time.
_MANIFEST_PATH = Path("app/static/dist/.vite/manifest.json")
_vite_manifest: dict = {}
if _MANIFEST_PATH.exists():
    _vite_manifest = json.loads(_MANIFEST_PATH.read_text())


def _vite_assets() -> dict:
    """Return Vite asset URLs for templates. Keys: js_file, css_files."""
    entry = _vite_manifest.get("htmx_app.js", {})
    js_file = entry.get("file", "assets/htmx_app.js")
    css_files = entry.get("css", [])
    # Also add standalone styles entry if not already in css list
    styles_entry = _vite_manifest.get("styles.css", {})
    if styles_entry.get("file") and styles_entry["file"] not in css_files:
        css_files = [styles_entry["file"]] + css_files
    return {"js_file": js_file, "css_files": css_files}


def _timesince_filter(dt):
    """Convert datetime to human-readable relative time string."""
    if not dt:
        return ""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff = now - dt
    seconds = diff.total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        mins = int(seconds // 60)
        return f"{mins} min ago"
    if seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = int(seconds // 86400)
    if days == 1:
        return "1 day ago"
    return f"{days} days ago"


templates.env.filters["timesince"] = _timesince_filter


def _is_htmx(request: Request) -> bool:
    """Check if this is an HTMX partial request (vs full page load)."""
    return request.headers.get("HX-Request") == "true"


def _base_ctx(request: Request, user: User, current_view: str = "") -> dict:
    """Shared template context for all views."""
    assets = _vite_assets()
    return {
        "request": request,
        "user_name": user.name if user else "",
        "user_email": user.email if user else "",
        "is_admin": user.role == "admin" if user else False,
        "current_view": current_view,
        "vite_js": assets["js_file"],
        "vite_css": assets["css_files"],
    }
