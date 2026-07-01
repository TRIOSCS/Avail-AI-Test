"""Retirement of the /requisitions2 opportunity-table surface.

The hidden parallel requisitions view was retired (router → redirect; templates + its 4 test
files removed). Its old URLs now 302-redirect to the canonical /v2/requisitions Sales Hub so
stale bookmarks keep working.

Called by: pytest
Depends on: app.routers.requisitions2 (redirect-only), conftest (client).
"""

from fastapi.testclient import TestClient


def test_requisitions2_root_redirects(client: TestClient):
    resp = client.get("/requisitions2", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/v2/requisitions"


def test_requisitions2_subpath_redirects(client: TestClient):
    """Any old sub-URL (table/detail/action/…) redirects to the canonical surface
    too."""
    resp = client.get("/requisitions2/table", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/v2/requisitions"
