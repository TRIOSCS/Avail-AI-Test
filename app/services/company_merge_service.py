"""Company merge service — reusable company merge logic.

Extracted from admin.py to be callable from both the admin API endpoint
and the background auto-dedup service. Key rule: dedup = merge data & add
sites, never erase.

Called by: admin.py (company-merge endpoint), auto_dedup_service.py
Depends on: models
"""

from datetime import timezone

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
        for v in getattr(remove, field) or []:
            if str(v) not in existing_set:
                existing.append(v)
                existing_set.add(str(v))
        setattr(keep, field, existing)

    # 2. Merge notes
    if remove.notes:
        sep = f"\n\n--- Merged from {remove.name} ---\n"
        keep.notes = (keep.notes or "") + sep + remove.notes

    # 2b. Absorb the loser's name + its alternates into keep.alternate_names so a
    #     re-import of the old name fuzzy-matches the survivor instead of recreating the
    #     duplicate (mirrors VendorCard._record_alternate_name). Dedup, and never store
    #     keep's own display name as one of its alternates. Backfill keep.normalized_name
    #     if it was never set (legacy rows pre-date migration 120).
    alts = list(keep.alternate_names or [])
    seen = set(alts)
    for candidate in [remove.name, *(remove.alternate_names or [])]:
        if candidate and candidate != keep.name and candidate not in seen:
            alts.append(candidate)
            seen.add(candidate)
    keep.alternate_names = alts

    if not keep.normalized_name:
        from ..vendor_utils import normalize_vendor_name

        keep.normalized_name = normalize_vendor_name(keep.name or "") or None

    # 3. Fill enrichment gaps
    for field in (
        "domain",
        "linkedin_url",
        "legal_name",
        "employee_size",
        "hq_city",
        "hq_state",
        "hq_country",
        "website",
        "industry",
        "phone",
        "credit_terms",
        "tax_id",
        "currency",
        "preferred_carrier",
        "account_type",
    ):
        if getattr(keep, field) is None and getattr(remove, field) is not None:
            setattr(keep, field, getattr(remove, field))

    # 4. Merge booleans
    keep.is_strategic = bool(keep.is_strategic) or bool(remove.is_strategic)

    # 5. Merge owner
    if not keep.account_owner_id and remove.account_owner_id:
        keep.account_owner_id = remove.account_owner_id

    # 6. Merge timestamps (make tz-safe for SQLite which strips tzinfo)
    def _tz_safe(dt):
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    if remove.last_activity_at:
        if not keep.last_activity_at or _tz_safe(remove.last_activity_at) > _tz_safe(keep.last_activity_at):
            keep.last_activity_at = _tz_safe(remove.last_activity_at)

    # 7. Handle sites — move or delete empty HQs
    def _norm_site_name(site):
        return (site.site_name or "").strip().upper()

    remove_sites = db.query(CustomerSite).filter(CustomerSite.company_id == remove.id).all()
    keep_site_names = {
        _norm_site_name(s) for s in db.query(CustomerSite).filter(CustomerSite.company_id == keep.id).all()
    }

    sites_deleted = 0
    sites_moved = 0
    for s in remove_sites:
        is_empty_hq = (
            _norm_site_name(s) == "HQ"
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
            if _norm_site_name(s) in keep_site_names:
                s.site_name = f"{remove.name} - {s.site_name or ''}"
            s.company_id = keep.id
            keep_site_names.add(_norm_site_name(s))
            sites_moved += 1

    # Flush site changes and expire the relationship so ORM cascade doesn't
    # try to delete sites we already moved.
    db.flush()
    db.expire(remove, ["sites"])

    # 8. Reassign company-level FKs
    from ..models.excess import ExcessList
    from ..models.intelligence import ProactiveDoNotOffer
    from ..models.purchase_history import CustomerPartHistory

    reassigned = 0
    for model, col in [
        (ActivityLog, "company_id"),
        (EnrichmentQueue, "company_id"),
        (Sighting, "source_company_id"),
        (CustomerPartHistory, "company_id"),
        (ExcessList, "company_id"),
        (ProactiveDoNotOffer, "company_id"),
    ]:
        try:
            count = (
                db.query(model)
                .filter(getattr(model, col) == remove.id)
                .update({col: keep.id}, synchronize_session="fetch")
            )
            reassigned += count
        except Exception as e:
            # Fail CLOSED: a reassignment error re-raises so the caller rolls back the whole
            # merge rather than proceeding to db.delete(remove) and orphaning / cascade-deleting
            # the un-reassigned rows (mirrors vendor_merge_service / delete_companies).
            logger.error("Company merge: FK reassignment failed on {}.{}: {}", model.__tablename__, col, e)
            raise ValueError(f"Company merge aborted — failed to reassign {model.__tablename__}.{col}: {e}") from e

    # 9. Delete removed company
    db.delete(remove)
    db.flush()

    # 10. Invalidate cache
    try:
        from ..cache.decorators import invalidate_prefix

        invalidate_prefix("company_list")
    except Exception as e:
        logger.warning("Company merge: cache invalidation failed: {}", e)

    logger.info(
        "Company merge: kept {} ({}), removed {} ({}), sites_moved={}, sites_deleted={}, reassigned={}",
        keep.id,
        keep.name,
        remove_id,
        remove.name or "?",
        sites_moved,
        sites_deleted,
        reassigned,
    )
    return {
        "ok": True,
        "kept": keep.id,
        "removed": remove_id,
        "sites_moved": sites_moved,
        "sites_deleted": sites_deleted,
        "reassigned": reassigned,
    }


def delete_companies(id_a: int, id_b: int, db: Session) -> dict:
    """Delete BOTH companies in a dedup pair (neither is worth keeping).

    Company-scoped data (sites, attachments, collaborators, customer part history,
    excess lists) is deleted along with the company — it has no meaning without it.
    Cross-domain references that merely *point at* the company (activity log, sightings,
    knowledge entries, prospect accounts) are NULLed so those records survive unlinked,
    mirroring how merge reassigns rather than cascade-deletes them. Does NOT commit —
    caller must commit.

    Returns:
        {"ok": True, "deleted": [int, int], "detached": int, "purged": int}

    Raises:
        ValueError if either company is missing or the two ids are identical.
    """
    from ..models import ActivityLog, Sighting
    from ..models.excess import ExcessList
    from ..models.knowledge import KnowledgeEntry
    from ..models.prospect_account import ProspectAccount
    from ..models.purchase_history import CustomerPartHistory

    if id_a == id_b:
        raise ValueError("Cannot delete a company pair with identical ids")
    co_a = db.get(Company, id_a)
    co_b = db.get(Company, id_b)
    if not co_a or not co_b:
        raise ValueError("One or both companies not found")

    ids = [id_a, id_b]

    # Detach soft references (nullable / SET NULL) — these records outlive the company.
    # Fail CLOSED: a detach error re-raises so the route rolls back rather than deleting
    # the companies anyway and silently orphaning/losing the dependent rows (mirrors the
    # vendor service's abort-and-surface).
    detached = 0
    for model, col in [
        (ActivityLog, "company_id"),
        (Sighting, "source_company_id"),
        (KnowledgeEntry, "company_id"),
        (ProspectAccount, "company_id"),
    ]:
        try:
            detached += (
                db.query(model).filter(getattr(model, col).in_(ids)).update({col: None}, synchronize_session="fetch")
            )
        except Exception as e:
            logger.error("Company delete-both: failed to detach {}.{}: {}", model.__tablename__, col, e)
            raise ValueError(f"Company delete aborted — failed to detach {model.__tablename__}.{col}: {e}") from e

    # Purge company-scoped rows (NOT-NULL company_id) that are meaningless without the
    # company. Sites/attachments/collaborators go via the ORM "all, delete-orphan" cascade
    # on db.delete(company); these two tables are not ORM-cascaded so purge explicitly.
    # Fail CLOSED here too — a purge error must abort, never delete the company anyway.
    purged = 0
    for model in (CustomerPartHistory, ExcessList):
        try:
            purged += db.query(model).filter(model.company_id.in_(ids)).delete(synchronize_session="fetch")
        except Exception as e:
            logger.error("Company delete-both: failed to purge {}: {}", model.__tablename__, e)
            raise ValueError(f"Company delete aborted — failed to purge {model.__tablename__}: {e}") from e

    db.delete(co_a)
    db.delete(co_b)
    db.flush()

    try:
        from ..cache.decorators import invalidate_prefix

        invalidate_prefix("company_list")
    except Exception as e:
        logger.warning("Company delete-both: cache invalidation failed: {}", e)

    logger.info("Company delete-both: removed {} + {}, detached {}, purged {}", id_a, id_b, detached, purged)
    return {"ok": True, "deleted": [id_a, id_b], "detached": detached, "purged": purged}
