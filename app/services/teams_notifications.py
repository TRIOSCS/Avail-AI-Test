"""Shared Teams notification helpers — channel posts and direct messages.

Provides reusable Teams integration functions so buy plan services (V1 and V3)
and any future notification code can post to Teams without duplicating the
webhook / Graph API chat logic.

Called by: buyplan_workflow/*, buyplan_notifications.py
Depends on: app.http_client, app.services.credential_service, app.utils.graph_client
"""

from loguru import logger

from app.http_client import http
from app.services.credential_service import get_credential_cached


async def post_teams_channel(message: str) -> None:
    """Post a message to the configured Teams channel via webhook.

    Uses an Adaptive Card wrapper so the message renders with markdown formatting in
    Teams. Silently skips if no webhook URL is configured.
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
            logger.warning("Teams webhook returned {}: {}", resp.status_code, resp.text[:200])
    except Exception as e:
        logger.error("Teams channel post failed: {}", e)


async def post_teams_channel_card(card: dict, webhook_url: str | None = None) -> None:
    """Post a FULL Adaptive Card to the configured Teams channel via webhook.

    Sibling of :func:`post_teams_channel`, but the caller supplies the entire Adaptive
    Card ``content`` dict (so it can carry a FactSet, an ``Action.OpenUrl`` button, colored
    TextBlocks, etc.) rather than a single markdown string. The card is wrapped verbatim in
    the same message envelope. Silently skips if no webhook URL is configured.

    ``webhook_url`` lets a caller target a specific channel webhook (e.g. the prepayment
    accounting/AP channel) instead of the shared ``TEAMS_WEBHOOK_URL`` credential. When
    omitted it falls back to that global credential, so every existing caller is unaffected.
    """
    webhook_url = webhook_url or get_credential_cached("teams_notifications", "TEAMS_WEBHOOK_URL")
    if not webhook_url:
        logger.debug("Teams webhook not configured — skipping channel card post")
        return
    try:
        resp = await http.post(
            webhook_url,
            json={
                "type": "message",
                "attachments": [
                    {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": card,
                    }
                ],
            },
            timeout=15,
        )
        if resp.status_code not in (200, 202):
            logger.warning("Teams webhook (card) returned {}: {}", resp.status_code, resp.text[:200])
    except Exception as e:
        logger.error("Teams channel card post failed: {}", e)


async def send_teams_dm(user, message: str, db=None) -> None:
    """Send a direct Teams message to a user via Graph API.

    Resolves the sending account (the token owner) via Graph ``/me``, then
    creates (or gets) the chat and posts the message. Graph requires a
    ``oneOnOne`` chat's members array to list EVERY participant including the
    caller, so:

    - Sender != recipient → two members: the token owner bound by AAD object
      id and the recipient bound by email.
    - Sender == recipient (the usual case here — DMs are sent with the
      recipient's own delegated token) → the Graph self-chat form: a single
      member bound by the caller's AAD object id. Chosen over skipping because
      it actually delivers — the message lands in the user's Teams self-chat
      and surfaces as a notification. (The old single-member payload bound by
      EMAIL is what Graph rejected with "Creation of 'OneOnOne' chat requires
      2 members".)

    Silently skips (DEBUG/WARNING log, never raises) if no valid token is
    available, ``/me`` cannot be resolved, or Graph rejects the chat creation
    (e.g. missing Chat.ReadWrite permissions).

    Args:
        user: User model instance (needs .email, .access_token)
        message: plain text message to send
        db: optional DB session for token refresh
    """
    if not user.access_token and not db:
        logger.debug("No token for {}, skipping Teams DM", user.email)
        return
    try:
        from app.utils.graph_client import GraphClient

        if db:
            from app.scheduler import get_valid_token

            token = await get_valid_token(user, db)
        else:
            token = user.access_token
        if not token:
            logger.debug("No valid token for {}, skipping Teams DM", user.email)
            return
        gc = GraphClient(token)
        # Resolve the sending account — chat membership must include the
        # caller, bound by AAD object id.
        me = await gc.get_json("/me")
        sender_id = me.get("id")
        if not sender_id:
            logger.warning("Teams DM to {} skipped — could not resolve sender via /me: {}", user.email, me)
            return
        sender_emails = {e.lower() for e in (me.get("mail"), me.get("userPrincipalName")) if isinstance(e, str) and e}
        graph_users = "https://graph.microsoft.com/v1.0/users"
        sender_member = {
            "@odata.type": "#microsoft.graph.aadUserConversationMember",
            "roles": ["owner"],
            "user@odata.bind": f"{graph_users}/{sender_id}",
        }
        if (user.email or "").lower() in sender_emails:
            # Self-chat: exactly one member — the caller, bound by AAD id.
            members = [sender_member]
        else:
            members = [
                sender_member,
                {
                    "@odata.type": "#microsoft.graph.aadUserConversationMember",
                    "roles": ["owner"],
                    "user@odata.bind": f"{graph_users}/{user.email}",
                },
            ]
        # Create or get the 1:1 chat (Graph returns the existing chat if one exists)
        chat = await gc.post_json("/chats", {"chatType": "oneOnOne", "members": members})
        chat_id = chat.get("id")
        if chat_id:
            await gc.post_json(f"/chats/{chat_id}/messages", {"body": {"content": message}})
            logger.info("Teams DM sent to {}", user.email)
    except Exception as e:
        logger.warning("Teams DM to {} failed (may not have Chat permissions): {}", user.email, e)
