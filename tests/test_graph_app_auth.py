import os

os.environ["TESTING"] = "1"
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import graph_app_auth as gaa


@pytest.fixture(autouse=True)
def _reset_cache():
    gaa._TOKEN_CACHE.clear()
    yield
    gaa._TOKEN_CACHE.clear()


async def test_returns_none_without_creds():
    with patch.object(gaa.settings, "azure_client_id", ""), patch.object(gaa.settings, "azure_tenant_id", ""):
        assert await gaa.get_app_graph_token() is None


async def test_acquires_and_caches_token():
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"access_token": "APPTOK", "expires_in": 3600}
    with (
        patch.object(gaa.settings, "azure_client_id", "cid"),
        patch.object(gaa.settings, "azure_client_secret", "sec"),
        patch.object(gaa.settings, "azure_tenant_id", "tid"),
        patch("app.services.graph_app_auth.http") as http,
    ):
        http.post = AsyncMock(return_value=resp)
        t1 = await gaa.get_app_graph_token()
        t2 = await gaa.get_app_graph_token()
    assert t1 == "APPTOK" and t2 == "APPTOK"
    assert http.post.call_count == 1  # second call served from cache
