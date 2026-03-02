"""Notification service — CRUD for in-app self-heal notifications.

Provides create, list unread, mark-read, and mark-all-read operations
for the self-heal pipeline notification system.

Called by: routers/notifications.py, services/diagnosis_service.py
Depends on: models/notification.py, models/auth.py
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.models.notification import Notification


def create_notification(
    db: Session,
    user_id: int,
    event_type: str,
    title: str,
    body: str | None = None,
    ticket_id: int | None = None,
) -> Notification:
    """Create a new notification for a user."""
    notif = Notification(
        user_id=user_id,
        ticket_id=ticket_id,
        event_type=event_type,
        title=title,
        body=body,
        is_read=False,
        created_at=datetime.now(timezone.utc),
    )
    db.add(notif)
    db.commit()
    db.refresh(notif)
    logger.info("Notification created: user={}, event={}", user_id, event_type)
    return notif


def get_unread(db: Session, user_id: int, limit: int = 50) -> list[dict]:
    """Get unread notifications for a user, newest first."""
    notifs = (
        db.query(Notification)
        .filter(Notification.user_id == user_id, Notification.is_read.is_(False))
        .order_by(Notification.created_at.desc())
        .limit(limit)
        .all()
    )
    return [_to_dict(n) for n in notifs]


def get_all(db: Session, user_id: int, limit: int = 100, offset: int = 0) -> dict:
    """Get all notifications for a user, paginated."""
    query = db.query(Notification).filter(Notification.user_id == user_id)
    total = query.count()
    notifs = (
        query.order_by(Notification.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {
        "items": [_to_dict(n) for n in notifs],
        "total": total,
        "unread_count": (
            db.query(Notification)
            .filter(Notification.user_id == user_id, Notification.is_read.is_(False))
            .count()
        ),
    }


def mark_read(db: Session, notification_id: int, user_id: int) -> bool:
    """Mark a single notification as read. Returns True if found and updated."""
    notif = (
        db.query(Notification)
        .filter(Notification.id == notification_id, Notification.user_id == user_id)
        .first()
    )
    if not notif:
        return False
    notif.is_read = True
    db.commit()
    return True


def mark_all_read(db: Session, user_id: int) -> int:
    """Mark all unread notifications as read. Returns count updated."""
    count = (
        db.query(Notification)
        .filter(Notification.user_id == user_id, Notification.is_read.is_(False))
        .update({"is_read": True})
    )
    db.commit()
    logger.info("Marked {} notifications read for user {}", count, user_id)
    return count


def _to_dict(notif: Notification) -> dict:
    """Convert a Notification to a JSON-safe dict."""
    return {
        "id": notif.id,
        "user_id": notif.user_id,
        "ticket_id": notif.ticket_id,
        "event_type": notif.event_type,
        "title": notif.title,
        "body": notif.body,
        "is_read": notif.is_read,
        "created_at": notif.created_at.isoformat() if notif.created_at else None,
    }
