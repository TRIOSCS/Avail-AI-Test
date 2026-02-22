"""Vite manifest integration for Jinja2 templates.

Provides helper functions that emit the correct <link>/<script> tags
depending on the environment:

- **Production** (manifest exists): content-hashed paths from manifest.json
- **Dev without build** (no manifest): raw source paths with cache-bust param
- **VITE_DEV=1**: tags pointing at the Vite dev server for HMR
"""

import json
import os
from functools import lru_cache
from markupsafe import Markup
from pathlib import Path

MANIFEST_PATH = Path(__file__).parent / "static" / "dist" / ".vite" / "manifest.json"
VITE_DEV_ORIGIN = "http://localhost:5173"


@lru_cache(maxsize=1)
def _load_manifest() -> dict | None:
    """Load and cache the Vite manifest. Returns None if not found."""
    if MANIFEST_PATH.is_file():
        return json.loads(MANIFEST_PATH.read_text())
    return None


def _manifest_url(entry_key: str) -> str | None:
    """Resolve an entry key to its hashed URL via the manifest."""
    manifest = _load_manifest()
    if not manifest:
        return None
    chunk = manifest.get(entry_key)
    if chunk:
        return f"/static/{chunk['file']}"
    return None


def _manifest_css(entry_key: str) -> list[str]:
    """Get CSS files associated with a JS entry from the manifest."""
    manifest = _load_manifest()
    if not manifest:
        return []
    chunk = manifest.get(entry_key)
    if chunk:
        return [f"/static/{css}" for css in chunk.get("css", [])]
    return []


def vite_css_tags(app_version: str = "") -> Markup:
    """Return <link> tags for CSS assets."""
    if os.environ.get("VITE_DEV"):
        # Vite dev server injects CSS via JS — no link tags needed
        return Markup("")

    # Try manifest first (production build)
    url = _manifest_url("styles.css")
    if url:
        tags = [f'<link rel="stylesheet" href="{url}">']
        # Also include CSS extracted from JS entry points
        for key in ("app.js", "crm.js"):
            tags.extend(f'<link rel="stylesheet" href="{css}">' for css in _manifest_css(key))
        return Markup("\n    ".join(tags))

    # Fallback: raw source file
    bust = f"?v={app_version}" if app_version else ""
    return Markup(f'<link rel="stylesheet" href="/static/styles.css{bust}">')


def vite_js_tags(app_version: str = "") -> Markup:
    """Return <script> tags for JS entry points."""
    if os.environ.get("VITE_DEV"):
        return Markup(
            f'<script type="module" src="{VITE_DEV_ORIGIN}/@vite/client"></script>\n'
            f'    <script type="module" src="{VITE_DEV_ORIGIN}/app.js"></script>\n'
            f'    <script type="module" src="{VITE_DEV_ORIGIN}/crm.js"></script>'
        )

    # Try manifest (production)
    app_url = _manifest_url("app.js")
    crm_url = _manifest_url("crm.js")
    if app_url and crm_url:
        return Markup(
            f'<script type="module" src="{app_url}"></script>\n'
            f'    <script type="module" src="{crm_url}"></script>'
        )

    # Fallback: raw source with importmap
    bust = f"?v={app_version}" if app_version else ""
    return Markup(
        '<script type="importmap">\n'
        "{\n"
        '  "imports": {\n'
        f'    "app": "/static/app.js{bust}"\n'
        "  }\n"
        "}\n"
        "</script>\n"
        f'    <script type="module" src="/static/app.js{bust}"></script>\n'
        f'    <script type="module" src="/static/crm.js{bust}"></script>'
    )
