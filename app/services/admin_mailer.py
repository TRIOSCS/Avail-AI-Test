"""admin_mailer.py — send external email as a logged-in admin's mailbox.

Purpose: One home for the "borrow an admin with a live delegated Graph token"
         send policy (there is NO app-token sendMail path). Who qualifies as
         sender, the token refresh, the skip semantics, and
         saveToSentItems="false" live here instead of being copied per
         notification module.

Called by: app.services.prepayment_notifications._send_group_email,
           app.services.buyplan_notifications.notify_stock_sale_approved
Depends on: app.utils.graph_client, app.utils.token_manager
"""

from loguru import logger
from sqlalchemy.orm import Session

from app.models.auth import User


async def send_group_email_as_admin(
    db: Session,
    to: list[str],
    subject: str,
    html: str,
    *,
    admin_emails: list[str],
    log_label: str,
) -> bool:
    """Send *html* to each address in *to* using a logged-in admin's delegated Graph
    token.

    *admin_emails* is the pool of candidate senders (callers pass
    ``settings.admin_emails``, evaluated in their own module so tests can patch
    it there); the first with a live token wins. If no admin has one, log +
    skip and return False (the caller records the honest failure). Returns
    True if at least one message was accepted. Sends are sequential and
    individually try/logged so one bad address can't suppress the rest. These
    are system notices — saveToSentItems stays "false".
    """
    from app.utils.graph_client import GraphClient, build_sendmail_payload
    from app.utils.token_manager import get_valid_token

    recipients = [a for a in to if a]
    if not recipients:
        return False

    admin_users = db.query(User).filter(User.email.in_(admin_emails)).all()
    sender = next((a for a in admin_users if a.access_token), None)
    if sender is None:
        logger.warning("{}: no admin with a live Graph token — skipping send to {}", log_label, recipients)
        return False
    token = await get_valid_token(sender, db)
    if not token:
        logger.warning("{}: admin Graph token unavailable — skipping send to {}", log_label, recipients)
        return False

    gc = GraphClient(token)
    sent_any = False
    for addr in recipients:
        try:
            await gc.post_json(
                "/me/sendMail",
                build_sendmail_payload(subject, html, addr, save_to_sent="false"),
            )
            sent_any = True
            logger.info("{} emailed to {}", log_label, addr)
        except Exception as e:  # noqa: BLE001 — best-effort fan-out: one bad address must not stop the rest
            logger.error("{} email to {} failed: {}", log_label, addr, e)
    return sent_any
