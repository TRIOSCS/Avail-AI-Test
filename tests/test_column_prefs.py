"""Tests for column preference save endpoint.

Called by: pytest
Depends on: app.routers.htmx_views.save_column_prefs
"""

from fastapi.testclient import TestClient

from tests.conftest import engine  # noqa: F401


def test_save_column_prefs_returns_html(client: TestClient):
    """POST /v2/partials/parts/column-prefs should save and return updated list."""
    resp = client.post(
        "/v2/partials/parts/column-prefs",
        data={"columns": ["mpn", "brand", "qty"]},
    )
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


def test_save_column_prefs_empty_defaults(client: TestClient):
    """Empty columns list should fall back to defaults."""
    resp = client.post(
        "/v2/partials/parts/column-prefs",
        data={},
    )
    assert resp.status_code == 200
