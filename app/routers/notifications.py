"""Notification router — in-app notification endpoints.

GET  /api/notifications       -- list all (paginated, with unread count)
GET  /api/notifications/unread -- unread only
POST /api/notifications/{id}/read -- mark one as read
POST /api/notifications/read-all  -- mark all as read

Called by: main.py (app.include_router)
Depends on: services/notification_service.py, dependencies.py
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_user
from app.models import User
from app.services import notification_service as svc

router = APIRouter(tags=["notifications"])


@router.get("/api/notifications")
async def list_notifications(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List all notifications for the current user, paginated."""
    return svc.get_all(db=db, user_id=user.id, limit=limit, offset=offset)


@router.get("/api/notifications/unread")
async def unread_notifications(
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get unread notifications for the current user."""
    items = svc.get_unread(db=db, user_id=user.id, limit=limit)
    return {"items": items, "count": len(items)}


@router.post("/api/notifications/{notification_id}/read")
async def mark_notification_read(
    notification_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark a single notification as read."""
    if not svc.mark_read(db=db, notification_id=notification_id, user_id=user.id):
        raise HTTPException(404, "Notification not found")
    return {"ok": True}


@router.post("/api/notifications/read-all")
async def mark_all_read(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark all notifications as read for the current user."""
    count = svc.mark_all_read(db=db, user_id=user.id)
    return {"ok": True, "count": count}


@router.post("/api/notifications/{notification_id}/clicked")
async def record_clicked(
    notification_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Record that a user clicked on a notification."""
    from app.models.notification import Notification
    from app.services.notify_intelligence import record_engagement

    notif = db.get(Notification, notification_id)
    if not notif or notif.user_id != user.id:
        raise HTTPException(404, "Notification not found")
    record_engagement(
        user_id=user.id,
        event_type=notif.event_type or "unknown",
        entity_id=str(notification_id),
        action="clicked",
        db=db,
    )
    db.commit()
    return {"ok": True}


@router.post("/api/notifications/{notification_id}/dismissed")
async def record_dismissed(
    notification_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Record that a user dismissed a notification."""
    from app.models.notification import Notification
    from app.services.notify_intelligence import record_engagement

    notif = db.get(Notification, notification_id)
    if not notif or notif.user_id != user.id:
        raise HTTPException(404, "Notification not found")
    record_engagement(
        user_id=user.id,
        event_type=notif.event_type or "unknown",
        entity_id=str(notification_id),
        action="dismissed",
        db=db,
    )
    db.commit()
    return {"ok": True}
