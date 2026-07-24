"""in_app_notifications.py — shared in-app Notification write seam.

Purpose: The one place that writes in-app Notification rows. Originally grew
         inside the approvals engine (services/approvals/notifications.py);
         lifted here when nightly-test alerting needed it too — a management
         command importing approvals internals would have been a layering smell.
         This restores the shared notify seam the removed notification_service
         used to provide.

Called by: app/services/approvals/notifications.py (re-exports for back-compat,
           which keeps app/routers/htmx/buy_plans.py and
           app/jobs/approval_outbox.py working unchanged),
           app/management/notify_nightly_status.py
Depends on: app.models.notification
"""

from datetime import UTC, datetime

from sqlalchemy.orm import Session


def write_in_app(
    db: Session,
    user_id: int,
    event_type: str,
    title: str,
    body: str | None = None,
) -> None:
    """Write one in-app Notification row (the caller commits)."""
    from app.models.notification import Notification

    notif = Notification(
        user_id=user_id,
        event_type=event_type,
        title=title,
        body=body,
        is_read=False,
        created_at=datetime.now(UTC),
    )
    db.add(notif)
