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


async def post_teams_channel(message: str) -> bool:
    """Post a message to the configured Teams channel via webhook.

    Uses an Adaptive Card wrapper so the message renders with markdown formatting in
    Teams. Silently skips if no webhook URL is configured.
    """
    webhook_url = get_credential_cached("teams_notifications", "TEAMS_WEBHOOK_URL")
    if not webhook_url:
        logger.debug("Teams webhook not configured — skipping channel post")
        return False
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
            return False
        return True
    except Exception as e:
        logger.error("Teams channel post failed: {}", e)
        return False


async def post_teams_channel_card(card: dict, webhook_url: str | None = None) -> bool:
    """Post a FULL Adaptive Card to the configured Teams channel via webhook.

    Sibling of :func:`post_teams_channel`, but the caller supplies the entire Adaptive
    Card ``content`` dict (so it can carry a FactSet, an ``Action.OpenUrl`` button, colored
    TextBlocks, etc.) rather than a single markdown string. The card is wrapped verbatim in
    the same message envelope. Returns True only on a confirmed 200/202 delivery;
    not-configured and every failure return False (never raises).

    ``webhook_url`` lets a caller target a specific channel webhook (e.g. the prepayment
    accounting/AP channel) instead of the shared ``TEAMS_WEBHOOK_URL`` credential. When
    omitted it falls back to that global credential, so every existing caller is unaffected.
    """
    webhook_url = webhook_url or get_credential_cached("teams_notifications", "TEAMS_WEBHOOK_URL")
    if not webhook_url:
        logger.debug("Teams webhook not configured — skipping channel card post")
        return False
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
            return False
        return True
    except Exception as e:
        logger.error("Teams channel card post failed: {}", e)
        return False


async def _find_self_chat(gc, sender_id: str) -> str | None:
    """Return the id of the caller's Teams self-chat ("Chat with self"), or None.

    The self-chat is the oneOnOne chat in ``/me/chats`` whose only member is the
    caller. Follows @odata.nextLink a few pages in case it is buried below more
    recently active chats.
    """
    url = "/me/chats?$expand=members&$top=50"
    for _ in range(4):
        page = await gc.get_json(url)
        for chat in page.get("value", []):
            members = chat.get("members") or []
            member_ids = {m.get("userId") for m in members if m.get("userId")}
            if chat.get("chatType") == "oneOnOne" and members and member_ids <= {sender_id}:
                chat_id = chat.get("id")
                return chat_id if isinstance(chat_id, str) else None
        url = page.get("@odata.nextLink")
        if not url:
            break
    return None


async def send_teams_dm(user, message: str, db=None) -> None:
    """Send a direct Teams message to a user via Graph API.

    Resolves the sending account (the token owner) via Graph ``/me``, then:

    - Sender != recipient → POST /chats with two members (token owner bound by
      AAD object id, recipient bound by email), then post the message.
    - Sender == recipient (the usual case here — DMs are sent with the
      recipient's own delegated token) → find the EXISTING Teams self-chat
      ("Chat with self") in ``/me/chats`` — the oneOnOne chat whose only
      member is the caller — and post to it. Graph cannot CREATE a
      single-member oneOnOne chat at all: v1.0 and beta both reject it with
      "Creation of 'OneOnOne' chat requires 2 members" regardless of bind
      format (verified live 2026-08-04), so creation is never attempted.

    Never raises. Skips with a DEBUG log when no valid token is available and
    a WARNING when ``/me`` cannot be resolved, the self-chat is missing
    (user must open "Chat with self" in Teams once), or Graph rejects the
    chat creation / message post — the retry layer returns
    ``{"error": status}`` dicts on 4xx, which are treated as failures here,
    never as silent success.

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
        if (user.email or "").lower() in sender_emails:
            chat_id = await _find_self_chat(gc, sender_id)
            if not chat_id:
                logger.warning(
                    "Teams DM to {} skipped — self-chat not found via /me/chats "
                    "(open 'Chat with self' in Teams once to provision it)",
                    user.email,
                )
                return
        else:
            graph_users = "https://graph.microsoft.com/v1.0/users"
            members = [
                {
                    "@odata.type": "#microsoft.graph.aadUserConversationMember",
                    "roles": ["owner"],
                    "user@odata.bind": f"{graph_users}/{sender_id}",
                },
                {
                    "@odata.type": "#microsoft.graph.aadUserConversationMember",
                    "roles": ["owner"],
                    "user@odata.bind": f"{graph_users}/{user.email}",
                },
            ]
            # Create or get the 1:1 chat (Graph returns the existing chat if one exists)
            chat = await gc.post_json("/chats", {"chatType": "oneOnOne", "members": members})
            chat_id = chat.get("id")
            if not chat_id:
                logger.warning("Teams DM to {} failed — chat creation rejected: {}", user.email, chat)
                return
        resp = await gc.post_json(f"/chats/{chat_id}/messages", {"body": {"content": message}})
        if isinstance(resp, dict) and resp.get("error"):
            logger.warning("Teams DM to {} failed — message post rejected: {}", user.email, resp)
            return
        logger.info("Teams DM sent to {}", user.email)
    except Exception as e:
        logger.warning("Teams DM to {} failed (may not have Chat permissions): {}", user.email, e)
