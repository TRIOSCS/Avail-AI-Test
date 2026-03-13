"""Purpose: Signed token helpers for Teams Adaptive Card actions.
Description: Creates and verifies expiring action tokens embedded in Teams
    Action.Submit payloads so plan actions cannot be forged by raw API calls.
Business Rules:
- Token must match both buy plan ID and action type.
- Token must be signed with app secret and expire after configured TTL.
- Verification returns structured reasons to support safe user-facing feedback.
Called by: app/services/teams.py, app/routers/teams_actions.py
Depends on: app/config.py, itsdangerous
"""

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ..config import settings

_TEAMS_ACTION_SALT = "teams-card-action-v1"


def _serializer() -> URLSafeTimedSerializer:
    """Build serializer bound to app secret and Teams action salt."""
    return URLSafeTimedSerializer(settings.secret_key, salt=_TEAMS_ACTION_SALT)


def create_teams_action_token(plan_id: int, action: str) -> str:
    """Create a signed token for a specific buy-plan action."""
    payload = {"plan_id": int(plan_id), "action": str(action)}
    return _serializer().dumps(payload)


def verify_teams_action_token(token: str, plan_id: int, action: str) -> tuple[bool, str]:
    """Validate token signature, expiration, and payload match.

    Returns:
        (True, "ok") when valid, otherwise (False, reason) where reason is one
        of: missing, expired, invalid, mismatched.
    """
    if not token:
        return False, "missing"

    try:
        payload = _serializer().loads(token, max_age=settings.teams_card_action_token_ttl_seconds)
    except SignatureExpired:
        return False, "expired"
    except BadSignature:
        return False, "invalid"

    if not isinstance(payload, dict):
        return False, "invalid"

    try:
        token_plan_id = int(payload.get("plan_id"))
    except (TypeError, ValueError):
        return False, "invalid"

    token_action = str(payload.get("action") or "")
    if token_plan_id != int(plan_id) or token_action != str(action):
        return False, "mismatched"

    return True, "ok"
