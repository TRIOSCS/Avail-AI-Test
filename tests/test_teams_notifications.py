"""Tests for app.services.teams_notifications — Teams channel and DM helpers.

Covers post_teams_channel() and send_teams_dm() with mocked external calls
(webhook HTTP, Graph API, credential service, token refresh).

Called by: pytest
Depends on: app.services.teams_notifications, unittest.mock
"""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from loguru import logger


@contextmanager
def _capture_logs(level):
    """Capture loguru messages at the given level into a list yielded to the caller."""
    captured = []
    sink_id = logger.add(lambda msg: captured.append(str(msg)), level=level)
    try:
        yield captured
    finally:
        logger.remove(sink_id)


# ---------------------------------------------------------------------------
# post_teams_channel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_teams_channel_skips_when_no_webhook():
    """Silently returns when TEAMS_WEBHOOK_URL is not configured."""
    with _capture_logs("DEBUG") as captured:
        with patch(
            "app.services.teams_notifications.get_credential_cached",
            return_value=None,
        ):
            from app.services.teams_notifications import post_teams_channel

            await post_teams_channel("hello")
        assert any("not configured" in m for m in captured)


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
    with _capture_logs("WARNING") as captured:
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


@pytest.mark.asyncio
async def test_post_teams_channel_logs_warning_on_bad_status():
    """Non-200/202 status codes are logged as warnings."""
    mock_resp = MagicMock(status_code=400, text="Bad Request")
    mock_http_post = AsyncMock(return_value=mock_resp)
    with _capture_logs("WARNING") as captured:
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


@pytest.mark.asyncio
async def test_post_teams_channel_catches_exception():
    """Network errors are caught and logged, not raised."""
    mock_http_post = AsyncMock(side_effect=ConnectionError("network down"))
    with _capture_logs("ERROR") as captured:
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


# ---------------------------------------------------------------------------
# send_teams_dm
# ---------------------------------------------------------------------------


GRAPH_USERS = "https://graph.microsoft.com/v1.0/users"

SENDER_ME = {
    "id": "sender-guid-1",
    "mail": "notifier@trioscs.com",
    "userPrincipalName": "notifier@trioscs.com",
}


def _make_user(email="buyer@trioscs.com", access_token="tok-123"):
    """Create a lightweight user-like object for DM tests."""
    return SimpleNamespace(email=email, access_token=access_token)


def _make_gc(me=SENDER_ME, chat={"id": "chat-id-123"}):
    """Mock GraphClient instance: /me identity + chat-create/message-post responses."""
    gc = MagicMock()
    gc.get_json = AsyncMock(return_value=me)
    gc.post_json = AsyncMock(side_effect=[chat, {}])
    return gc


@pytest.mark.asyncio
async def test_send_teams_dm_skips_no_token_no_db():
    """Skips DM when user has no access_token and no db session provided."""
    user = _make_user(access_token=None)
    with _capture_logs("DEBUG") as captured:
        from app.services.teams_notifications import send_teams_dm

        await send_teams_dm(user, "hello")
        assert any("No token" in m for m in captured)


@pytest.mark.asyncio
async def test_send_teams_dm_skips_when_token_refresh_returns_none():
    """Skips DM when get_valid_token returns None (expired, no refresh)."""
    user = _make_user(access_token=None)
    mock_db = MagicMock()
    with _capture_logs("DEBUG") as captured:
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


@pytest.mark.asyncio
async def test_send_teams_dm_creates_chat_with_two_members():
    """POST /chats carries BOTH members: token owner (by AAD id) + recipient (by email).

    Graph rejects oneOnOne chats with fewer than 2 members ("Creation of 'OneOnOne' chat
    requires 2 members"), so the payload must bind the sender resolved via /me AND the
    recipient.
    """
    user = _make_user(access_token="direct-token")
    mock_gc_instance = _make_gc()

    with patch(
        "app.utils.graph_client.GraphClient",
        return_value=mock_gc_instance,
    ) as mock_gc_cls:
        from app.services.teams_notifications import send_teams_dm

        await send_teams_dm(user, "notification text")

    # GraphClient constructed with the user's token; sender resolved via /me
    mock_gc_cls.assert_called_once_with("direct-token")
    mock_gc_instance.get_json.assert_awaited_once_with("/me")

    chat_call = mock_gc_instance.post_json.await_args_list[0]
    assert chat_call.args[0] == "/chats"
    payload = chat_call.args[1]
    assert payload["chatType"] == "oneOnOne"
    members = payload["members"]
    assert len(members) == 2
    for m in members:
        assert m["@odata.type"] == "#microsoft.graph.aadUserConversationMember"
        assert m["roles"] == ["owner"]
    assert members[0]["user@odata.bind"] == f"{GRAPH_USERS}/sender-guid-1"
    assert members[1]["user@odata.bind"] == f"{GRAPH_USERS}/{user.email}"

    mock_gc_instance.post_json.assert_any_await(
        "/chats/chat-id-123/messages",
        {"body": {"content": "notification text"}},
    )


@pytest.mark.asyncio
async def test_send_teams_dm_self_chat_when_sender_is_recipient():
    """Sender == recipient → Graph self-chat: ONE member bound by the caller's AAD id.

    Duplicate members would be rejected, and the old email-bound single member is
    exactly the payload Graph 400s on. Email match is case-insensitive.
    """
    user = _make_user(email="Buyer@trioscs.com")
    me = {"id": "self-guid", "mail": "buyer@trioscs.com", "userPrincipalName": "buyer@trioscs.com"}
    mock_gc_instance = _make_gc(me=me)

    with patch(
        "app.utils.graph_client.GraphClient",
        return_value=mock_gc_instance,
    ):
        from app.services.teams_notifications import send_teams_dm

        await send_teams_dm(user, "self note")

    chat_call = mock_gc_instance.post_json.await_args_list[0]
    assert chat_call.args[0] == "/chats"
    members = chat_call.args[1]["members"]
    assert len(members) == 1
    # Bound by AAD object id, NOT by email
    assert members[0]["user@odata.bind"] == f"{GRAPH_USERS}/self-guid"

    mock_gc_instance.post_json.assert_any_await(
        "/chats/chat-id-123/messages",
        {"body": {"content": "self note"}},
    )


@pytest.mark.asyncio
async def test_send_teams_dm_skips_when_me_unresolved():
    """If /me yields no id (e.g. error dict from the retry layer), no chat is
    attempted."""
    user = _make_user()
    mock_gc_instance = _make_gc(me={"error": 401, "detail": "token expired"})

    with _capture_logs("WARNING") as captured:
        with patch(
            "app.utils.graph_client.GraphClient",
            return_value=mock_gc_instance,
        ):
            from app.services.teams_notifications import send_teams_dm

            await send_teams_dm(user, "never sent")

        assert any("could not resolve sender" in m for m in captured)
    mock_gc_instance.post_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_teams_dm_refreshes_token_via_db():
    """When db is provided, uses get_valid_token for a fresh token."""
    user = _make_user(access_token=None)
    mock_db = MagicMock()
    mock_gc_instance = _make_gc(chat={"id": "chat-abc"})

    with (
        patch(
            "app.utils.graph_client.GraphClient",
            return_value=mock_gc_instance,
        ) as mock_gc_cls,
        patch(
            "app.scheduler.get_valid_token",
            new_callable=AsyncMock,
            return_value="refreshed-token",
        ),
    ):
        from app.services.teams_notifications import send_teams_dm

        await send_teams_dm(user, "dm text", db=mock_db)

    mock_gc_cls.assert_called_once_with("refreshed-token")
    assert mock_gc_instance.post_json.call_count == 2


@pytest.mark.asyncio
async def test_send_teams_dm_skips_message_when_no_chat_id():
    """If /chats returns no id, the message post is skipped."""
    user = _make_user()
    mock_gc_instance = _make_gc()
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
    with _capture_logs("WARNING") as captured:
        with patch(
            "app.utils.graph_client.GraphClient",
            side_effect=RuntimeError("graph unavailable"),
        ):
            from app.services.teams_notifications import send_teams_dm

            await send_teams_dm(user, "boom")

        assert any("failed" in m and "Chat permissions" in m for m in captured)
