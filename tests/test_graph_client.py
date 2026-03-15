"""test_graph_client.py — Tests for app/utils/graph_client.py.

Mock HTTP calls and asyncio.sleep to test Graph API client retry,
pagination, and delta query logic.

Called by: pytest
Depends on: app/utils/graph_client.py
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.utils.graph_client as _gc_mod
from app.utils.graph_client import GraphClient, GraphSyncStateExpired


@pytest.fixture(autouse=True)
def _restore_retry_constants():
    """Restore production retry constants for graph client tests."""
    orig_retries, orig_backoff = _gc_mod.MAX_RETRIES, _gc_mod.BACKOFF_BASE
    _gc_mod.MAX_RETRIES = 3
    _gc_mod.BACKOFF_BASE = 2
    yield
    _gc_mod.MAX_RETRIES, _gc_mod.BACKOFF_BASE = orig_retries, orig_backoff


# ── Helpers ─────────────────────────────────────────────────────────


def _mock_response(status_code=200, json_data=None, text="", headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = text
    resp.headers = headers or {}
    return resp


# ═══════════════════════════════════════════════════════════════════════
#  GET requests
# ═══════════════════════════════════════════════════════════════════════


class TestGraphClientGet:
    @pytest.mark.asyncio
    @patch("app.utils.graph_client.http")
    async def test_200_returns_json(self, mock_http):
        mock_http.get = AsyncMock(return_value=_mock_response(200, {"value": [1, 2]}))

        gc = GraphClient("test-token")
        result = await gc.get_json("/me/messages")
        assert result == {"value": [1, 2]}

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.http")
    async def test_bearer_header_included(self, mock_http):
        mock_http.get = AsyncMock(return_value=_mock_response(200, {}))

        gc = GraphClient("my-token")
        await gc.get_json("/me/messages")

        call_kwargs = mock_http.get.call_args.kwargs
        assert "Bearer my-token" in call_kwargs["headers"]["Authorization"]

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.http")
    async def test_immutable_id_header(self, mock_http):
        mock_http.get = AsyncMock(return_value=_mock_response(200, {}))

        gc = GraphClient("test-token")
        await gc.get_json("/me/messages")

        call_kwargs = mock_http.get.call_args.kwargs
        assert "ImmutableId" in call_kwargs["headers"]["Prefer"]


# ═══════════════════════════════════════════════════════════════════════
#  Retry logic
# ═══════════════════════════════════════════════════════════════════════


class TestGraphClientRetry:
    @pytest.mark.asyncio
    @patch("app.utils.graph_client.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.utils.graph_client.http")
    async def test_429_retries_with_retry_after(self, mock_http, mock_sleep):
        mock_http.get = AsyncMock(
            side_effect=[
                _mock_response(429, headers={"Retry-After": "3"}),
                _mock_response(200, {"value": []}),
            ]
        )

        gc = GraphClient("test-token")
        result = await gc.get_json("/me/messages")
        assert result == {"value": []}
        mock_sleep.assert_called_with(3)

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.utils.graph_client.http")
    async def test_5xx_exponential_backoff(self, mock_http, mock_sleep):
        mock_http.get = AsyncMock(
            side_effect=[
                _mock_response(503, text="Service Unavailable"),
                _mock_response(200, {"value": []}),
            ]
        )

        gc = GraphClient("test-token")
        result = await gc.get_json("/me/messages")
        assert result == {"value": []}
        # First attempt (attempt=0): backoff = 2^(0+1) = 2
        mock_sleep.assert_called_with(2)

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.utils.graph_client.http")
    async def test_400_no_retry(self, mock_http, mock_sleep):
        mock_http.get = AsyncMock(return_value=_mock_response(400, text="Bad Request"))

        gc = GraphClient("test-token")
        result = await gc.get_json("/me/messages")
        assert result["error"] == 400
        mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.utils.graph_client.http")
    async def test_401_no_retry(self, mock_http, mock_sleep):
        mock_http.get = AsyncMock(return_value=_mock_response(401, text="Unauthorized"))

        gc = GraphClient("test-token")
        result = await gc.get_json("/me/messages")
        assert result["error"] == 401
        mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.utils.graph_client.http")
    async def test_404_no_retry(self, mock_http, mock_sleep):
        mock_http.get = AsyncMock(return_value=_mock_response(404, text="Not Found"))

        gc = GraphClient("test-token")
        result = await gc.get_json("/me/messages")
        assert result["error"] == 404
        mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.utils.graph_client.http")
    async def test_410_raises_sync_state_expired(self, mock_http, mock_sleep):
        mock_http.get = AsyncMock(return_value=_mock_response(410, text="SyncStateNotFound"))

        gc = GraphClient("test-token")
        with pytest.raises(GraphSyncStateExpired):
            await gc.get_json("/me/messages/delta")

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.utils.graph_client.http")
    async def test_max_retries_exhausted(self, mock_http, mock_sleep):
        mock_http.get = AsyncMock(side_effect=ConnectionError("refused"))

        gc = GraphClient("test-token")
        with pytest.raises(ConnectionError):
            await gc.get_json("/me/messages")
        # 3 retries + 1 initial = 4 total attempts
        assert mock_http.get.call_count == 4


# ═══════════════════════════════════════════════════════════════════════
#  POST requests
# ═══════════════════════════════════════════════════════════════════════


class TestGraphClientPost:
    @pytest.mark.asyncio
    @patch("app.utils.graph_client.http")
    async def test_200_returns_json(self, mock_http):
        mock_http.post = AsyncMock(return_value=_mock_response(200, {"id": "msg-1"}))

        gc = GraphClient("test-token")
        result = await gc.post_json("/me/sendMail", {"message": {}})
        assert result == {"id": "msg-1"}

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.http")
    async def test_202_returns_empty_dict(self, mock_http):
        mock_http.post = AsyncMock(return_value=_mock_response(202))

        gc = GraphClient("test-token")
        result = await gc.post_json("/me/sendMail", {"message": {}})
        assert result == {}


# ═══════════════════════════════════════════════════════════════════════
#  Pagination
# ═══════════════════════════════════════════════════════════════════════


class TestGraphClientPagination:
    @pytest.mark.asyncio
    @patch("app.utils.graph_client.http")
    async def test_follows_next_link(self, mock_http):
        mock_http.get = AsyncMock(
            side_effect=[
                _mock_response(
                    200,
                    {
                        "value": [{"id": 1}, {"id": 2}],
                        "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/messages?$skip=2",
                    },
                ),
                _mock_response(
                    200,
                    {
                        "value": [{"id": 3}],
                    },
                ),
            ]
        )

        gc = GraphClient("test-token")
        items = await gc.get_all_pages("/me/messages")
        assert len(items) == 3

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.http")
    async def test_max_items_cap(self, mock_http):
        mock_http.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "value": [{"id": i} for i in range(100)],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/next",
                },
            )
        )

        gc = GraphClient("test-token")
        items = await gc.get_all_pages("/me/messages", max_items=50)
        assert len(items) == 50


# ═══════════════════════════════════════════════════════════════════════
#  Delta Query
# ═══════════════════════════════════════════════════════════════════════


class TestGraphClientDelta:
    @pytest.mark.asyncio
    @patch("app.utils.graph_client.http")
    async def test_initial_sync_returns_items_and_token(self, mock_http):
        mock_http.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "value": [{"id": "m1"}, {"id": "m2"}],
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta?token=abc",
                },
            )
        )

        gc = GraphClient("test-token")
        items, token = await gc.delta_query("/me/mailFolders/Inbox/messages/delta")
        assert len(items) == 2
        assert token == "https://graph.microsoft.com/v1.0/delta?token=abc"

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.http")
    async def test_incremental_sync_uses_token(self, mock_http):
        mock_http.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "value": [{"id": "m3"}],
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta?token=def",
                },
            )
        )

        gc = GraphClient("test-token")
        old_token = "https://graph.microsoft.com/v1.0/delta?token=abc"
        items, new_token = await gc.delta_query("/me/mailFolders/Inbox/messages/delta", delta_token=old_token)

        assert len(items) == 1
        # Should have called with the old token URL directly
        call_url = mock_http.get.call_args.kwargs.get("url") or mock_http.get.call_args[1].get(
            "url", mock_http.get.call_args[0][0] if mock_http.get.call_args[0] else ""
        )
        # The GraphClient should use the delta_token as the URL
        first_call_args = mock_http.get.call_args_list[0]
        assert old_token in str(first_call_args)

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.http")
    async def test_delta_pagination_nextlink_before_deltalink(self, mock_http):
        mock_http.get = AsyncMock(
            side_effect=[
                _mock_response(
                    200,
                    {
                        "value": [{"id": "m1"}],
                        "@odata.nextLink": "https://graph.microsoft.com/v1.0/next",
                    },
                ),
                _mock_response(
                    200,
                    {
                        "value": [{"id": "m2"}],
                        "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta?token=final",
                    },
                ),
            ]
        )

        gc = GraphClient("test-token")
        items, token = await gc.delta_query("/me/mailFolders/Inbox/messages/delta")
        assert len(items) == 2
        assert token == "https://graph.microsoft.com/v1.0/delta?token=final"
        assert mock_http.get.call_count == 2


# ═══════════════════════════════════════════════════════════════════════
#  Additional coverage — 204 response (line 150) and max_retries return (line 189)
# ═══════════════════════════════════════════════════════════════════════


class TestGraphClientAdditional:
    @pytest.mark.asyncio
    @patch("app.utils.graph_client.http")
    async def test_204_returns_empty_dict(self, mock_http):
        """204 No Content returns empty dict (line 150)."""
        mock_http.get = AsyncMock(return_value=_mock_response(204))

        gc = GraphClient("test-token")
        result = await gc.get_json("/me/contacts/delete-something")
        assert result == {}

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.utils.graph_client.http")
    async def test_max_retries_exhausted_5xx_returns_error(self, mock_http, mock_sleep):
        """After max retries on 5xx, returns error dict (line 189)."""
        mock_http.get = AsyncMock(return_value=_mock_response(503, text="Service Unavailable"))

        gc = GraphClient("test-token")
        result = await gc.get_json("/me/messages")
        # After MAX_RETRIES+1 attempts of 503, should return error dict
        assert result["error"] == "max_retries" or result["error"] == 503
        assert mock_http.get.call_count >= 2  # At least initial + retries

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.http")
    async def test_201_returns_json(self, mock_http):
        """201 Created returns JSON (same as 200)."""
        mock_http.post = AsyncMock(return_value=_mock_response(201, {"id": "new-1"}))

        gc = GraphClient("test-token")
        result = await gc.post_json("/me/calendars", {"name": "Test"})
        assert result == {"id": "new-1"}

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.http")
    async def test_full_url_not_prefixed(self, mock_http):
        """URLs starting with http are used as-is, not prefixed."""
        mock_http.get = AsyncMock(return_value=_mock_response(200, {"value": []}))

        gc = GraphClient("test-token")
        await gc.get_json("https://graph.microsoft.com/v1.0/custom/path")

        call_args = mock_http.get.call_args
        assert "https://graph.microsoft.com/v1.0/custom/path" in str(call_args)

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.utils.graph_client.http")
    async def test_429_default_backoff(self, mock_http, mock_sleep):
        """429 without Retry-After header uses exponential backoff."""
        mock_http.get = AsyncMock(
            side_effect=[
                _mock_response(429, headers={}),
                _mock_response(200, {"value": []}),
            ]
        )

        gc = GraphClient("test-token")
        result = await gc.get_json("/me/messages")
        assert result == {"value": []}
        # Default backoff: 2^(0+1) = 2
        mock_sleep.assert_called()
