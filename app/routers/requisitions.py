"""
requisitions.py — Requisitions, Requirements, Search & Stock Import Router

CRUD operations for requisitions and their line-item requirements.
Multi-source search triggering, sighting management, and stock list import.

Business Rules:
- Requisitions contain requirements (parent/child)
- Search queries all active connectors in parallel
- Stock import creates sightings matched to requirements by MPN
- Sighting scoring uses 6 weighted factors (see scoring.py)

Called by: main.py (router mount)
Depends on: models, search_service, file_utils, scoring, vendor_utils
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from ..schemas.requisitions import (
    RequisitionCreate,
    RequisitionUpdate,
    RequirementCreate,
    RequirementUpdate,
    RequisitionOut,
    SightingUnavailableIn,
)
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import get_req_for_user, require_buyer, require_user
from ..search_service import (
    search_requirement,
    sighting_to_dict,
    _get_material_history,
    _history_to_result,
)
from .rfq import _enrich_with_vendor_cards
from ..models import (
    Contact,
    Offer,
    Requirement,
    Requisition,
    Sighting,
    User,
    VendorResponse,
)

router = APIRouter(tags=["requisitions"])


@router.get("/api/requisitions")
async def list_requisitions(
    q: str = "",
    status: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    # Single query with subquery counts — avoids N+1 lazy loads
    from sqlalchemy import select

    req_count_sq = (
        select(sqlfunc.count(Requirement.id))
        .where(Requirement.requisition_id == Requisition.id)
        .correlate(Requisition)
        .scalar_subquery()
        .label("requirement_count")
    )
    contact_count_sq = (
        select(sqlfunc.count(Contact.id))
        .where(Contact.requisition_id == Requisition.id)
        .correlate(Requisition)
        .scalar_subquery()
        .label("contact_count")
    )
    reply_count_sq = (
        select(sqlfunc.count(VendorResponse.id))
        .where(VendorResponse.requisition_id == Requisition.id)
        .correlate(Requisition)
        .scalar_subquery()
        .label("reply_count")
    )
    latest_reply_sq = (
        select(sqlfunc.max(VendorResponse.received_at))
        .where(VendorResponse.requisition_id == Requisition.id)
        .correlate(Requisition)
        .scalar_subquery()
        .label("latest_reply_at")
    )
    # Detect unseen offers: latest offer created_at > offers_viewed_at (or viewed_at is NULL and offers exist)
    from sqlalchemy import case, and_, or_, literal

    latest_offer_sq = (
        select(sqlfunc.max(Offer.created_at))
        .where(Offer.requisition_id == Requisition.id)
        .correlate(Requisition)
        .scalar_subquery()
    )
    has_new_offers_sq = case(
        (
            and_(
                latest_offer_sq.isnot(None),
                or_(
                    Requisition.offers_viewed_at.is_(None),
                    latest_offer_sq > Requisition.offers_viewed_at,
                ),
            ),
            literal(True),
        ),
        else_=literal(False),
    ).label("has_new_offers")

    latest_offer_at_sq = latest_offer_sq.label("latest_offer_at")
    # Count requirements that have at least one sighting (for progress indicator)
    sourced_count_sq = (
        select(sqlfunc.count(sqlfunc.distinct(Requirement.id)))
        .where(
            Requirement.requisition_id == Requisition.id,
            select(sqlfunc.count(Sighting.id))
            .where(Sighting.requirement_id == Requirement.id)
            .correlate(Requirement)
            .scalar_subquery()
            > 0,
        )
        .correlate(Requisition)
        .scalar_subquery()
        .label("sourced_count")
    )
    query = db.query(
        Requisition,
        req_count_sq,
        contact_count_sq,
        reply_count_sq,
        latest_reply_sq,
        has_new_offers_sq,
        latest_offer_at_sq,
        sourced_count_sq,
    )
    if user.role == "sales":
        query = query.filter(Requisition.created_by == user.id)
    # Buyers see all requisitions

    if q.strip():
        safe_q = q.strip().replace("%", r"\%").replace("_", r"\_")
        query = query.filter(Requisition.name.ilike(f"%{safe_q}%"))
    elif status == "archive":
        query = query.filter(Requisition.status.in_(["archived", "won", "lost"]))
    else:
        query = query.filter(Requisition.status.notin_(["archived", "won", "lost"]))

    rows = query.order_by(Requisition.created_at.desc()).limit(500).all()
    # Pre-load creator names for buyers (they see all reqs)
    creator_names = {}
    if user.role == "buyer":
        creator_ids = {r.created_by for r, _, _, _, _, _, _, _ in rows if r.created_by}
        if creator_ids:
            creators = (
                db.query(User.id, User.name, User.email)
                .filter(User.id.in_(creator_ids))
                .all()
            )
            creator_names = {u.id: u.name or u.email.split("@")[0] for u in creators}
    return [
        {
            "id": r.id,
            "name": r.name,
            "status": r.status,
            "customer_site_id": r.customer_site_id,
            "customer_display": (
                f"{r.customer_site.company.name} — {r.customer_site.site_name}"
                if r.customer_site and r.customer_site.company
                else r.customer_name or ""
            ),
            "requirement_count": req_cnt,
            "contact_count": con_cnt,
            "reply_count": reply_cnt or 0,
            "latest_reply_at": latest_reply.isoformat() if latest_reply else None,
            "has_new_offers": bool(has_new),
            "latest_offer_at": latest_offer.isoformat() if latest_offer else None,
            "created_by_name": creator_names.get(r.created_by, "")
            if user.role == "buyer"
            else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "last_searched_at": r.last_searched_at.isoformat()
            if r.last_searched_at
            else None,
            "sourced_count": sourced_cnt or 0,
            "cloned_from_id": r.cloned_from_id,
        }
        for r, req_cnt, con_cnt, reply_cnt, latest_reply, has_new, latest_offer, sourced_cnt in rows
    ]


@router.post("/api/requisitions", response_model=RequisitionOut)
async def create_requisition(
    body: RequisitionCreate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    req = Requisition(
        name=body.name,
        customer_site_id=body.customer_site_id,
        customer_name=body.customer_name,
        created_by=user.id,
        status="draft",
    )
    db.add(req)
    db.commit()
    return {"id": req.id, "name": req.name}


@router.put("/api/requisitions/{req_id}/archive")
async def toggle_archive(
    req_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404)
    if req.status in ("archived", "won", "lost"):
        req.status = "active"
    else:
        req.status = "archived"
    db.commit()
    return {"ok": True, "status": req.status}


@router.put("/api/requisitions/{req_id}")
async def update_requisition(
    req_id: int,
    body: RequisitionUpdate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404)
    if body.name is not None:
        req.name = body.name.strip() or req.name
    if body.customer_site_id is not None:
        req.customer_site_id = body.customer_site_id
    db.commit()
    return {"ok": True, "name": req.name}


# ── Requirements ─────────────────────────────────────────────────────────
@router.get("/api/requisitions/{req_id}/requirements")
async def list_requirements(
    req_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404)

    # Single query: get vendor counts per requirement via SQL (avoids loading all sightings)
    vendor_counts = {}
    if req.requirements:
        req_ids = [r.id for r in req.requirements]
        rows = (
            db.query(
                Sighting.requirement_id,
                sqlfunc.count(
                    sqlfunc.distinct(sqlfunc.lower(sqlfunc.trim(Sighting.vendor_name)))
                ),
            )
            .filter(
                Sighting.requirement_id.in_(req_ids),
                Sighting.vendor_name.isnot(None),
            )
            .group_by(Sighting.requirement_id)
            .all()
        )
        for rid, cnt in rows:
            vendor_counts[rid] = cnt

    results = []
    for r in req.requirements:
        results.append(
            {
                "id": r.id,
                "primary_mpn": r.primary_mpn,
                "target_qty": r.target_qty,
                "target_price": float(r.target_price) if r.target_price else None,
                "substitutes": r.substitutes or [],
                "sighting_count": vendor_counts.get(r.id, 0),
                "firmware": r.firmware or "",
                "date_codes": r.date_codes or "",
                "hardware_codes": r.hardware_codes or "",
                "packaging": r.packaging or "",
                "condition": r.condition or "",
            }
        )
    return results


@router.post("/api/requisitions/{req_id}/requirements")
async def add_requirements(
    req_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404)
    raw = await request.json()
    items = raw if isinstance(raw, list) else [raw]
    created = []
    for item in items:
        try:
            parsed = RequirementCreate.model_validate(item)
        except Exception:
            continue  # skip invalid items (matches prior behaviour of skipping blank mpn)
        r = Requirement(
            requisition_id=req_id,
            primary_mpn=parsed.primary_mpn,
            target_qty=parsed.target_qty,
            target_price=parsed.target_price,
            substitutes=parsed.substitutes[:20],
        )
        db.add(r)
        created.append(r)
    db.commit()
    return [{"id": r.id, "primary_mpn": r.primary_mpn} for r in created]


@router.post("/api/requisitions/{req_id}/upload")
async def upload_requirements(
    req_id: int,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404)
    content = await file.read()
    if len(content) > 10_000_000:
        raise HTTPException(413, "File too large — 10MB maximum")
    fname = (file.filename or "").lower()
    try:
        from ..file_utils import parse_tabular_file

        rows = parse_tabular_file(content, fname)
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(400, f"Could not parse file: {str(e)[:200]}")

    created = 0
    for row in rows:
        mpn = (
            row.get("primary_mpn")
            or row.get("mpn")
            or row.get("part_number")
            or row.get("part")
            or row.get("pn")
            or row.get("oem_pn")
            or row.get("oem")
            or row.get("sku")
            or ""
        )
        if not mpn:
            continue
        qty = row.get("target_qty") or row.get("qty") or row.get("quantity") or "1"
        subs = []
        sub_str = row.get("substitutes") or row.get("subs") or ""
        if sub_str:
            subs = [
                s.strip() for s in sub_str.replace("\n", ",").split(",") if s.strip()
            ]
        for i in range(1, 21):
            s = row.get(f"sub_{i}") or row.get(f"sub{i}") or ""
            if s:
                subs.append(s)
        r = Requirement(
            requisition_id=req_id,
            primary_mpn=mpn,
            target_qty=int(qty) if qty.isdigit() else 1,
            substitutes=subs[:20],
        )
        db.add(r)
        created += 1
    db.commit()
    return {"created": created, "total_rows": len(rows)}


@router.delete("/api/requirements/{item_id}")
async def delete_requirement(
    item_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    r = db.get(Requirement, item_id)
    if not r:
        raise HTTPException(404)
    req = get_req_for_user(db, user, r.requisition_id)
    if not req:
        raise HTTPException(403)
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
        raise HTTPException(404)
    req = get_req_for_user(db, user, r.requisition_id)
    if not req:
        raise HTTPException(403)
    if data.primary_mpn is not None:
        r.primary_mpn = data.primary_mpn.strip()
    if data.target_qty is not None:
        r.target_qty = data.target_qty
    if data.substitutes is not None:
        r.substitutes = data.substitutes[:20]
    if data.target_price is not None:
        r.target_price = data.target_price
    if data.firmware is not None:
        r.firmware = data.firmware.strip()
    if data.date_codes is not None:
        r.date_codes = data.date_codes.strip()
    if data.hardware_codes is not None:
        r.hardware_codes = data.hardware_codes.strip()
    if data.packaging is not None:
        r.packaging = data.packaging.strip()
    if data.condition is not None:
        r.condition = data.condition.strip()
    db.commit()
    return {"ok": True}


# ── Search ───────────────────────────────────────────────────────────────
@router.post("/api/requisitions/{req_id}/search")
async def search_all(
    req_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404)
    results = {}
    for r in req.requirements:
        sightings = await search_requirement(r, db)
        label = r.primary_mpn or f"Req #{r.id}"
        results[str(r.id)] = {"label": label, "sightings": sightings}

    # Stamp last searched time (resets 30-day auto-archive clock)
    req.last_searched_at = datetime.now(timezone.utc)
    # Transition draft→active on first search; reactivate if archived
    if req.status in ("draft", "archived"):
        req.status = "active"
    db.commit()

    # Enrich with vendor card ratings (no contact lookup — that happens at RFQ time)
    _enrich_with_vendor_cards(results, db)
    return results


@router.post("/api/requirements/{item_id}/search")
async def search_one(
    item_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    r = db.get(Requirement, item_id)
    if not r:
        raise HTTPException(404)
    sightings = await search_requirement(r, db)
    # Wrap in same structure as search_all so enrichment works
    results = {
        str(r.id): {"label": r.primary_mpn or f"Req #{r.id}", "sightings": sightings}
    }
    _enrich_with_vendor_cards(results, db)
    return {"sightings": results[str(r.id)]["sightings"]}


# ── Saved sightings (no re-search) ──────────────────────────────────────
@router.get("/api/requisitions/{req_id}/sightings")
async def get_saved_sightings(
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return previously saved sightings from DB without triggering a new search."""
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404)
    now = datetime.now(timezone.utc)
    results: dict = {}
    for r in req.requirements:
        rows = (
            db.query(Sighting)
            .filter(Sighting.requirement_id == r.id)
            .order_by(Sighting.score.desc())
            .all()
        )
        label = r.primary_mpn or f"Req #{r.id}"
        sighting_dicts = []
        for s in rows:
            d = sighting_to_dict(s)
            d["is_historical"] = False
            d["is_material_history"] = False
            sighting_dicts.append(d)

        # Append material history (vendors seen before but not in fresh results)
        fresh_vendors = {s.vendor_name.lower() for s in rows}
        pns = [r.primary_mpn] + (r.substitutes or [])
        pns = [p for p in pns if p]
        history = _get_material_history(pns, fresh_vendors, db)
        for h in history:
            sighting_dicts.append(_history_to_result(h, now))

        if not sighting_dicts:
            continue
        sighting_dicts.sort(key=lambda x: x.get("score", 0), reverse=True)
        results[str(r.id)] = {"label": label, "sightings": sighting_dicts}
    _enrich_with_vendor_cards(results, db)
    return results


# ── Mark sighting as unavailable ─────────────────────────────────────────
@router.put("/api/sightings/{sighting_id}/unavailable")
async def mark_unavailable(
    sighting_id: int,
    data: SightingUnavailableIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    s = db.get(Sighting, sighting_id)
    if not s:
        raise HTTPException(404)
    # Verify ownership: sighting → requirement → requisition → user (sales restricted)
    req_check = (
        db.query(Requisition)
        .join(Requirement)
        .filter(
            Requirement.id == s.requirement_id,
        )
    )
    if user.role == "sales":
        req_check = req_check.filter(Requisition.created_by == user.id)
    if not req_check.first():
        raise HTTPException(403, "Not your sighting")
    s.is_unavailable = data.unavailable
    db.commit()
    return {"ok": True, "is_unavailable": s.is_unavailable}


# ── Vendor Stock List Import ─────────────────────────────────────────────
@router.post("/api/requisitions/{req_id}/import-stock")
async def import_stock_list(
    req_id: int,
    request: Request,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    """Import a vendor stock list CSV/Excel as sightings for matching requirements."""
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404)

    form = await request.form()
    file = form.get("file")
    vendor_name = form.get("vendor_name", "Manual Import")
    if not file:
        raise HTTPException(400, "No file uploaded")

    content = await file.read()
    if len(content) > 10_000_000:
        raise HTTPException(413, "File too large — 10MB maximum")
    fname = file.filename.lower()

    # Parse rows using shared utility
    from ..file_utils import parse_tabular_file

    rows = parse_tabular_file(content, fname)

    # Build a set of MPNs we're looking for in this requisition
    req_mpns = {}
    for r in req.requirements:
        all_mpns = [r.primary_mpn.strip().lower()] if r.primary_mpn else []
        for sub in r.substitutes or []:
            if sub and sub.strip():
                all_mpns.append(sub.strip().lower())
        for mpn in all_mpns:
            req_mpns[mpn] = r

    matched = 0
    imported = 0

    from ..file_utils import parse_num

    for row in rows:
        mpn = (
            row.get("mpn")
            or row.get("part_number")
            or row.get("part")
            or row.get("pn")
            or row.get("sku")
            or row.get("mfr_part")
            or ""
        ).strip()
        if not mpn:
            continue
        imported += 1

        # Check if this MPN matches any requirement
        r = req_mpns.get(mpn.lower())
        if not r:
            continue

        qty_str = (
            row.get("qty") or row.get("quantity") or row.get("qty_available") or ""
        )
        price_str = row.get("price") or row.get("unit_price") or row.get("cost") or ""
        mfg = row.get("manufacturer") or row.get("mfg") or row.get("mfr") or ""

        s = Sighting(
            requirement_id=r.id,
            vendor_name=vendor_name.strip(),
            mpn_matched=mpn,
            manufacturer=mfg,
            qty_available=int(parse_num(qty_str) or 0) if qty_str else None,
            unit_price=parse_num(price_str),
            currency=row.get("currency", "USD"),
            source_type="stock_list",
            confidence=70,
            raw_data=row,
            created_at=datetime.now(timezone.utc),
        )
        s.score = 50  # Neutral score for manual imports
        db.add(s)
        matched += 1

    db.commit()
    return {"imported_rows": imported, "matched_sightings": matched}


# ── Contacts ─────────────────────────────────────────────────────────────
