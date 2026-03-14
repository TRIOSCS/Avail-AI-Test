"""Shared Teams notification helpers — channel posts and direct messages.

Provides reusable Teams integration functions so buy plan services (V1 and V3)
and any future notification code can post to Teams without duplicating the
webhook / Graph API chat logic.

Called by: buyplan_service.py, buyplan_notifications.py
Depends on: app.http_client, app.services.credential_service, app.utils.graph_client
"""

from loguru import logger

from app.http_client import http
from app.services.credential_service import get_credential_cached


async def post_teams_channel(message: str) -> None:
    """Post a message to the configured Teams channel via webhook.

    Uses an Adaptive Card wrapper so the message renders with markdown
    formatting in Teams. Silently skips if no webhook URL is configured.
    """
    webhook_url = get_credential_cached("teams_notifications", "TEAMS_WEBHOOK_URL")
    if not webhook_url:
        logger.debug("Teams webhook not configured — skipping channel post")
        return
    try:
        resp = await http.post(
            webhook_url,
            json={
                "type": "message",
                "attachments": [
                    {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": {
                            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                            "type": "AdaptiveCard",
                            "version": "1.4",
                            "body": [{"type": "TextBlock", "text": message, "wrap": True}],
                        },
                    }
                ],
            },
            timeout=15,
        )
        if resp.status_code not in (200, 202):
            logger.warning("Teams webhook returned %d: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        logger.error("Teams channel post failed: %s", e)


async def send_teams_dm(user, message: str, db=None) -> None:
    """Send a direct Teams message to a user via Graph API.

    Creates (or gets) a 1:1 chat with the user and posts the message.
    Silently skips if no valid token is available or if Graph API
    rejects the chat creation (e.g. missing Chat.ReadWrite permissions).

    Args:
        user: User model instance (needs .email, .access_token)
        message: plain text message to send
        db: optional DB session for token refresh
    """
    if not user.access_token and not db:
        logger.debug("No token for %s, skipping Teams DM", user.email)
        return
    try:
        from app.utils.graph_client import GraphClient

        if db:
            from app.scheduler import get_valid_token

            token = await get_valid_token(user, db)
        else:
            token = user.access_token
        if not token:
            logger.debug("No valid token for %s, skipping Teams DM", user.email)
            return
        gc = GraphClient(token)
        # Create or get 1:1 chat with the user (self-chat acts as notification)
        chat = await gc.post_json(
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
        chat_id = chat.get("id")
        if chat_id:
            await gc.post_json(f"/chats/{chat_id}/messages", {"body": {"content": message}})
            logger.info("Teams DM sent to %s", user.email)
    except Exception as e:
        logger.debug("Teams DM to %s failed (may not have Chat permissions): %s", user.email, e)
