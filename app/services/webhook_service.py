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
import logging
import secrets
from datetime import datetime, timezone, timedelta

from sqlalchemy.orm import Session

from app.models import GraphSubscription, User
from app.config import settings

log = logging.getLogger("avail.webhook")

# Graph webhook subscriptions for mail expire after max 3 days (4230 min)
SUBSCRIPTION_LIFETIME_HOURS = 70  # ~3 days, renew before expiry
RENEW_BUFFER_HOURS = 6            # renew when less than 6h remaining


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
    existing = db.query(GraphSubscription).filter(
        GraphSubscription.user_id == user.id,
        GraphSubscription.expiration_dt > datetime.now(timezone.utc)
    ).first()
    if existing:
        log.debug(f"Active subscription exists for {user.email}: {existing.subscription_id}")
        return existing

    client_state = secrets.token_hex(16)
    expiration = datetime.now(timezone.utc) + timedelta(hours=SUBSCRIPTION_LIFETIME_HOURS)

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

    log.info(f"Created Graph subscription {sub_id} for {user.email}, expires {expiration}")
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

    new_expiration = datetime.now(timezone.utc) + timedelta(hours=SUBSCRIPTION_LIFETIME_HOURS)

    gc = GraphClient(token)
    try:
        await gc.post_json(
            f"/subscriptions/{sub.subscription_id}",
            {"expirationDateTime": new_expiration.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")}
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
    expiring = db.query(GraphSubscription).filter(
        GraphSubscription.expiration_dt <= cutoff
    ).all()

    for sub in expiring:
        await renew_subscription(sub, db)


async def ensure_all_users_subscribed(db: Session):
    """Create subscriptions for any M365-connected user that doesn't have one."""
    users = db.query(User).filter(
        User.m365_connected.is_(True),
        User.role.in_(["buyer", "sales"]),
    ).all()

    for user in users:
        existing = db.query(GraphSubscription).filter(
            GraphSubscription.user_id == user.id,
            GraphSubscription.expiration_dt > datetime.now(timezone.utc)
        ).first()
        if not existing:
            await create_mail_subscription(user, db)


# ═══════════════════════════════════════════════════════════════════════
#  NOTIFICATION HANDLER
# ═══════════════════════════════════════════════════════════════════════

async def handle_notification(payload: dict, db: Session):
    """Process a Graph webhook notification payload.

    Graph sends a list of notifications. For each, we fetch the message
    from Graph and log it as an activity.
    """
    from app.scheduler import get_valid_token
    from app.utils.graph_client import GraphClient
    from app.services.activity_service import log_email_activity

    notifications = payload.get("value", [])
    if not notifications:
        return

    for notif in notifications:
        # Validate client_state
        client_state = notif.get("clientState")
        sub_id = notif.get("subscriptionId")

        sub = db.query(GraphSubscription).filter(
            GraphSubscription.subscription_id == sub_id
        ).first()
        if not sub:
            log.warning(f"Unknown subscription {sub_id}, ignoring")
            continue
        if sub.client_state and sub.client_state != client_state:
            log.warning(f"Client state mismatch for {sub_id}, ignoring")
            continue

        user = db.get(User, sub.user_id)
        if not user:
            continue

        resource = notif.get("resource", "")
        # resource looks like: "Users('abc')/Messages('msgid')"
        # We need to fetch the full message to get sender/recipients/subject
        change_type = notif.get("changeType")
        if change_type != "created":
            continue

        token = await get_valid_token(user, db)
        if not token:
            continue

        gc = GraphClient(token)
        try:
            # Fetch the message details
            msg = await gc.get_json(f"/{resource}", params={
                "$select": "id,subject,from,toRecipients,sentDateTime,isDraft,parentFolderId"
            })
        except Exception as e:
            log.error(f"Failed to fetch message for notification: {e}")
            continue

        if msg.get("isDraft"):
            continue

        message_id = msg.get("id")
        subject = msg.get("subject", "")

        # Determine direction: sent by user or received
        from_addr = _extract_email(msg.get("from"))
        to_addrs = [_extract_email(r) for r in msg.get("toRecipients", [])]

        user_email = user.email.lower()

        if from_addr and from_addr.lower() == user_email:
            # Outbound — log for each recipient
            for to_addr in to_addrs:
                if to_addr:
                    to_name = _extract_name(
                        next((r for r in msg.get("toRecipients", [])
                              if _extract_email(r) == to_addr), None)
                    )
                    log_email_activity(
                        user_id=user.id,
                        direction="sent",
                        email_addr=to_addr,
                        subject=subject,
                        external_id=f"{message_id}:to:{to_addr}",
                        contact_name=to_name,
                        db=db,
                    )
        else:
            # Inbound — log for the sender
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

    db.commit()


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
