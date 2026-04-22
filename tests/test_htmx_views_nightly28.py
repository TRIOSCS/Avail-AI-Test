"""tests/test_htmx_views_nightly28.py — Targeted helper-function coverage for
htmx_views.py.

Covers previously untested private helpers and v2_page requisitions path:
  - _vite_assets()          line 100  (styles file prepended when absent from htmx_app.js css)
  - _staleness_tier()       lines 4279-4289 (all staleness tiers + naive datetime branch)
  - _sanitize_hx_params()   lines 4270-4273 (invalid hx_target + invalid push_url_base)
  - v2_page                 lines 209-212, 217, 228-230 (requisitions path branch)

Called by: pytest autodiscovery (asyncio_mode = auto)
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


class TestViteAssetsHelper:
    """Tests for _vite_assets() — particularly the styles-file-prepend branch (line
    100)."""

    def test_styles_file_prepended_when_absent_from_js_css(self):
        """Line 100: styles entry prepended when htmx_app.js css list lacks it."""
        from app.routers.htmx_views import _vite_assets

        mock_manifest = {
            "htmx_app.js": {"file": "assets/htmx_app-ABC.js", "css": ["assets/mobile.css"]},
            "styles.css": {"file": "assets/styles-XYZ.css"},
        }
        with patch("app.routers.htmx_views._vite_manifest", mock_manifest):
            result = _vite_assets()

        assert result["js_file"] == "assets/htmx_app-ABC.js"
        assert result["css_files"][0] == "assets/styles-XYZ.css"
        assert "assets/mobile.css" in result["css_files"]

    def test_styles_file_not_duplicated_when_already_present(self):
        """No duplication: styles file already in htmx_app.js css list → line 100 skipped."""
        from app.routers.htmx_views import _vite_assets

        mock_manifest = {
            "htmx_app.js": {"file": "assets/htmx_app-ABC.js", "css": ["assets/styles-XYZ.css"]},
            "styles.css": {"file": "assets/styles-XYZ.css"},
        }
        with patch("app.routers.htmx_views._vite_manifest", mock_manifest):
            result = _vite_assets()

        assert result["css_files"].count("assets/styles-XYZ.css") == 1

    def test_empty_manifest_returns_defaults(self):
        """Empty manifest falls back to default asset paths."""
        from app.routers.htmx_views import _vite_assets

        with patch("app.routers.htmx_views._vite_manifest", {}):
            result = _vite_assets()

        assert result["js_file"] == "assets/htmx_app.js"
        assert result["css_files"] == []


class TestStalenessHelper:
    """Tests for _staleness_tier() — all four return values."""

    def test_none_returns_new(self):
        from app.routers.htmx_views import _staleness_tier

        assert _staleness_tier(None) == "new"

    def test_recent_timestamp(self):
        from app.routers.htmx_views import _staleness_tier

        ts = datetime.now(timezone.utc) - timedelta(days=5)
        assert _staleness_tier(ts) == "recent"

    def test_due_soon_timestamp(self):
        from app.routers.htmx_views import _staleness_tier

        ts = datetime.now(timezone.utc) - timedelta(days=20)
        assert _staleness_tier(ts) == "due_soon"

    def test_overdue_timestamp(self):
        from app.routers.htmx_views import _staleness_tier

        ts = datetime.now(timezone.utc) - timedelta(days=40)
        assert _staleness_tier(ts) == "overdue"

    def test_naive_datetime_treated_as_utc(self):
        """Naive datetime (no tzinfo) → line 4283 adds UTC, staleness computed
        correctly."""
        from app.routers.htmx_views import _staleness_tier

        naive = datetime.now() - timedelta(days=40)
        assert naive.tzinfo is None
        result = _staleness_tier(naive)
        assert result == "overdue"


class TestSanitizeHxParams:
    """Tests for _sanitize_hx_params() — allowlist enforcement."""

    def test_invalid_hx_target_replaced_with_default(self):
        """Line 4271: invalid hx_target replaced with '#main-content'."""
        from app.routers.htmx_views import _sanitize_hx_params

        target, push = _sanitize_hx_params("evil-target", "/v2/vendors", "/v2/vendors")
        assert target == "#main-content"
        assert push == "/v2/vendors"

    def test_invalid_push_url_replaced_with_default(self):
        """Line 4273: invalid push_url_base replaced with default_push arg."""
        from app.routers.htmx_views import _sanitize_hx_params

        target, push = _sanitize_hx_params("#main-content", "/evil/path", "/v2/vendors")
        assert target == "#main-content"
        assert push == "/v2/vendors"

    def test_both_invalid_both_replaced(self):
        from app.routers.htmx_views import _sanitize_hx_params

        target, push = _sanitize_hx_params("bad", "bad", "/v2/customers")
        assert target == "#main-content"
        assert push == "/v2/customers"

    def test_valid_values_pass_through_unchanged(self):
        from app.routers.htmx_views import _sanitize_hx_params

        target, push = _sanitize_hx_params("#main-content", "/v2/vendors", "/v2/vendors")
        assert target == "#main-content"
        assert push == "/v2/vendors"

    def test_crm_tab_content_is_allowed(self):
        from app.routers.htmx_views import _sanitize_hx_params

        target, push = _sanitize_hx_params("#crm-tab-content", "/v2/customers", "/v2/vendors")
        assert target == "#crm-tab-content"
        assert push == "/v2/customers"


class TestV2PageRequisitionsPath:
    """Cover the v2_page branches for /v2/requisitions* paths.

    test_htmx_views_deep.py::TestV2PagePathVariants covers all paths except
    /v2/requisitions* — so lines 209-210, 212, 217, 228-230 remain uncovered.
    We must patch get_user (not require_user) because v2_page uses the session-based
    get_user helper, not the Depends-injected require_user.
    """

    def _get(self, client: TestClient, path: str, user) -> int:
        with patch("app.routers.htmx_views.get_user", return_value=user):
            resp = client.get(path)
        return resp.status_code

    def test_v2_requisitions_list(self, client: TestClient, test_user):
        """Lines 209-210, 217: elif '/requisitions' branch sets workspace partial."""
        assert self._get(client, "/v2/requisitions", test_user) == 200

    def test_v2_root_falls_through_to_else(self, client: TestClient, test_user):
        """Line 212: else branch fires for /v2 (no specific section)."""
        assert self._get(client, "/v2", test_user) == 200

    def test_v2_requisitions_detail(self, client: TestClient, db_session: Session, test_user):
        """Lines 228-230: requisitions detail URL sets partial to /v2/partials/requisitions/{id}."""
        from app.models import Requisition

        req = Requisition(name="Test Req", status="active", created_by=test_user.id)
        db_session.add(req)
        db_session.commit()
        db_session.refresh(req)
        assert self._get(client, f"/v2/requisitions/{req.id}", test_user) == 200
