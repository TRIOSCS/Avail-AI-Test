"""test_api_versioning.py — Tests for API version prefix middleware.

Verifies that /api/v1/... paths are rewritten to /api/... internally,
old /api/... paths still work, and X-API-Version header is set.

Called by: pytest
Depends on: conftest.py client fixture (uses test DB + auth overrides)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


class TestApiVersionMiddleware:
    """Test the /api/v1/ rewrite middleware."""

    def test_health_returns_version_header(self, client: TestClient):
        resp = client.get("/health")
        assert resp.headers.get("X-API-Version") == "v1"

    @pytest.mark.parametrize(
        "path",
        [
            "/api/admin/health",  # old /api/... path still works
            "/api/v1/admin/health",  # /api/v1/ rewrites to the same endpoint
        ],
        ids=["old_api_path", "v1_prefix_rewrites"],
    )
    def test_admin_health_reachable_with_version_header(self, client: TestClient, path: str):
        resp = client.get(path)
        assert resp.status_code in (200, 401, 403)
        assert resp.headers.get("X-API-Version") == "v1"

    def test_v1_prefix_on_nonexistent_returns_404(self, client: TestClient):
        resp = client.get("/api/v1/does-not-exist-12345")
        assert resp.status_code == 404

    def test_non_api_paths_unaffected(self, client: TestClient):
        """Static/root paths should not be rewritten."""
        resp = client.get("/health")
        assert resp.status_code in (200, 503)  # 503 when degraded (no Redis/scheduler in test)
