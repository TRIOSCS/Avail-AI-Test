"""Teams bot router — Outgoing Webhook message handler.

Receives messages from Teams Outgoing Webhook, validates HMAC signature,
routes to intent handler, and returns Adaptive Card responses.

Setup: Teams admin -> Outgoing Webhooks -> Add URL pointing to /api/teams-bot/message
The HMAC secret from Teams is stored in SystemConfig as 'teams_bot_hmac_secret'.

Called by: app/main.py (router registration)
Depends on: app/services/teams_bot_service.py
"""

import base64
import hashlib
import hmac

from fastapi import APIRouter, HTTPException, Request
from loguru import logger

router = APIRouter(prefix="/api/teams-bot", tags=["teams-bot"])


def _get_bot_config() -> dict:
    """Get bot config from SystemConfig."""
    try:
        from app.database import SessionLocal
        from app.services.admin_service import get_config_values

        db = SessionLocal()
        try:
            return get_config_values(db, ["teams_bot_enabled", "teams_bot_hmac_secret"])
        finally:
            db.close()
    except Exception:
        return {}


def _validate_hmac(body: bytes, auth_header: str, secret: str) -> bool:
    """Validate Teams Outgoing Webhook HMAC-SHA256 signature."""
    try:
        secret_bytes = base64.b64decode(secret)
        expected = base64.b64encode(
            hmac.new(secret_bytes, body, hashlib.sha256).digest()
        ).decode()
        return hmac.compare_digest(f"HMAC {expected}", auth_header)
    except Exception:
        return False


@router.post("/message")
async def handle_message(request: Request):
    """Receive and process a Teams Outgoing Webhook message.

    Teams sends JSON with: {type, text, from: {name, id}, ...}
    Must return within 5 seconds or Teams shows error.
    For slow queries, return a "Thinking..." card and follow up via DM.
    """
    config = _get_bot_config()
    if not config.get("teams_bot_enabled", "").lower() == "true":
        raise HTTPException(503, "Teams bot is not enabled")

    # Validate HMAC if secret is configured
    hmac_secret = config.get("teams_bot_hmac_secret", "")
    if hmac_secret:
        body = await request.body()
        auth = request.headers.get("Authorization", "")
        if not _validate_hmac(body, auth, hmac_secret):
            raise HTTPException(401, "Invalid HMAC signature")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    user_name = data.get("from", {}).get("name", "Unknown")
    user_aad_id = data.get("from", {}).get("aadObjectId", "")
    raw_text = data.get("text", "").strip()

    # Strip bot @mention from message text
    # Teams prepends "<at>BotName</at> " to the message
    import re
    text = re.sub(r"<at>.*?</at>\s*", "", raw_text).strip()

    if not text:
        return _card_response("Hi! Ask me about your pipeline, quotes, or deals. Type 'help' for commands.")

    logger.info("Teams bot query from %s: %s", user_name, text[:100])

    # Route to handler
    try:
        from app.services.teams_bot_service import handle_query

        result = await handle_query(text, user_name, user_aad_id)
        return result
    except Exception as e:
        logger.warning("Teams bot handler failed: %s", e, exc_info=True)
        return _card_response("Something went wrong processing your request. Please try again.")


@router.get("/setup")
async def bot_setup():
    """Return step-by-step setup instructions for the Teams Outgoing Webhook."""
    return {
        "steps": [
            "1. Go to Microsoft Teams admin center",
            "2. Navigate to Teams apps > Manage apps",
            "3. Select the team where you want the bot",
            "4. Click '...' > Outgoing Webhooks > Create",
            "5. Set Name: 'AVAIL Bot', Callback URL: '<your-domain>/api/teams-bot/message'",
            "6. Copy the HMAC security token shown",
            "7. In AVAIL admin, set SystemConfig keys:",
            "   - teams_bot_enabled = true",
            "   - teams_bot_hmac_secret = <paste HMAC token>",
            "8. Test by @mentioning the bot in Teams: '@AVAIL Bot help'",
        ],
        "current_config": _get_bot_config(),
    }


@router.post("/config")
async def update_bot_config(body: dict):
    """Update Teams bot configuration. Body: {enabled, hmac_secret}."""
    from app.database import SessionLocal
    from app.models.config import SystemConfig

    db = SessionLocal()
    try:
        for key, config_key in [("enabled", "teams_bot_enabled"), ("hmac_secret", "teams_bot_hmac_secret")]:
            if key in body:
                existing = db.query(SystemConfig).filter(SystemConfig.key == config_key).first()
                if existing:
                    existing.value = str(body[key])
                else:
                    db.add(SystemConfig(key=config_key, value=str(body[key])))
        db.commit()
        return {"ok": True}
    finally:
        db.close()


def _card_response(message: str) -> dict:
    """Build a simple Adaptive Card response for Teams."""
    return {
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
    }
