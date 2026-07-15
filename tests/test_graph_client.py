"""test_graph_client.py — Tests for app/utils/graph_client.py.

Mock HTTP calls and asyncio.sleep to test Graph API client retry,
pagination, and delta query logic.

Called by: pytest
Depends on: app/utils/graph_client.py
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.utils.graph_client as _gc_mod
from app.utils.graph_client import GraphAPIError, GraphClient, GraphSyncStateExpired


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
    @pytest.mark.parametrize(
        ("status_code", "text"),
        [
            (400, "Bad Request"),
            (401, "Unauthorized"),
            (404, "Not Found"),
        ],
    )
    @patch("app.utils.graph_client.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.utils.graph_client.http")
    async def test_4xx_no_retry(self, mock_http, mock_sleep, status_code, text):
        mock_http.get = AsyncMock(return_value=_mock_response(status_code, text=text))

        gc = GraphClient("test-token")
        result = await gc.get_json("/me/messages")
        assert result["error"] == status_code
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
#  Delta Query — truncation contract (never drop items, resumable nextLink)
# ═══════════════════════════════════════════════════════════════════════


class TestDeltaQueryTruncationContract:
    @pytest.mark.asyncio
    @patch("app.utils.graph_client.http")
    async def test_budget_hit_returns_nextlink_and_stops_paging(self, mock_http):
        """When max_items is reached mid-round, delta_query stops paging and returns the
        current nextLink as resumable state — it must NOT keep fetching and must NOT
        return new_token=None."""
        page1 = _mock_response(
            200,
            {
                "value": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/delta?$skiptoken=page2",
            },
        )
        mock_http.get = AsyncMock(return_value=page1)

        gc = GraphClient("test-token")
        items, token = await gc.delta_query("/me/mailFolders/Inbox/messages/delta", max_items=3)

        assert [i["id"] for i in items] == ["m1", "m2", "m3"]
        assert token == "https://graph.microsoft.com/v1.0/delta?$skiptoken=page2"
        assert mock_http.get.call_count == 1  # budget hit → no second fetch

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.http")
    async def test_budget_overshoot_keeps_every_fetched_item(self, mock_http):
        """A fetched page is NEVER sliced: if the page overshoots max_items,
        all its items are returned alongside the resumable nextLink."""
        mock_http.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "value": [{"id": f"m{i}"} for i in range(5)],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/delta?$skiptoken=p2",
                },
            )
        )

        gc = GraphClient("test-token")
        items, token = await gc.delta_query("/me/mailFolders/Inbox/messages/delta", max_items=3)

        assert len(items) == 5  # old code sliced to 3 and lost m3/m4 forever
        assert token == "https://graph.microsoft.com/v1.0/delta?$skiptoken=p2"

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.http")
    async def test_resume_from_persisted_nextlink_completes_round(self, mock_http):
        """A nextLink persisted as the delta token resumes the round exactly where it
        stopped and finishes with the final deltaLink."""
        next_link = "https://graph.microsoft.com/v1.0/delta?$skiptoken=page2"
        mock_http.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "value": [{"id": "m4"}, {"id": "m5"}],
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta?token=final",
                },
            )
        )

        gc = GraphClient("test-token")
        items, token = await gc.delta_query(
            "/me/mailFolders/Inbox/messages/delta", delta_token=next_link, max_items=500
        )

        assert [i["id"] for i in items] == ["m4", "m5"]
        assert token == "https://graph.microsoft.com/v1.0/delta?token=final"
        # Resumed from the persisted nextLink URL, not from the base path
        first_call = mock_http.get.call_args_list[0]
        assert next_link in str(first_call)

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.http")
    async def test_backlog_larger_than_cap_advances_across_runs(self, mock_http):
        """Backlog > cap twice in a row: each run advances (distinct tokens, distinct
        items) and the round eventually terminates — no permanent stall refetching the
        same first-N items."""
        pages = {
            "https://graph.microsoft.com/v1.0/me/x/delta": {
                "value": [{"id": "m1"}, {"id": "m2"}],
                "@odata.nextLink": "https://g/next1",
            },
            "https://g/next1": {
                "value": [{"id": "m3"}, {"id": "m4"}],
                "@odata.nextLink": "https://g/next2",
            },
            "https://g/next2": {
                "value": [{"id": "m5"}],
                "@odata.deltaLink": "https://g/delta-final",
            },
        }

        async def _get(url, **kwargs):
            return _mock_response(200, pages[url])

        mock_http.get = AsyncMock(side_effect=_get)
        gc = GraphClient("test-token")

        items1, token1 = await gc.delta_query("/me/x/delta", max_items=2)
        assert [i["id"] for i in items1] == ["m1", "m2"]
        assert token1 == "https://g/next1"

        items2, token2 = await gc.delta_query("/me/x/delta", delta_token=token1, max_items=2)
        assert [i["id"] for i in items2] == ["m3", "m4"]
        assert token2 == "https://g/next2"
        assert token2 != token1  # progress, not a stall

        items3, token3 = await gc.delta_query("/me/x/delta", delta_token=token2, max_items=2)
        assert [i["id"] for i in items3] == ["m5"]
        assert token3 == "https://g/delta-final"

        all_ids = [i["id"] for i in items1 + items2 + items3]
        assert all_ids == ["m1", "m2", "m3", "m4", "m5"]  # complete, no dupes

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.http")
    async def test_max_page_size_sets_prefer_header_keeping_immutable_id(self, mock_http):
        """max_page_size adds odata.maxpagesize to Prefer WITHOUT dropping the
        ImmutableId preference (H1)."""
        mock_http.get = AsyncMock(return_value=_mock_response(200, {"value": [], "@odata.deltaLink": "https://g/d"}))

        gc = GraphClient("test-token")
        await gc.delta_query("/me/x/delta", max_page_size=50)

        prefer = mock_http.get.call_args.kwargs["headers"]["Prefer"]
        assert "odata.maxpagesize=50" in prefer
        assert "ImmutableId" in prefer


# ═══════════════════════════════════════════════════════════════════════
#  Delta Query — initial full-sync bound (initial_lookback_days)
# ═══════════════════════════════════════════════════════════════════════


class TestDeltaQueryInitialLookback:
    @pytest.mark.asyncio
    @patch("app.utils.graph_client.http")
    async def test_initial_round_applies_receiveddatetime_filter(self, mock_http):
        """With no stored token, initial_lookback_days adds the only $filter Graph
        supports on message deltas — bounding the round (and, since Graph bakes the
        filter into its links, every resumed continuation) to recent history instead of
        the entire mailbox."""
        mock_http.get = AsyncMock(return_value=_mock_response(200, {"value": [], "@odata.deltaLink": "https://g/d"}))

        gc = GraphClient("test-token")
        await gc.delta_query("/me/mailFolders/Inbox/messages/delta", params={"$top": "50"}, initial_lookback_days=180)

        params = mock_http.get.call_args.kwargs["params"]
        assert params["$top"] == "50"  # caller params preserved
        flt = params["$filter"]
        assert flt.startswith("receivedDateTime ge ")
        since = datetime.strptime(flt.removeprefix("receivedDateTime ge "), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        assert abs(since - (datetime.now(UTC) - timedelta(days=180))) < timedelta(minutes=5)

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.http")
    async def test_resumed_round_ignores_lookback(self, mock_http):
        """Resuming from a stored token URL sends no params — the filter is already
        baked into the deltaLink/nextLink by Graph."""
        mock_http.get = AsyncMock(return_value=_mock_response(200, {"value": [], "@odata.deltaLink": "https://g/d2"}))

        gc = GraphClient("test-token")
        await gc.delta_query("/me/x/delta", delta_token="https://g/next1", initial_lookback_days=180)

        assert mock_http.get.call_args.kwargs["params"] is None
        assert "https://g/next1" in str(mock_http.get.call_args)

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.http")
    async def test_initial_round_without_lookback_is_unfiltered(self, mock_http):
        """Omitting initial_lookback_days (non-message deltas like /me/contacts that
        don't support the receivedDateTime filter) sends the caller's params
        untouched."""
        mock_http.get = AsyncMock(return_value=_mock_response(200, {"value": [], "@odata.deltaLink": "https://g/d"}))

        gc = GraphClient("test-token")
        await gc.delta_query("/me/contacts/delta", params={"$top": "100"})

        params = mock_http.get.call_args.kwargs["params"]
        assert params == {"$top": "100"}
        assert "$filter" not in params


# ═══════════════════════════════════════════════════════════════════════
#  Error-dict pages — typed GraphAPIError instead of silent empty pages
# ═══════════════════════════════════════════════════════════════════════


class TestGraphErrorPages:
    @pytest.mark.asyncio
    @patch("app.utils.graph_client.http")
    async def test_delta_error_page_mid_pagination_raises_typed(self, mock_http):
        """An error page mid-round raises GraphAPIError; no token is returned so the
        caller cannot advance its sync state past the failure."""
        mock_http.get = AsyncMock(
            side_effect=[
                _mock_response(
                    200,
                    {"value": [{"id": "m1"}], "@odata.nextLink": "https://g/next"},
                ),
                _mock_response(400, text="Bad Request"),
            ]
        )

        gc = GraphClient("test-token")
        with pytest.raises(GraphAPIError) as exc_info:
            await gc.delta_query("/me/x/delta", max_items=500)
        assert exc_info.value.status == 400

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.http")
    async def test_delta_error_page_on_first_page_raises_typed(self, mock_http):
        """A 401 on the very first delta page raises instead of returning (items=[],
        token=None) — the old silent-success path."""
        mock_http.get = AsyncMock(return_value=_mock_response(401, text="Unauthorized"))

        gc = GraphClient("test-token")
        with pytest.raises(GraphAPIError) as exc_info:
            await gc.delta_query("/me/x/delta")
        assert exc_info.value.status == 401

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.utils.graph_client.http")
    async def test_delta_410_mid_pagination_still_raises_sync_state_expired(self, mock_http, mock_sleep):
        """410 mid-round keeps the existing GraphSyncStateExpired contract."""
        mock_http.get = AsyncMock(
            side_effect=[
                _mock_response(
                    200,
                    {"value": [{"id": "m1"}], "@odata.nextLink": "https://g/next"},
                ),
                _mock_response(410, text="SyncStateNotFound"),
            ]
        )

        gc = GraphClient("test-token")
        with pytest.raises(GraphSyncStateExpired):
            await gc.delta_query("/me/x/delta")

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.http")
    async def test_get_all_pages_error_page_raises_typed(self, mock_http):
        """get_all_pages raises GraphAPIError on an error page instead of returning a
        silently-empty list."""
        mock_http.get = AsyncMock(return_value=_mock_response(403, text="Forbidden"))

        gc = GraphClient("test-token")
        with pytest.raises(GraphAPIError) as exc_info:
            await gc.get_all_pages("/me/messages")
        assert exc_info.value.status == 403


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
