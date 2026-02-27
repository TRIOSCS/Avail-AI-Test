"""Company merge service — reusable company merge logic.

Extracted from admin.py to be callable from both the admin API endpoint
and the background auto-dedup service. Key rule: dedup = merge data & add
sites, never erase.

Called by: admin.py (company-merge endpoint), auto_dedup_service.py
Depends on: models
"""

from loguru import logger
from sqlalchemy.orm import Session

from ..models import Company, CustomerSite, SiteContact


def merge_companies(keep_id: int, remove_id: int, db: Session) -> dict:
    """Merge company remove_id into keep_id.

    Moves sites, merges tags/notes/fields, reassigns FK references,
    and deletes the removed company. Does NOT commit — caller must commit.

    Returns:
        {"ok": True, "kept": int, "removed": int,
         "sites_moved": int, "sites_deleted": int, "reassigned": int}

    Raises:
        ValueError if companies not found or same id.
    """
    from ..models import ActivityLog, EnrichmentQueue, Requisition, Sighting

    keep = db.get(Company, keep_id)
    remove = db.get(Company, remove_id)
    if not keep or not remove:
        raise ValueError("One or both companies not found")
    if keep.id == remove.id:
        raise ValueError("Cannot merge a company with itself")

    # 1. Merge JSON array tags (deduplicate)
    for field in ("brand_tags", "commodity_tags"):
        existing = list(getattr(keep, field) or [])
        existing_set = set(str(v) for v in existing)
        for v in (getattr(remove, field) or []):
            if str(v) not in existing_set:
                existing.append(v)
                existing_set.add(str(v))
        setattr(keep, field, existing)

    # 2. Merge notes
    if remove.notes:
        sep = f"\n\n--- Merged from {remove.name} ---\n"
        keep.notes = (keep.notes or "") + sep + remove.notes

    # 3. Fill enrichment gaps
    for field in (
        "domain", "linkedin_url", "legal_name", "employee_size",
        "hq_city", "hq_state", "hq_country", "website", "industry",
        "phone", "credit_terms", "tax_id", "currency", "preferred_carrier", "account_type",
    ):
        if getattr(keep, field) is None and getattr(remove, field) is not None:
            setattr(keep, field, getattr(remove, field))

    # 4. Merge booleans
    keep.is_strategic = bool(keep.is_strategic) or bool(remove.is_strategic)

    # 5. Merge owner
    if not keep.account_owner_id and remove.account_owner_id:
        keep.account_owner_id = remove.account_owner_id

    # 6. Merge timestamps
    if remove.last_activity_at:
        if not keep.last_activity_at or remove.last_activity_at > keep.last_activity_at:
            keep.last_activity_at = remove.last_activity_at

    # 7. Handle sites — move or delete empty HQs
    remove_sites = db.query(CustomerSite).filter(CustomerSite.company_id == remove.id).all()
    keep_site_names = {
        s.site_name.strip().upper()
        for s in db.query(CustomerSite).filter(CustomerSite.company_id == keep.id).all()
    }

    sites_deleted = 0
    sites_moved = 0
    for s in remove_sites:
        is_empty_hq = (
            (s.site_name or "").strip().upper() == "HQ"
            and not s.contact_name
            and not s.contact_email
            and not s.address_line1
            and db.query(SiteContact).filter(SiteContact.customer_site_id == s.id).count() == 0
            and db.query(Requisition).filter(Requisition.customer_site_id == s.id).count() == 0
        )
        if is_empty_hq:
            db.delete(s)
            sites_deleted += 1
        else:
            if s.site_name.strip().upper() in keep_site_names:
                s.site_name = f"{remove.name} - {s.site_name}"
            s.company_id = keep.id
            keep_site_names.add(s.site_name.strip().upper())
            sites_moved += 1

    # Flush site changes and expire the relationship so ORM cascade doesn't
    # try to delete sites we already moved.
    db.flush()
    db.expire(remove, ["sites"])

    # 8. Reassign company-level FKs
    reassigned = 0
    for model, col in [
        (ActivityLog, "company_id"),
        (EnrichmentQueue, "company_id"),
        (Sighting, "source_company_id"),
    ]:
        try:
            count = db.query(model).filter(getattr(model, col) == remove.id).update(
                {col: keep.id}, synchronize_session="fetch"
            )
            reassigned += count
        except Exception:
            pass

    # 9. Delete removed company
    db.delete(remove)
    db.flush()

    # 10. Invalidate cache
    try:
        from ..cache.decorators import invalidate_prefix
        invalidate_prefix("company_list")
    except Exception:
        pass

    logger.info(
        "Company merge: kept %d (%s), removed %d (%s), "
        "sites_moved=%d, sites_deleted=%d, reassigned=%d",
        keep.id, keep.name, remove_id, remove.name or "?",
        sites_moved, sites_deleted, reassigned,
    )
    return {
        "ok": True,
        "kept": keep.id,
        "removed": remove_id,
        "sites_moved": sites_moved,
        "sites_deleted": sites_deleted,
        "reassigned": reassigned,
    }
