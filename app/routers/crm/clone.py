from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ...database import get_db
from ...dependencies import require_user
from ...models import Offer, Requirement, Requisition, User
from ...utils.normalization import (
    normalize_condition,
    normalize_mpn,
    normalize_mpn_key,
    normalize_packaging,
)

router = APIRouter()


# ── Clone Requisition ────────────────────────────────────────────────────


@router.post("/api/requisitions/{req_id}/clone")
async def clone_requisition(
    req_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    from ...dependencies import get_req_for_user

    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    new_req = Requisition(
        name=f"{req.name} (clone)",
        customer_name=req.customer_name,
        customer_site_id=req.customer_site_id,
        status="active",
        cloned_from_id=req.id,
        created_by=user.id,
    )
    db.add(new_req)
    db.flush()
    for r in req.requirements:
        cloned_mpn = normalize_mpn(r.primary_mpn) or r.primary_mpn
        # Dedup substitutes by canonical key
        seen_keys = {normalize_mpn_key(cloned_mpn)}
        deduped_subs = []
        for s in (r.substitutes or []):
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
    db.flush()
    # Map old requirement IDs → new for offer cloning
    req_map: dict[int, int] = {}
    for old_r in req.requirements:
        new_r = (
            db.query(Requirement)
            .filter(
                Requirement.requisition_id == new_req.id,
                Requirement.primary_mpn == old_r.primary_mpn,
            )
            .first()
        )
        if new_r:
            req_map[old_r.id] = new_r.id
    for o in req.offers:
        if o.status in ("active", "selected"):
            new_o = Offer(
                requisition_id=new_req.id,
                requirement_id=req_map.get(o.requirement_id),
                vendor_card_id=o.vendor_card_id,
                vendor_name=o.vendor_name,
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
    db.commit()
    return {"ok": True, "id": new_req.id, "name": new_req.name}
