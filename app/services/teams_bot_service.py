"""Teams bot helpers — retained for shared utilities.

Used by: teams_qa_service.py (imports _resolve_user)
Depends on: app.models.auth.User
"""
from loguru import logger


def _resolve_user(user_aad_id: str, db):
    """Resolve a Teams AAD user ID to an AVAIL user."""
    if not user_aad_id:
        return None
    try:
        from app.models.auth import User
        return db.query(User).filter(User.azure_ad_id == user_aad_id).first()
    except Exception:
        logger.warning("Failed to resolve Teams user %s", user_aad_id, exc_info=True)
        return None
