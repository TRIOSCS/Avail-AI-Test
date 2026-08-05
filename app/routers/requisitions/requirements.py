"""Requirement line-item edit endpoints.

Business Rules:
- Requirements are line-items within a requisition (parent/child)
- Search runs only on explicit user action (see v2 partial routes in htmx_views.py)

Called by: requisitions.__init__ (sub-router)
Depends on: models, schemas, search_service, normalization utils
"""

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from sqlalchemy.orm import Session

from ...constants import ActivityType
from ...database import get_db
from ...dependencies import get_req_for_user, require_user
from ...models import ChangeLog, Requirement, User
from ...schemas.requisitions import RequirementUpdate
from ...search_service import resolve_material_card
from ...services.activity_service import log_activity
from ...services.requirement_service import req_packaging
from ...utils.normalization import (
    normalize_condition,
    normalize_mpn,
    normalize_mpn_key,
)

router = APIRouter(tags=["requisitions"])


def _dedupe_substitutes(subs: list[str], primary_mpn: str) -> list[dict]:
    """Deduplicate and normalize substitutes, store as dicts with mpn + manufacturer.

    Args:
        subs: List of raw substitute MPN strings
        primary_mpn: Primary MPN to exclude from duplicates

    Returns:
        List of dicts with {"mpn": normalized_mpn, "manufacturer": ""}
    """
    seen_keys = {normalize_mpn_key(primary_mpn)}
    deduped = []

    for s in subs:
        ns = normalize_mpn(s) or s.strip()
        key = normalize_mpn_key(ns)
        if key and key not in seen_keys:
            seen_keys.add(key)
            deduped.append({"mpn": ns, "manufacturer": ""})

    return deduped


def _substitute_keys(requirement: Requirement) -> list[str]:
    """Return normalized MPN keys for a requirement's substitutes.

    Handles both the canonical dict form ({"mpn": ..., "manufacturer": ...})
    and the legacy plain-string form.
    """
    keys = []
    for sub in requirement.substitutes or []:
        # Robust to legacy strings, dict form (incl. {"mpn": None}), and malformed
        # non-str/non-dict entries — mirrors parse_substitute_mpns.
        if isinstance(sub, str):
            sub_str = sub.strip()
        elif isinstance(sub, dict):
            sub_str = str(sub.get("mpn") or "").strip()
        else:
            continue
        if sub_str:
            sub_key = normalize_mpn_key(sub_str)
            if sub_key:
                keys.append(sub_key)
    return keys


@router.delete("/api/requirements/{item_id}")
async def delete_requirement(item_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    r = db.get(Requirement, item_id)
    if not r:
        raise HTTPException(404, "Requirement not found")
    req = get_req_for_user(db, user, r.requisition_id)
    if not req:
        raise HTTPException(403, "Not authorized for this requisition")
    db.delete(r)
    db.commit()
    return {"ok": True}


@router.put("/api/requirements/{item_id}")
async def update_requirement(
    item_id: int,
    data: RequirementUpdate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    r = db.get(Requirement, item_id)
    if not r:
        raise HTTPException(404, "Requirement not found")
    req = get_req_for_user(db, user, r.requisition_id)
    if not req:
        raise HTTPException(403, "Not authorized for this requisition")
    _req_track_fields = [
        "primary_mpn",
        "manufacturer",
        "target_qty",
        "target_price",
        "firmware",
        "date_codes",
        "hardware_codes",
        "packaging",
        "condition",
        "notes",
        "sale_notes",
    ]
    old_vals = {f: getattr(r, f) for f in _req_track_fields}
    if data.manufacturer is not None:
        r.manufacturer = data.manufacturer.strip()
    if data.primary_mpn is not None:
        r.primary_mpn = normalize_mpn(data.primary_mpn) or data.primary_mpn.strip()
        r.normalized_mpn = normalize_mpn_key(data.primary_mpn)
        try:
            nested = db.begin_nested()
            mat_card = resolve_material_card(data.primary_mpn, db, r.manufacturer or "")
            r.material_card_id = mat_card.id if mat_card else None
            nested.commit()
        except Exception:
            nested.rollback()
            r.material_card_id = None
            logger.warning("resolve_material_card failed for {}", data.primary_mpn, exc_info=True)
    if data.target_qty is not None:
        r.target_qty = data.target_qty
    if data.substitutes is not None:
        deduped = _dedupe_substitutes(data.substitutes, r.primary_mpn)
        r.substitutes = deduped[:20]
    if data.target_price is not None:
        r.target_price = data.target_price
    if data.firmware is not None:
        r.firmware = data.firmware.strip()
    if data.date_codes is not None:
        r.date_codes = data.date_codes.strip()
    if data.hardware_codes is not None:
        r.hardware_codes = data.hardware_codes.strip()
    # NULL-on-unmapped, matching requirement_service: a raw fallback would trip
    # chk_req_condition / chk_req_packaging on fresh migration-built DBs.
    if data.packaging is not None:
        r.packaging = req_packaging(data.packaging)
    if data.condition is not None:
        r.condition = normalize_condition(data.condition)
    if data.notes is not None:
        r.notes = data.notes.strip()
    if data.sale_notes is not None:
        r.sale_notes = data.sale_notes.strip()
    if data.description is not None:
        r.description = data.description.strip()
    if data.package_type is not None:
        r.package_type = data.package_type.strip()
    if data.revision is not None:
        r.revision = data.revision.strip()
    if data.customer_pn is not None:
        r.customer_pn = data.customer_pn.strip()
    if data.brand is not None:
        r.brand = data.brand.strip()
    if data.need_by_date is not None:
        r.need_by_date = data.need_by_date
    new_vals = {f: getattr(r, f) for f in _req_track_fields}
    for f in _req_track_fields:
        old_v = str(old_vals.get(f) or "")
        new_v = str(new_vals.get(f) or "")
        if old_v != new_v:
            db.add(
                ChangeLog(
                    entity_type="requirement",
                    entity_id=item_id,
                    user_id=user.id,
                    field_name=f,
                    old_value=old_v,
                    new_value=new_v,
                )
            )
    if str(new_vals.get("sale_notes") or "") != str(old_vals.get("sale_notes") or ""):
        log_activity(
            db,
            activity_type=ActivityType.SALES_NOTE,
            requisition_id=req.id,
            requirement_id=r.id,
            user_id=user.id,
            description="Sales note updated",
            details={"requirement_id": r.id},
        )
    db.commit()
    return {"ok": True}
