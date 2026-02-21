"""Graph webhook service — subscribe to mail events, process notifications.

Microsoft Graph sends push notifications when emails arrive or are sent.
We validate, fetch the message details, and auto-log activities.

Usage:
    # Create subscription for a user
    await create_mail_subscription(user, db)

    # Handle incoming webhook POST (called from the FastAPI endpoint)
    await handle_notification(payload, db)

    # Renew expiring subscriptions (called from scheduler)
    await renew_expiring_subscriptions(db)
"""

import hmac
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.models import GraphSubscription, User

log = logging.getLogger("avail.webhook")

# Graph webhook subscriptions for mail expire after max 3 days (4230 min)
SUBSCRIPTION_LIFETIME_HOURS = 70  # ~3 days, renew before expiry
RENEW_BUFFER_HOURS = 6  # renew when less than 6h remaining

# Replay protection: reject duplicate notifications within this window
REPLAY_WINDOW_SECONDS = 300  # 5 minutes
_seen_notifications: dict[str, float] = {}  # key -> timestamp


# ═══════════════════════════════════════════════════════════════════════
#  SUBSCRIPTION MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════


async def create_mail_subscription(user: User, db: Session) -> GraphSubscription | None:
    """Create a Graph webhook subscription for a user's mailbox.

    Subscribes to /me/messages with changeType=created so we get notified
    on both sent and received emails.
    """
    from app.scheduler import get_valid_token
    from app.utils.graph_client import GraphClient

    token = await get_valid_token(user, db)
    if not token:
        log.warning(f"No valid token for {user.email}, skipping subscription")
        return None

    # Check for existing active subscription
    existing = (
        db.query(GraphSubscription)
        .filter(
            GraphSubscription.user_id == user.id,
            GraphSubscription.expiration_dt > datetime.now(timezone.utc),
        )
        .first()
    )
    if existing:
        log.debug(
            f"Active subscription exists for {user.email}: {existing.subscription_id}"
        )
        return existing

    client_state = secrets.token_hex(16)
    expiration = datetime.now(timezone.utc) + timedelta(
        hours=SUBSCRIPTION_LIFETIME_HOURS
    )

    notification_url = f"{settings.app_url}/api/webhooks/graph"

    payload = {
        "changeType": "created",
        "notificationUrl": notification_url,
        "resource": "/me/messages",
        "expirationDateTime": expiration.strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
        "clientState": client_state,
    }

    gc = GraphClient(token)
    try:
        result = await gc.post_json("/subscriptions", payload)
    except Exception as e:
        log.error(f"Failed to create subscription for {user.email}: {e}")
        return None

    sub_id = result.get("id")
    if not sub_id:
        log.error(f"No subscription ID in response for {user.email}: {result}")
        return None

    record = GraphSubscription(
        user_id=user.id,
        subscription_id=sub_id,
        resource="/me/messages",
        change_type="created",
        expiration_dt=expiration,
        client_state=client_state,
    )
    db.add(record)
    db.commit()

    log.info(
        f"Created Graph subscription {sub_id} for {user.email}, expires {expiration}"
    )
    return record


async def renew_subscription(sub: GraphSubscription, db: Session) -> bool:
    """Renew a Graph subscription before it expires."""
    from app.scheduler import get_valid_token
    from app.utils.graph_client import GraphClient

    user = db.get(User, sub.user_id)
    if not user:
        return False

    token = await get_valid_token(user, db)
    if not token:
        return False

    new_expiration = datetime.now(timezone.utc) + timedelta(
        hours=SUBSCRIPTION_LIFETIME_HOURS
    )

    gc = GraphClient(token)
    try:
        await gc.post_json(
            f"/subscriptions/{sub.subscription_id}",
            {
                "expirationDateTime": new_expiration.strftime(
                    "%Y-%m-%dT%H:%M:%S.0000000Z"
                )
            },
        )
    except Exception as e:
        log.error(f"Failed to renew subscription {sub.subscription_id}: {e}")
        # Subscription may have expired — delete and let scheduler recreate
        db.delete(sub)
        db.commit()
        return False

    sub.expiration_dt = new_expiration
    db.commit()
    log.info(f"Renewed subscription {sub.subscription_id} until {new_expiration}")
    return True


async def renew_expiring_subscriptions(db: Session):
    """Renew all subscriptions expiring within the buffer window."""
    cutoff = datetime.now(timezone.utc) + timedelta(hours=RENEW_BUFFER_HOURS)
    expiring = (
        db.query(GraphSubscription)
        .filter(GraphSubscription.expiration_dt <= cutoff)
        .all()
    )

    for sub in expiring:
        await renew_subscription(sub, db)


async def ensure_all_users_subscribed(db: Session):
    """Create subscriptions for any M365-connected user that doesn't have one."""
    users = (
        db.query(User)
        .filter(
            User.m365_connected.is_(True),
            User.role.in_(["buyer", "sales", "trader"]),
        )
        .all()
    )

    for user in users:
        existing = (
            db.query(GraphSubscription)
            .filter(
                GraphSubscription.user_id == user.id,
                GraphSubscription.expiration_dt > datetime.now(timezone.utc),
            )
            .first()
        )
        if not existing:
            await create_mail_subscription(user, db)


# ═══════════════════════════════════════════════════════════════════════
#  NOTIFICATION VALIDATION
# ═══════════════════════════════════════════════════════════════════════


def _prune_replay_cache():
    """Remove expired entries from the replay-protection cache."""
    cutoff = time.monotonic() - REPLAY_WINDOW_SECONDS
    expired = [k for k, ts in _seen_notifications.items() if ts < cutoff]
    for k in expired:
        del _seen_notifications[k]


def validate_notifications(payload: dict, db: Session) -> list[dict]:
    """Validate incoming Graph webhook notifications.

    For each notification in the payload:
    - Reject unknown subscriptions
    - Timing-safe clientState comparison (prevents timing attacks)
    - Replay protection (reject duplicate sub+resource within 5min window)

    Returns a list of validated notification dicts, each enriched with
    ``_subscription`` and ``_user`` keys.
    """
    notifications = payload.get("value", [])
    if not notifications:
        return []

    _prune_replay_cache()
    now = time.monotonic()
    validated = []

    for notif in notifications:
        sub_id = notif.get("subscriptionId")
        client_state = notif.get("clientState", "")

        # Look up subscription
        sub = (
            db.query(GraphSubscription)
            .filter(GraphSubscription.subscription_id == sub_id)
            .first()
        )
        if not sub:
            log.warning(f"Unknown subscription {sub_id}, ignoring")
            continue

        # Timing-safe clientState comparison
        if sub.client_state:
            if not hmac.compare_digest(sub.client_state, client_state or ""):
                log.warning(f"Client state mismatch for {sub_id}, ignoring")
                continue

        # Replay protection
        resource = notif.get("resource", "")
        replay_key = f"{sub_id}:{resource}"
        if replay_key in _seen_notifications:
            log.warning(f"Replay detected for {replay_key}, ignoring")
            continue
        _seen_notifications[replay_key] = now

        user = db.get(User, sub.user_id)
        if not user:
            continue

        notif["_subscription"] = sub
        notif["_user"] = user
        validated.append(notif)

    return validated


# ═══════════════════════════════════════════════════════════════════════
#  NOTIFICATION HANDLER
# ═══════════════════════════════════════════════════════════════════════


async def handle_notification(payload: dict, db: Session, validated: list[dict] | None = None):
    """Process a Graph webhook notification payload.

    When *validated* is provided (from ``validate_notifications``), skip
    the per-notification validation loop and process the pre-validated
    list directly.  Falls back to inline validation for backward compat.

    Graph sends a list of notifications. For each, we fetch the message
    from Graph, log it as an activity, and trigger inbox poll for RFQ
    reply matching when inbound messages are detected.
    """
    from app.email_service import poll_inbox
    from app.scheduler import get_valid_token
    from app.services.activity_service import log_email_activity
    from app.utils.graph_client import GraphClient

    # Use pre-validated list when available; otherwise fall back to
    # inline validation for backward compatibility with existing callers.
    if validated is not None:
        items = validated
    else:
        notifications = payload.get("value", [])
        if not notifications:
            return

        items = []
        for notif in notifications:
            client_state = notif.get("clientState")
            sub_id = notif.get("subscriptionId")

            sub = (
                db.query(GraphSubscription)
                .filter(GraphSubscription.subscription_id == sub_id)
                .first()
            )
            if not sub:
                log.warning(f"Unknown subscription {sub_id}, ignoring")
                continue
            if sub.client_state and sub.client_state != client_state:
                log.warning(f"Client state mismatch for {sub_id}, ignoring")
                continue

            user = db.get(User, sub.user_id)
            if not user:
                continue

            notif["_subscription"] = sub
            notif["_user"] = user
            items.append(notif)

    if not items:
        return

    # Track users who received inbound messages so we can poll their inbox
    users_with_inbound = {}  # user_id -> (user, token)

    for notif in items:
        user = notif["_user"]

        resource = notif.get("resource", "")
        change_type = notif.get("changeType")
        if change_type != "created":
            continue

        token = await get_valid_token(user, db)
        if not token:
            continue

        gc = GraphClient(token)
        try:
            msg = await gc.get_json(
                f"/{resource}",
                params={
                    "$select": "id,subject,from,toRecipients,sentDateTime,isDraft,parentFolderId"
                },
            )
        except Exception as e:
            log.error(f"Failed to fetch message for notification: {e}")
            continue

        if msg.get("isDraft"):
            continue

        message_id = msg.get("id")
        subject = msg.get("subject", "")

        from_addr = _extract_email(msg.get("from"))
        user_email = user.email.lower()

        if from_addr and from_addr.lower() == user_email:
            continue
        else:
            from_name = _extract_name(msg.get("from"))
            if from_addr:
                log_email_activity(
                    user_id=user.id,
                    direction="received",
                    email_addr=from_addr,
                    subject=subject,
                    external_id=message_id,
                    contact_name=from_name,
                    db=db,
                )
                if user.id not in users_with_inbound:
                    users_with_inbound[user.id] = (user, token)

    db.commit()

    # Trigger inbox poll for users who received inbound messages
    # This matches vendor replies to outbound RFQ contacts in near-real-time
    for user_id, (user, token) in users_with_inbound.items():
        try:
            new_responses = await poll_inbox(
                token=token,
                db=db,
                scanned_by_user_id=user.id,
            )
            if new_responses:
                log.info(
                    f"Webhook-triggered poll [{user.email}]: {len(new_responses)} new response(s)"
                )
        except Exception as e:
            log.error(f"Webhook-triggered poll failed for {user.email}: {e}")


# ═══════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════


def _extract_email(recipient: dict | None) -> str | None:
    """Extract email from Graph recipient structure."""
    if not recipient:
        return None
    addr = recipient.get("emailAddress", {})
    return addr.get("address")


def _extract_name(recipient: dict | None) -> str | None:
    """Extract display name from Graph recipient structure."""
    if not recipient:
        return None
    addr = recipient.get("emailAddress", {})
    return addr.get("name")
