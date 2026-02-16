"""
test_api_versioning.py — Tests for API version prefix middleware.

Verifies that /api/v1/... paths are rewritten to /api/... internally,
old /api/... paths still work, and X-API-Version header is set.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from app.main import app
    return TestClient(app)


class TestApiVersionMiddleware:
    """Test the /api/v1/ rewrite middleware."""

    def test_health_returns_version_header(self, client: TestClient):
        resp = client.get("/health")
        assert resp.headers.get("X-API-Version") == "v1"

    def test_old_api_path_still_works(self, client: TestClient):
        resp = client.get("/api/admin/health")
        assert resp.status_code in (200, 401, 403)
        assert resp.headers.get("X-API-Version") == "v1"

    def test_v1_prefix_rewrites_to_api(self, client: TestClient):
        """GET /api/v1/admin/health should reach the same endpoint as /api/admin/health."""
        resp = client.get("/api/v1/admin/health")
        assert resp.status_code in (200, 401, 403)
        assert resp.headers.get("X-API-Version") == "v1"

    def test_v1_prefix_on_nonexistent_returns_404(self, client: TestClient):
        resp = client.get("/api/v1/does-not-exist-12345")
        assert resp.status_code == 404

    def test_non_api_paths_unaffected(self, client: TestClient):
        """Static/root paths should not be rewritten."""
        resp = client.get("/health")
        assert resp.status_code == 200
