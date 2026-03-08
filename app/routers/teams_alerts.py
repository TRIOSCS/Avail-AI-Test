"""Teams DM alert configuration endpoints.

CRUD for per-user alert config (webhook URL, enabled flag) and a test endpoint.

Called by: app/main.py (router registration)
Depends on: app/models/teams_alert_config.py, app/services/teams_alert_service.py
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_user
from ..models.auth import User
from ..models.teams_alert_config import TeamsAlertConfig

router = APIRouter(prefix="/api/teams-alerts", tags=["teams-alerts"])


@router.get("/config")
async def get_config(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Get the current user's Teams alert config."""
    config = db.query(TeamsAlertConfig).filter(TeamsAlertConfig.user_id == user.id).first()
    if not config:
        return {"user_id": user.id, "teams_webhook_url": None, "alerts_enabled": True, "configured": False}
    return {
        "user_id": config.user_id,
        "teams_webhook_url": config.teams_webhook_url,
        "alerts_enabled": config.alerts_enabled,
        "configured": True,
    }


@router.put("/config")
async def update_config(body: dict, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Create or update the current user's alert config."""
    config = db.query(TeamsAlertConfig).filter(TeamsAlertConfig.user_id == user.id).first()
    if not config:
        config = TeamsAlertConfig(user_id=user.id)
        db.add(config)
    if "teams_webhook_url" in body:
        config.teams_webhook_url = body["teams_webhook_url"]
    if "alerts_enabled" in body:
        config.alerts_enabled = bool(body["alerts_enabled"])
    config.updated_at = datetime.now(timezone.utc)
    db.commit()
    return {"ok": True, "alerts_enabled": config.alerts_enabled}


@router.delete("/config")
async def delete_config(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Remove the current user's alert config."""
    config = db.query(TeamsAlertConfig).filter(TeamsAlertConfig.user_id == user.id).first()
    if config:
        db.delete(config)
        db.commit()
    return {"ok": True}


