"""Tests for app.services.teams_notifications — Teams channel and DM helpers.

Covers post_teams_channel() and send_teams_dm() with mocked external calls
(webhook HTTP, Graph API, credential service, token refresh).

Called by: pytest
Depends on: app.services.teams_notifications, unittest.mock
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from loguru import logger

# ---------------------------------------------------------------------------
# post_teams_channel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_teams_channel_skips_when_no_webhook():
    """Silently returns when TEAMS_WEBHOOK_URL is not configured."""
    captured = []
    sink_id = logger.add(lambda msg: captured.append(str(msg)), level="DEBUG")
    try:
        with patch(
            "app.services.teams_notifications.get_credential_cached",
            return_value=None,
        ):
            from app.services.teams_notifications import post_teams_channel

            await post_teams_channel("hello")
        assert any("not configured" in m for m in captured)
    finally:
        logger.remove(sink_id)


@pytest.mark.asyncio
async def test_post_teams_channel_success():
    """Posts adaptive card JSON to the webhook URL on success."""
    mock_resp = MagicMock(status_code=200, text="ok")
    mock_http_post = AsyncMock(return_value=mock_resp)

    with (
        patch(
            "app.services.teams_notifications.get_credential_cached",
            return_value="https://outlook.office.com/webhook/test",
        ),
        patch("app.services.teams_notifications.http") as mock_http,
    ):
        mock_http.post = mock_http_post
        from app.services.teams_notifications import post_teams_channel

        await post_teams_channel("Buy plan approved")

    mock_http_post.assert_called_once()
    call_kwargs = mock_http_post.call_args
    payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
    # Verify adaptive card structure
    assert payload["type"] == "message"
    assert len(payload["attachments"]) == 1
    card = payload["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard"
    assert card["body"][0]["text"] == "Buy plan approved"


@pytest.mark.asyncio
async def test_post_teams_channel_accepts_202():
    """202 Accepted is also treated as success (no warning logged)."""
    mock_resp = MagicMock(status_code=202, text="accepted")
    mock_http_post = AsyncMock(return_value=mock_resp)
    captured = []
    sink_id = logger.add(lambda msg: captured.append(str(msg)), level="WARNING")
    try:
        with (
            patch(
                "app.services.teams_notifications.get_credential_cached",
                return_value="https://outlook.office.com/webhook/test",
            ),
            patch("app.services.teams_notifications.http") as mock_http,
        ):
            mock_http.post = mock_http_post
            from app.services.teams_notifications import post_teams_channel

            await post_teams_channel("test 202")

        assert not any("webhook returned" in m for m in captured)
    finally:
        logger.remove(sink_id)


@pytest.mark.asyncio
async def test_post_teams_channel_logs_warning_on_bad_status():
    """Non-200/202 status codes are logged as warnings."""
    mock_resp = MagicMock(status_code=400, text="Bad Request")
    mock_http_post = AsyncMock(return_value=mock_resp)
    captured = []
    sink_id = logger.add(lambda msg: captured.append(str(msg)), level="WARNING")
    try:
        with (
            patch(
                "app.services.teams_notifications.get_credential_cached",
                return_value="https://outlook.office.com/webhook/test",
            ),
            patch("app.services.teams_notifications.http") as mock_http,
        ):
            mock_http.post = mock_http_post
            from app.services.teams_notifications import post_teams_channel

            await post_teams_channel("fail")

        assert any("webhook returned" in m for m in captured)
    finally:
        logger.remove(sink_id)


@pytest.mark.asyncio
async def test_post_teams_channel_catches_exception():
    """Network errors are caught and logged, not raised."""
    mock_http_post = AsyncMock(side_effect=ConnectionError("network down"))
    captured = []
    sink_id = logger.add(lambda msg: captured.append(str(msg)), level="ERROR")
    try:
        with (
            patch(
                "app.services.teams_notifications.get_credential_cached",
                return_value="https://outlook.office.com/webhook/test",
            ),
            patch("app.services.teams_notifications.http") as mock_http,
        ):
            mock_http.post = mock_http_post
            from app.services.teams_notifications import post_teams_channel

            await post_teams_channel("boom")

        assert any("channel post failed" in m for m in captured)
    finally:
        logger.remove(sink_id)


# ---------------------------------------------------------------------------
# send_teams_dm
# ---------------------------------------------------------------------------


def _make_user(email="buyer@trioscs.com", access_token="tok-123"):
    """Create a lightweight user-like object for DM tests."""
    return SimpleNamespace(email=email, access_token=access_token)


@pytest.mark.asyncio
async def test_send_teams_dm_skips_no_token_no_db():
    """Skips DM when user has no access_token and no db session provided."""
    user = _make_user(access_token=None)
    captured = []
    sink_id = logger.add(lambda msg: captured.append(str(msg)), level="DEBUG")
    try:
        from app.services.teams_notifications import send_teams_dm

        await send_teams_dm(user, "hello")
        assert any("No token" in m for m in captured)
    finally:
        logger.remove(sink_id)


@pytest.mark.asyncio
async def test_send_teams_dm_skips_when_token_refresh_returns_none():
    """Skips DM when get_valid_token returns None (expired, no refresh)."""
    user = _make_user(access_token=None)
    mock_db = MagicMock()
    captured = []
    sink_id = logger.add(lambda msg: captured.append(str(msg)), level="DEBUG")
    try:
        with (
            patch(
                "app.services.teams_notifications.GraphClient",
                create=True,
            ),
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            from app.services.teams_notifications import send_teams_dm

            await send_teams_dm(user, "hello", db=mock_db)
        assert any("No valid token" in m for m in captured)
    finally:
        logger.remove(sink_id)


@pytest.mark.asyncio
async def test_send_teams_dm_uses_access_token_when_no_db():
    """Uses user.access_token directly when no db session is provided."""
    user = _make_user(access_token="direct-token")
    mock_gc_instance = MagicMock()
    mock_gc_instance.post_json = AsyncMock(
        side_effect=[
            {"id": "chat-id-123"},  # /chats response
            {},  # /chats/{id}/messages response
        ]
    )

    with patch(
        "app.utils.graph_client.GraphClient",
        return_value=mock_gc_instance,
    ):
        from app.services.teams_notifications import send_teams_dm

        await send_teams_dm(user, "notification text")

    # Verify GraphClient was constructed with the user's token
    mock_gc_instance.post_json.assert_any_call(
        "/chats",
        {
            "chatType": "oneOnOne",
            "members": [
                {
                    "@odata.type": "#microsoft.graph.aadUserConversationMember",
                    "roles": ["owner"],
                    "user@odata.bind": f"https://graph.microsoft.com/v1.0/users/{user.email}",
                }
            ],
        },
    )
    mock_gc_instance.post_json.assert_any_call(
        "/chats/chat-id-123/messages",
        {"body": {"content": "notification text"}},
    )


@pytest.mark.asyncio
async def test_send_teams_dm_refreshes_token_via_db():
    """When db is provided, uses get_valid_token for a fresh token."""
    user = _make_user(access_token=None)
    mock_db = MagicMock()
    mock_gc_instance = MagicMock()
    mock_gc_instance.post_json = AsyncMock(
        side_effect=[
            {"id": "chat-abc"},
            {},
        ]
    )

    with (
        patch(
            "app.utils.graph_client.GraphClient",
            return_value=mock_gc_instance,
        ),
        patch(
            "app.scheduler.get_valid_token",
            new_callable=AsyncMock,
            return_value="refreshed-token",
        ),
    ):
        from app.services.teams_notifications import send_teams_dm

        await send_teams_dm(user, "dm text", db=mock_db)

    assert mock_gc_instance.post_json.call_count == 2


@pytest.mark.asyncio
async def test_send_teams_dm_skips_message_when_no_chat_id():
    """If /chats returns no id, the message post is skipped."""
    user = _make_user()
    mock_gc_instance = MagicMock()
    mock_gc_instance.post_json = AsyncMock(return_value={})  # no "id" key

    with patch(
        "app.utils.graph_client.GraphClient",
        return_value=mock_gc_instance,
    ):
        from app.services.teams_notifications import send_teams_dm

        await send_teams_dm(user, "should not send message")

    # Only one call (/chats), no second call for messages
    assert mock_gc_instance.post_json.call_count == 1


@pytest.mark.asyncio
async def test_send_teams_dm_catches_exception():
    """Graph API errors are caught and logged as warnings."""
    user = _make_user()
    captured = []
    sink_id = logger.add(lambda msg: captured.append(str(msg)), level="WARNING")
    try:
        with patch(
            "app.utils.graph_client.GraphClient",
            side_effect=RuntimeError("graph unavailable"),
        ):
            from app.services.teams_notifications import send_teams_dm

            await send_teams_dm(user, "boom")

        assert any("failed" in m and "Chat permissions" in m for m in captured)
    finally:
        logger.remove(sink_id)
