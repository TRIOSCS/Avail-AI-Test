"""
test_vite_coverage.py — Tests for app/vite.py

Covers uncovered lines:
- _load_manifest when file exists (line 26) / not found (line 26)
- _manifest_url with/without manifest (lines 33, 37)
- _manifest_css with/without manifest (lines 44, 48)
- vite_css_tags: VITE_DEV, production manifest, fallback (lines 55, 67-68)
- vite_js_tags: VITE_DEV, production manifest, fallback (lines 74, 90-91)
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

from markupsafe import Markup

import app.vite as vite_mod
from app.vite import (
    _manifest_css,
    _manifest_url,
    vite_app_url,
    vite_crm_url,
    vite_css_tags,
    vite_js_tags,
)

# ── Reset the lru_cache before each test ──────────────────────────────


def _clear_manifest_cache():
    """Clear the _load_manifest lru_cache."""
    vite_mod._load_manifest.cache_clear()


# ── _load_manifest ────────────────────────────────────────────────────


class TestLoadManifest:
    def test_no_manifest_file(self):
        """Returns None when manifest.json does not exist."""
        _clear_manifest_cache()
        with patch.object(Path, "is_file", return_value=False):
            result = vite_mod._load_manifest()
            assert result is None

    def test_manifest_file_exists(self, tmp_path):
        """Returns parsed manifest dict when file exists."""
        _clear_manifest_cache()
        manifest_data = {
            "app.js": {"file": "assets/app-abc123.js", "css": ["assets/app-abc.css"]},
            "crm.js": {"file": "assets/crm-def456.js"},
            "styles.css": {"file": "assets/styles-ghi789.css"},
        }
        manifest_file = tmp_path / "manifest.json"
        manifest_file.write_text(json.dumps(manifest_data))

        with patch.object(vite_mod, "MANIFEST_PATH", manifest_file):
            result = vite_mod._load_manifest()
            assert result is not None
            assert "app.js" in result
            assert result["app.js"]["file"] == "assets/app-abc123.js"


# ── _manifest_url ─────────────────────────────────────────────────────


class TestManifestUrl:
    def test_no_manifest_returns_none(self):
        """When no manifest, returns None."""
        _clear_manifest_cache()
        with patch.object(vite_mod, "_load_manifest", return_value=None):
            result = _manifest_url("app.js")
            assert result is None

    def test_entry_found(self):
        """Returns /static/ prefixed path for found entry."""
        _clear_manifest_cache()
        manifest = {"app.js": {"file": "assets/app-abc.js"}}
        with patch.object(vite_mod, "_load_manifest", return_value=manifest):
            result = _manifest_url("app.js")
            assert result == "/static/assets/app-abc.js"

    def test_entry_not_found(self):
        """Returns None when entry key is not in manifest."""
        _clear_manifest_cache()
        manifest = {"app.js": {"file": "assets/app-abc.js"}}
        with patch.object(vite_mod, "_load_manifest", return_value=manifest):
            result = _manifest_url("missing.js")
            assert result is None


# ── _manifest_css ─────────────────────────────────────────────────────


class TestManifestCss:
    def test_no_manifest_returns_empty(self):
        """When no manifest, returns empty list."""
        _clear_manifest_cache()
        with patch.object(vite_mod, "_load_manifest", return_value=None):
            result = _manifest_css("app.js")
            assert result == []

    def test_entry_with_css(self):
        """Returns CSS file paths for entry."""
        _clear_manifest_cache()
        manifest = {"app.js": {"file": "assets/app.js", "css": ["assets/app.css", "assets/vendor.css"]}}
        with patch.object(vite_mod, "_load_manifest", return_value=manifest):
            result = _manifest_css("app.js")
            assert result == ["/static/assets/app.css", "/static/assets/vendor.css"]

    def test_entry_without_css(self):
        """Returns empty list when entry has no css key."""
        _clear_manifest_cache()
        manifest = {"app.js": {"file": "assets/app.js"}}
        with patch.object(vite_mod, "_load_manifest", return_value=manifest):
            result = _manifest_css("app.js")
            assert result == []

    def test_entry_not_found_returns_empty(self):
        """Returns empty list when entry key not in manifest."""
        _clear_manifest_cache()
        manifest = {"app.js": {"file": "assets/app.js"}}
        with patch.object(vite_mod, "_load_manifest", return_value=manifest):
            result = _manifest_css("missing.js")
            assert result == []


# ── vite_css_tags ─────────────────────────────────────────────────────


class TestViteCssTags:
    def test_vite_dev_returns_empty(self):
        """In VITE_DEV mode, returns empty Markup (CSS injected via JS)."""
        _clear_manifest_cache()
        with patch.dict(os.environ, {"VITE_DEV": "1"}):
            result = vite_css_tags()
            assert result == Markup("")

    def test_production_with_manifest(self):
        """With manifest, returns link tags with hashed URLs."""
        _clear_manifest_cache()
        manifest = {
            "styles.css": {"file": "assets/styles-abc.css"},
            "app.js": {"file": "assets/app.js", "css": ["assets/app.css"]},
            "crm.js": {"file": "assets/crm.js", "css": ["assets/crm.css"]},
        }
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VITE_DEV", None)
            with patch.object(vite_mod, "_load_manifest", return_value=manifest):
                result = vite_css_tags()
                assert "assets/styles-abc.css" in str(result)
                assert "assets/app.css" in str(result)
                assert "assets/crm.css" in str(result)
                assert "<link" in str(result)

    def test_fallback_raw_source(self):
        """Without manifest and not in VITE_DEV, uses raw source path."""
        _clear_manifest_cache()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VITE_DEV", None)
            with patch.object(vite_mod, "_load_manifest", return_value=None):
                result = vite_css_tags(app_version="1.0")
                assert "/static/styles.css?v=1.0" in str(result)

    def test_fallback_no_version(self):
        """Fallback without version has no cache bust param."""
        _clear_manifest_cache()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VITE_DEV", None)
            with patch.object(vite_mod, "_load_manifest", return_value=None):
                result = vite_css_tags()
                assert "/static/styles.css" in str(result)
                assert "?v=" not in str(result)


# ── vite_js_tags ──────────────────────────────────────────────────────


class TestViteJsTags:
    def test_vite_dev_returns_dev_scripts(self):
        """In VITE_DEV mode, returns scripts pointing at dev server."""
        _clear_manifest_cache()
        with patch.dict(os.environ, {"VITE_DEV": "1"}):
            result = vite_js_tags()
            assert "localhost:5173" in str(result)
            assert "@vite/client" in str(result)
            assert "app.js" in str(result)
            assert "crm.js" in str(result)

    def test_production_with_manifest(self):
        """With manifest, returns script tags with hashed URLs including tickets.js."""
        _clear_manifest_cache()
        manifest = {
            "app.js": {"file": "assets/app-abc.js"},
            "crm.js": {"file": "assets/crm-def.js"},
            "tickets.js": {"file": "assets/tickets-ghi.js"},
            "touch.js": {"file": "assets/touch-jkl.js"},
        }
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VITE_DEV", None)
            with patch.object(vite_mod, "_load_manifest", return_value=manifest):
                result = vite_js_tags()
                assert "assets/app-abc.js" in str(result)
                assert "assets/crm-def.js" in str(result)
                assert "assets/tickets-ghi.js" in str(result)
                assert "assets/touch-jkl.js" in str(result)
                assert "<script" in str(result)

    def test_fallback_importmap(self):
        """Without manifest and not in VITE_DEV, uses importmap + raw source."""
        _clear_manifest_cache()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VITE_DEV", None)
            with patch.object(vite_mod, "_load_manifest", return_value=None):
                result = vite_js_tags(app_version="2.0")
                assert "importmap" in str(result)
                assert "/static/app.js?v=2.0" in str(result)
                assert "/static/crm.js?v=2.0" in str(result)

    def test_fallback_no_version(self):
        """Fallback without version has no cache bust param."""
        _clear_manifest_cache()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VITE_DEV", None)
            with patch.object(vite_mod, "_load_manifest", return_value=None):
                result = vite_js_tags()
                assert "/static/app.js" in str(result)
                assert "?v=" not in str(result)

    def test_manifest_with_tickets_and_touch(self):
        """With manifest including tickets.js and touch.js, all script tags emitted."""
        _clear_manifest_cache()
        manifest = {
            "app.js": {"file": "assets/app-abc.js"},
            "crm.js": {"file": "assets/crm-def.js"},
            "tickets.js": {"file": "assets/tickets-ghi.js"},
            "touch.js": {"file": "assets/touch-jkl.js"},
        }
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VITE_DEV", None)
            with patch.object(vite_mod, "_load_manifest", return_value=manifest):
                result = vite_js_tags()
                assert "assets/tickets-ghi.js" in str(result)
                assert "assets/touch-jkl.js" in str(result)

    def test_partial_manifest_falls_back(self):
        """If only one entry in manifest, falls back to importmap."""
        _clear_manifest_cache()
        manifest = {
            "app.js": {"file": "assets/app-abc.js"},
            # crm.js missing
        }
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VITE_DEV", None)
            with patch.object(vite_mod, "_load_manifest", return_value=manifest):
                result = vite_js_tags()
                # Since crm_url is None, falls to importmap
                assert "importmap" in str(result)


# ── vite_app_url / vite_crm_url ─────────────────────────────────────


class TestViteAppUrl:
    def test_manifest_hit_returns_hashed_url(self):
        """When manifest has app.js entry, return its hashed URL (line 75)."""
        _clear_manifest_cache()
        manifest = {"app.js": {"file": "assets/app-ha5h.js"}}
        with patch.object(vite_mod, "_load_manifest", return_value=manifest):
            result = vite_app_url("1.0")
            assert result == "/static/assets/app-ha5h.js"

    def test_no_manifest_falls_back(self):
        """Without manifest, returns raw source with bust param."""
        _clear_manifest_cache()
        with patch.object(vite_mod, "_load_manifest", return_value=None):
            result = vite_app_url("2.0")
            assert result == "/static/app.js?v=2.0"


class TestViteCrmUrl:
    def test_manifest_hit_returns_hashed_url(self):
        """When manifest has crm.js entry, return its hashed URL (line 84)."""
        _clear_manifest_cache()
        manifest = {"crm.js": {"file": "assets/crm-x9z.js"}}
        with patch.object(vite_mod, "_load_manifest", return_value=manifest):
            result = vite_crm_url("1.0")
            assert result == "/static/assets/crm-x9z.js"

    def test_no_manifest_falls_back(self):
        """Without manifest, returns raw source with bust param."""
        _clear_manifest_cache()
        with patch.object(vite_mod, "_load_manifest", return_value=None):
            result = vite_crm_url("3.0")
            assert result == "/static/crm.js?v=3.0"
