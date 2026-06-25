"""Contact merge service — reusable contact dedup/merge logic.

Mirrors company_merge_service.py: reassigns child rows, backfills scalar gaps,
appends notes, and deletes the loser. Does NOT commit — caller must commit.

Called by: htmx_views.py (contact-merge endpoint)
Depends on: models
"""

from loguru import logger
from sqlalchemy.orm import Session

from ..models.crm import CustomerSite, SiteContact


def merge_contacts(keep_id: int, remove_id: int, db: Session) -> dict:
    """Merge contact remove_id into keep_id.

    Reassigns ActivityLog.site_contact_id, SiteContactAttachment.site_contact_id,
    RequisitionTask.site_contact_id to the keeper, backfills keeper scalar gaps from
    the loser, and deletes the loser. Does NOT commit.

    Returns:
        {"ok": True, "kept": int, "removed": int, "reassigned": int}

    Raises:
        ValueError if contacts not found, same id.
    """
    from ..models.intelligence import ActivityLog
    from ..models.task import RequisitionTask

    keep = db.get(SiteContact, keep_id)
    remove = db.get(SiteContact, remove_id)

    if not keep or not remove:
        raise ValueError("One or both contacts not found")
    if keep.id == remove.id:
        raise ValueError("Cannot merge a contact with itself")

    # 1. Reassign FK references
    reassigned = 0
    for model, col in [
        (ActivityLog, "site_contact_id"),
        (RequisitionTask, "site_contact_id"),
    ]:
        try:
            count = (
                db.query(model)
                .filter(getattr(model, col) == remove.id)
                .update({col: keep.id}, synchronize_session="fetch")
            )
            reassigned += count
        except Exception as e:
            logger.warning(
                "Contact merge: failed to reassign {}.{}: {}",
                model.__tablename__,
                col,
                e,
            )

    # SiteContactAttachment has cascade="all, delete-orphan" on the relationship,
    # meaning deleting the SiteContact would cascade-delete its attachments. We want
    # to KEEP the attachments on the keeper instead. Reassign them explicitly.
    from ..models.crm import SiteContactAttachment

    att_count = (
        db.query(SiteContactAttachment)
        .filter(SiteContactAttachment.site_contact_id == remove.id)
        .update({"site_contact_id": keep.id}, synchronize_session="fetch")
    )
    reassigned += att_count

    # 2. Backfill scalar gaps on keeper from loser (fill only if keeper is NULL)
    for field in ("title", "phone", "linkedin_url", "contact_role", "wechat_id"):
        if getattr(keep, field) is None and getattr(remove, field) is not None:
            setattr(keep, field, getattr(remove, field))

    # email: keep keeper's email (unique-per-site constraint); don't overwrite
    if keep.email is None and remove.email is not None:
        keep.email = remove.email

    # 3. Merge notes
    if remove.notes:
        sep = f"\n\n--- Merged from {remove.full_name} ---\n"
        keep.notes = (keep.notes or "") + sep + remove.notes

    # 4. Boolean merge (OR semantics for is_priority; explicit states preserved)
    keep.is_priority = bool(keep.is_priority) or bool(remove.is_priority)

    # 5. Expire the loser's attachments relationship so ORM cascade doesn't
    #    delete the rows we just reassigned.
    db.flush()
    db.expire(remove, ["attachments"])

    # 6. If loser was the account-level primary contact on the Company, clear it
    #    (SET NULL — let the UI surface re-assign). CustomerSite has no primary_contact_id;
    #    primary_contact_id lives on Company.
    from ..models.crm import Company

    loser_site = db.get(CustomerSite, remove.customer_site_id)
    if loser_site:
        company = db.get(Company, loser_site.company_id)
        if company and company.primary_contact_id == remove.id:
            company.primary_contact_id = None

    # 7. Delete loser
    db.delete(remove)
    db.flush()

    logger.info(
        "Contact merge: kept {} ({}), removed {} ({}), reassigned={}",
        keep.id,
        keep.full_name,
        remove_id,
        remove.full_name or "?",
        reassigned,
    )
    return {"ok": True, "kept": keep.id, "removed": remove_id, "reassigned": reassigned}
