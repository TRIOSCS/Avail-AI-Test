from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ...constants import ActivityType, RequisitionStatus
from ...database import get_db
from ...dependencies import require_user
from ...models import Offer, Requirement, Requisition, User
from ...services.activity_service import log_activity
from ...utils.normalization import (
    normalize_condition,
    normalize_mpn,
    normalize_mpn_key,
    normalize_packaging,
)

router = APIRouter()


# ── Clone Requisition ────────────────────────────────────────────────────


@router.post("/api/requisitions/{req_id}/clone")
async def clone_requisition(req_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    from ...dependencies import get_req_for_user

    # get_req_for_user enforces role-scoped ownership (SALES/TRADER restricted to
    # requisitions they created) and raises 404 for non-owners.
    req = get_req_for_user(db, user, req_id)
    new_req = Requisition(
        name=f"{req.name} (clone)",
        customer_name=req.customer_name,
        customer_site_id=req.customer_site_id,
        status=RequisitionStatus.ACTIVE,
        cloned_from_id=req.id,
        created_by=user.id,
    )
    db.add(new_req)
    db.flush()
    cloned_pairs = []  # (old_requirement, new_requirement) for offer remapping after flush
    for r in req.requirements:
        cloned_mpn = normalize_mpn(r.primary_mpn) or r.primary_mpn
        # Dedup substitutes by canonical key
        seen_keys = {normalize_mpn_key(cloned_mpn)}
        deduped_subs = []
        for s in r.substitutes or []:
            ns = normalize_mpn(s) or s
            key = normalize_mpn_key(ns)
            if key and key not in seen_keys:
                seen_keys.add(key)
                deduped_subs.append(ns)
        new_r = Requirement(
            requisition_id=new_req.id,
            primary_mpn=cloned_mpn,
            normalized_mpn=normalize_mpn_key(cloned_mpn),
            oem_pn=r.oem_pn,
            brand=r.brand,
            sku=r.sku,
            target_qty=r.target_qty,
            target_price=r.target_price,
            substitutes=deduped_subs[:20],
            condition=normalize_condition(r.condition) or r.condition,
            packaging=normalize_packaging(r.packaging) or r.packaging,
            notes=r.notes,
        )
        db.add(new_r)
        cloned_pairs.append((r, new_r))
    db.flush()
    # Map old requirement IDs → new for offer cloning. Keyed on the new (cloned) MPN so
    # that later requirements sharing an MPN win, matching the prior re-query behavior.
    mpn_to_new_id = {new_r.primary_mpn: new_r.id for _, new_r in cloned_pairs}
    req_map = {
        old_r.id: mpn_to_new_id[old_r.primary_mpn] for old_r, _ in cloned_pairs if old_r.primary_mpn in mpn_to_new_id
    }
    for o in req.offers:
        if o.status in ("active", "selected"):
            new_o = Offer(
                requisition_id=new_req.id,
                requirement_id=req_map.get(o.requirement_id),
                vendor_card_id=o.vendor_card_id,
                vendor_name=o.vendor_name,
                vendor_name_normalized=o.vendor_name_normalized,
                mpn=o.mpn,
                manufacturer=o.manufacturer,
                qty_available=o.qty_available,
                unit_price=o.unit_price,
                lead_time=o.lead_time,
                date_code=o.date_code,
                condition=o.condition,
                packaging=o.packaging,
                moq=o.moq,
                source=o.source,
                entered_by_id=user.id,
                notes=f"Reference from REQ-{req.id:03d}",
                status="reference",
            )
            db.add(new_o)
            db.flush()
            log_activity(
                db,
                activity_type=ActivityType.OFFER_CREATED,
                requisition_id=new_o.requisition_id,
                requirement_id=new_o.requirement_id,
                user_id=user.id,
                vendor_card_id=new_o.vendor_card_id,
                description=f"Offer added: {new_o.vendor_name} — {new_o.mpn}",
                details={"offer_id": new_o.id, "source": new_o.source},
            )
    db.commit()
    return {"ok": True, "id": new_req.id, "name": new_req.name}
