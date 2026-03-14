"""Requirements, search, sightings, and stock import endpoints.

Business Rules:
- Requirements are line-items within a requisition (parent/child)
- Search triggers all active connectors in parallel via asyncio.gather
- Stock import creates sightings matched to requirements by MPN
- NC/ICS browser-based searches enqueued as background tasks
- Duplicate detection warns when same MPN quoted for same customer within 30 days

Called by: requisitions.__init__ (sub-router)
Depends on: models, schemas, search_service, file_utils, normalization utils
"""

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload

from ...database import get_db
from ...dependencies import get_req_for_user, require_buyer, require_user
from ...models import (
    ChangeLog,
    Contact,
    CustomerSite,
    MaterialCard,
    Offer,
    Requirement,
    Requisition,
    Sighting,
    SourcingLead,
    User,
)
from ...rate_limit import limiter
from ...schemas.requisitions import (
    RequirementCreate,
    RequirementUpdate,
    SearchOptions,
    SightingUnavailableIn,
)
from ...schemas.sourcing_leads import LeadDetailOut, LeadFeedbackIn, LeadOut, LeadStatusUpdateIn
from ...services.sourcing_leads import (
    append_lead_feedback,
    attach_lead_metadata_to_results,
    get_requisition_leads,
    update_lead_status,
)
from ...utils.normalization import (
    normalize_condition,
    normalize_mpn,
    normalize_mpn_key,
    normalize_packaging,
    normalize_price,
    normalize_quantity,
)
from ...vendor_utils import normalize_vendor_name

router = APIRouter(tags=["requisitions"])


def _annotate_buyer_outcomes(req: Requisition, results: dict, db: Session) -> None:
    """Annotate each lead with buyer outcome for quick triage progress.

    Outcomes:
    - open: not yet qualified
    - offer_logged: buyer converted lead to an offer
    - unavailable_confirmed: buyer marked lead unavailable
    """
    req_ids = [r.id for r in req.requirements]
    if not req_ids or not results:
        return

    offer_rows = (
        db.query(
            Offer.requirement_id,
            Offer.vendor_name_normalized,
            Offer.normalized_mpn,
            Offer.mpn,
        )
        .filter(
            Offer.requirement_id.in_(req_ids),
            Offer.status != "rejected",
        )
        .all()
    )
    offer_keys: set[tuple[int, str, str]] = set()
    for row in offer_rows:
        vendor_norm = normalize_vendor_name(row.vendor_name_normalized or "")
        mpn_key = row.normalized_mpn or normalize_mpn_key(row.mpn or "")
        if vendor_norm and mpn_key:
            offer_keys.add((row.requirement_id, vendor_norm, mpn_key))

    for req_id in req_ids:
        group = results.get(str(req_id))
        if not isinstance(group, dict):
            continue
        sightings = group.get("sightings") or []
        outcome_counts = {
            "open": 0,
            "offer_logged": 0,
            "unavailable_confirmed": 0,
        }
        for sighting in sightings:
            if not isinstance(sighting, dict):
                continue
            if sighting.get("is_unavailable"):
                outcome = "unavailable_confirmed"
            else:
                vendor_norm = normalize_vendor_name(sighting.get("vendor_name") or "")
                mpn_key = normalize_mpn_key(sighting.get("mpn_matched") or "")
                if vendor_norm and mpn_key and (req_id, vendor_norm, mpn_key) in offer_keys:
                    outcome = "offer_logged"
                else:
                    outcome = "open"
            sighting["buyer_outcome"] = outcome
            outcome_counts[outcome] += 1
        group["buyer_outcomes"] = outcome_counts


def _attach_lead_data(requirements: list[Requirement], results: dict, db: Session) -> None:
    """Annotate sighting rows with persisted lead metadata and build lead cards/summary.

    Uses canonical SourcingLead records (written during search by sync_leads_for_sightings)
    as the single source of truth for lead confidence, safety, and buyer status.

    Called by: search endpoints in this router.
    Depends on: app.services.sourcing_leads.attach_lead_metadata_to_results
    """
    req_ids = [r.id for r in requirements if r.id]
    if not req_ids:
        return

    # Step 1: annotate individual sighting rows with lead_id, buyer_status, scores
    sightings_by_req: dict[int, list[dict]] = {}
    for req_item in requirements:
        group = results.get(str(req_item.id))
        if not isinstance(group, dict):
            continue
        sightings = group.get("sightings") or []
        if isinstance(sightings, list):
            sightings_by_req[req_item.id] = sightings
    if sightings_by_req:
        attach_lead_metadata_to_results(db, sightings_by_req)

    # Step 2: build lead_cards and lead_summary from persisted SourcingLead rows
    leads = (
        db.query(SourcingLead)
        .filter(SourcingLead.requirement_id.in_(req_ids))
        .order_by(SourcingLead.confidence_score.desc(), SourcingLead.updated_at.desc())
        .all()
    )
    leads_by_req: dict[int, list[SourcingLead]] = {}
    for lead in leads:
        leads_by_req.setdefault(lead.requirement_id, []).append(lead)

    for req_item in requirements:
        group = results.get(str(req_item.id))
        if not isinstance(group, dict):
            continue
        req_leads = leads_by_req.get(req_item.id, [])
        lead_cards = []
        for lead in req_leads:
            lead_cards.append({
                "lead_id": lead.id,
                "lead_public_id": lead.lead_id,
                "vendor_name": lead.vendor_name,
                "vendor_name_normalized": lead.vendor_name_normalized,
                "vendor_card_id": lead.vendor_card_id,
                "part_requested": lead.part_number_requested,
                "part_matched": lead.part_number_matched,
                "match_type": lead.match_type,
                "source_attribution": [lead.primary_source_type],
                "lead_confidence_pct": int(lead.confidence_score or 0),
                "lead_confidence_band": lead.confidence_band,
                "vendor_safety_pct": int(lead.vendor_safety_score or 0),
                "vendor_safety_band": lead.vendor_safety_band,
                "reason_summary": lead.reason_summary,
                "risk_flags": lead.risk_flags or [],
                "safety_summary": lead.vendor_safety_summary or "",
                "contact": {
                    "name": lead.contact_name,
                    "emails": [lead.contact_email] if lead.contact_email else [],
                    "phones": [lead.contact_phone] if lead.contact_phone else [],
                    "url": lead.contact_url,
                },
                "suggested_next_action": lead.suggested_next_action,
                "buyer_status": lead.buyer_status,
                "evidence_count": lead.evidence_count,
                "corroborated": lead.corroborated,
                "timestamps": {
                    "first_seen_at": lead.source_first_seen_at.isoformat() if lead.source_first_seen_at else None,
                    "last_seen_at": lead.source_last_seen_at.isoformat() if lead.source_last_seen_at else None,
                    "updated_at": lead.updated_at.isoformat() if lead.updated_at else None,
                },
            })
        group["lead_cards"] = lead_cards
        group["lead_summary"] = {
            "total_leads": len(lead_cards),
            "high_confidence": sum(
                1 for lc in lead_cards if (lc.get("lead_confidence_pct") or 0) >= 75
            ),
            "high_safety": sum(
                1 for lc in lead_cards if (lc.get("vendor_safety_pct") or 0) >= 75
            ),
        }


def _enqueue_ics_nc_batch(requirement_ids: list[int]):
    """Queue requirements for ICS and NC browser-based searches (background task)."""
    from ...database import SessionLocal
    from ...services.ics_worker.queue_manager import enqueue_for_ics_search
    from ...services.nc_worker.queue_manager import enqueue_for_nc_search

    bg_db = SessionLocal()
    try:
        for rid in requirement_ids:
            try:
                enqueue_for_nc_search(rid, bg_db)
            except Exception:
                logger.debug("NC enqueue failed for requirement %s", rid, exc_info=True)
            try:
                enqueue_for_ics_search(rid, bg_db)
            except Exception:
                logger.debug("ICS enqueue failed for requirement %s", rid, exc_info=True)
    finally:
        bg_db.close()


@router.get("/api/requisitions/{req_id}/requirements")
async def list_requirements(req_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """List requirements for a requisition with sighting counts."""
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")

    vendor_counts = {}
    offer_counts = {}
    offer_selected_counts = {}
    task_counts = {}
    if req.requirements:
        req_ids = [r.id for r in req.requirements]
        rows = (
            db.query(
                Sighting.requirement_id,
                sqlfunc.count(sqlfunc.distinct(Sighting.vendor_name_normalized)),
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

        offer_rows = (
            db.query(Offer.requirement_id, sqlfunc.count(Offer.id))
            .filter(
                Offer.requirement_id.in_(req_ids),
                Offer.status.in_(["active", "won"]),
            )
            .group_by(Offer.requirement_id)
            .all()
        )
        for rid, cnt in offer_rows:
            offer_counts[rid] = cnt

        # Count selected-for-quote offers per requirement
        sel_rows = (
            db.query(Offer.requirement_id, sqlfunc.count(Offer.id))
            .filter(
                Offer.requirement_id.in_(req_ids),
                Offer.status.in_(["active", "won"]),
                Offer.selected_for_quote.is_(True),
            )
            .group_by(Offer.requirement_id)
            .all()
        )
        for rid, cnt in sel_rows:
            offer_selected_counts[rid] = cnt

        # Task counts per requirement
        from ...models import RequisitionTask

        part_refs = [f"requirement:{rid}" for rid in req_ids]
        task_rows = (
            db.query(RequisitionTask.source_ref, sqlfunc.count(RequisitionTask.id))
            .filter(
                RequisitionTask.requisition_id == req_id,
                RequisitionTask.source_ref.in_(part_refs),
                RequisitionTask.status != "done",
            )
            .group_by(RequisitionTask.source_ref)
            .all()
        )
        for ref, cnt in task_rows:
            rid = int(ref.split(":")[1])
            task_counts[rid] = cnt

    contact_count = (db.query(sqlfunc.count(Contact.id)).filter(Contact.requisition_id == req_id).scalar()) or 0
    last_activity_row = db.query(sqlfunc.max(Contact.created_at)).filter(Contact.requisition_id == req_id).scalar()
    hours_since = None
    if last_activity_row:
        delta = datetime.now(timezone.utc) - last_activity_row.replace(tzinfo=timezone.utc)
        hours_since = delta.total_seconds() / 3600

    results = []
    for r in req.requirements:
        oc = offer_counts.get(r.id, 0)
        sc = offer_selected_counts.get(r.id, 0)
        # Determine part-level workflow step: sourcing → offers → selected → quoted → ...
        # quote_status lives on Quote rows, not on Requisition, so we check sc/oc only here.
        if sc > 0:
            step = "selected"
        elif oc > 0:
            step = "offers"
        elif vendor_counts.get(r.id, 0) > 0:
            step = "sourced"
        else:
            step = "new"

        results.append(
            {
                "id": r.id,
                "primary_mpn": r.primary_mpn,
                "target_qty": r.target_qty,
                "target_price": float(r.target_price) if r.target_price else None,
                "substitutes": r.substitutes or [],
                "sighting_count": vendor_counts.get(r.id, 0),
                "offer_count": oc,
                "selected_count": sc,
                "task_count": task_counts.get(r.id, 0),
                "step": step,
                "contact_count": contact_count,
                "hours_since_activity": round(hours_since, 1) if hours_since is not None else None,
                "brand": r.brand or "",
                "firmware": r.firmware or "",
                "date_codes": r.date_codes or "",
                "hardware_codes": r.hardware_codes or "",
                "packaging": r.packaging or "",
                "condition": r.condition or "",
                "notes": r.notes or "",
                "sale_notes": r.sale_notes or "",
                "sourcing_status": r.sourcing_status or "open",
            }
        )
    return results


@router.post("/api/requisitions/{req_id}/requirements")
async def add_requirements(
    req_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    raw = await request.json()
    items = raw if isinstance(raw, list) else [raw]
    created = []
    skipped = []
    for idx, item in enumerate(items):
        try:
            parsed = RequirementCreate.model_validate(item)
        except (ValueError, TypeError) as exc:
            skipped.append({"index": idx, "error": str(exc)})
            continue
        seen_keys = {normalize_mpn_key(parsed.primary_mpn)}
        deduped_subs = []
        for s in parsed.substitutes:
            key = normalize_mpn_key(s)
            if key and key not in seen_keys:
                seen_keys.add(key)
                deduped_subs.append(s)
        from ...search_service import resolve_material_card

        mat_card = None
        try:
            nested = db.begin_nested()
            mat_card = resolve_material_card(parsed.primary_mpn, db)
            nested.commit()
        except Exception:
            nested.rollback()
            logger.debug("resolve_material_card failed for %s", parsed.primary_mpn, exc_info=True)

        r = Requirement(
            requisition_id=req_id,
            primary_mpn=parsed.primary_mpn,
            normalized_mpn=normalize_mpn_key(parsed.primary_mpn),
            material_card_id=mat_card.id if mat_card else None,
            target_qty=parsed.target_qty,
            target_price=parsed.target_price,
            substitutes=deduped_subs[:20],
            condition=parsed.condition or "",
            date_codes=parsed.date_codes or "",
            firmware=parsed.firmware or "",
            hardware_codes=parsed.hardware_codes or "",
            packaging=parsed.packaging or "",
            notes=parsed.notes or "",
        )
        db.add(r)
        created.append(r)
    db.commit()

    # Tag propagation
    try:
        from ...services.tagging import propagate_tags_to_entity

        for r in created:
            if r.material_card_id and req.customer_site_id:
                propagate_tags_to_entity("customer_site", req.customer_site_id, r.material_card_id, 1.0, db)
                site = db.get(CustomerSite, req.customer_site_id)
                if site and site.company_id:
                    propagate_tags_to_entity("company", site.company_id, r.material_card_id, 1.0, db)
        if created:
            db.commit()
    except Exception:  # pragma: no cover
        logger.debug("Tag propagation failed for requirements", exc_info=True)

    # NC enqueue
    def _nc_enqueue_batch(requirement_ids: list[int]):
        from ...database import SessionLocal
        from ...services.nc_worker.queue_manager import enqueue_for_nc_search

        bg_db = SessionLocal()
        try:
            for rid in requirement_ids:
                try:
                    enqueue_for_nc_search(rid, bg_db)
                except Exception:
                    logger.debug("NC enqueue failed for requirement %s", rid, exc_info=True)
        finally:
            bg_db.close()

    # ICS enqueue
    def _ics_enqueue_batch(requirement_ids: list[int]):
        from ...database import SessionLocal
        from ...services.ics_worker.queue_manager import enqueue_for_ics_search

        bg_db = SessionLocal()
        try:
            for rid in requirement_ids:
                try:
                    enqueue_for_ics_search(rid, bg_db)
                except Exception:
                    logger.debug("ICS enqueue failed for requirement %s", rid, exc_info=True)
        finally:
            bg_db.close()

    if created:
        background_tasks.add_task(_nc_enqueue_batch, [r.id for r in created])
        background_tasks.add_task(_ics_enqueue_batch, [r.id for r in created])

    # Duplicate detection
    duplicates = []
    if req.customer_site_id and created:
        from datetime import timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        card_ids = [r.material_card_id for r in created if r.material_card_id]
        if card_ids:
            dup_rows = (
                db.query(Requirement.primary_mpn, Requisition.id, Requisition.name)
                .join(Requisition, Requirement.requisition_id == Requisition.id)
                .filter(
                    Requirement.material_card_id.in_(card_ids),
                    Requisition.customer_site_id == req.customer_site_id,
                    Requisition.id != req_id,
                    Requisition.created_at >= cutoff,
                    Requisition.status.notin_(["archived"]),
                )
                .all()
            )
            seen = set()
            for mpn, rid, rname in dup_rows:
                key = f"{mpn}:{rid}"
                if key not in seen:
                    seen.add(key)
                    duplicates.append({"mpn": mpn, "req_id": rid, "req_name": rname})

    result = {
        "created": [{"id": r.id, "primary_mpn": r.primary_mpn} for r in created],
        "duplicates": duplicates,
    }
    if skipped:
        result["skipped"] = skipped
    return result


@router.post("/api/requisitions/{req_id}/upload")
async def upload_requirements(
    req_id: int,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    content = await file.read()
    if len(content) > 10_000_000:
        raise HTTPException(413, "File too large — 10MB maximum")
    fname = (file.filename or "").lower()
    try:
        from ...file_utils import parse_tabular_file

        rows = parse_tabular_file(content, fname)
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(400, f"Could not parse file: {str(e)[:200]}")

    created = 0
    for row in rows:
        raw_mpn = (
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
        mpn = normalize_mpn(raw_mpn)
        if not mpn:
            continue
        qty_raw = row.get("target_qty") or row.get("qty") or row.get("quantity") or "1"
        qty = normalize_quantity(qty_raw) or 1
        subs = []
        sub_str = row.get("substitutes") or row.get("subs") or ""
        if sub_str:
            subs = [s.strip() for s in sub_str.replace("\n", ",").split(",") if s.strip()]
        for i in range(1, 21):
            s = row.get(f"sub_{i}") or row.get(f"sub{i}") or ""
            if s:
                subs.append(s)
        seen_keys = {normalize_mpn_key(mpn)}
        deduped_subs = []
        for s in subs:
            ns = normalize_mpn(s)
            if not ns:
                continue
            key = normalize_mpn_key(ns)
            if key not in seen_keys:
                seen_keys.add(key)
                deduped_subs.append(ns)

        condition = normalize_condition(row.get("condition") or row.get("cond") or "")
        packaging = normalize_packaging(row.get("packaging") or row.get("package") or row.get("pkg") or "")
        date_codes = (row.get("date_codes") or row.get("date_code") or row.get("dc") or "").strip() or None
        manufacturer = (row.get("manufacturer") or row.get("brand") or row.get("mfr") or "").strip() or None
        notes = (row.get("notes") or row.get("note") or "").strip() or None
        target_price_raw = row.get("target_price") or row.get("price") or ""
        target_price = normalize_price(target_price_raw)

        from ...search_service import resolve_material_card

        mat_card = resolve_material_card(mpn, db)

        r = Requirement(
            requisition_id=req_id,
            primary_mpn=mpn,
            normalized_mpn=normalize_mpn_key(mpn),
            material_card_id=mat_card.id if mat_card else None,
            target_qty=qty,
            target_price=target_price,
            substitutes=deduped_subs[:20],
            condition=condition,
            packaging=packaging,
            date_codes=date_codes,
            brand=manufacturer,
            notes=notes,
        )
        db.add(r)
        created += 1
    db.commit()

    # Tag propagation for uploaded requirements
    try:
        from ...services.tagging import propagate_tags_to_entity

        uploaded_reqs = (
            db.query(Requirement)
            .filter(Requirement.requisition_id == req_id)
            .order_by(Requirement.id.desc())
            .limit(created)
            .all()
        )
        for r_item in uploaded_reqs:  # pragma: no cover
            if r_item.material_card_id and req.customer_site_id:
                propagate_tags_to_entity("customer_site", req.customer_site_id, r_item.material_card_id, 1.0, db)
                site = db.get(CustomerSite, req.customer_site_id)
                if site and site.company_id:
                    propagate_tags_to_entity("company", site.company_id, r_item.material_card_id, 1.0, db)
        if uploaded_reqs:
            db.commit()
    except Exception:  # pragma: no cover
        logger.debug("Tag propagation failed for uploaded requirements", exc_info=True)

    # NC enqueue for uploaded requirements
    def _nc_enqueue_uploaded(requisition_id: int, count: int):
        from ...database import SessionLocal
        from ...services.nc_worker.queue_manager import enqueue_for_nc_search

        bg_db = SessionLocal()
        try:
            for r_item in (
                bg_db.query(Requirement)
                .filter(
                    Requirement.requisition_id == requisition_id,
                )
                .order_by(Requirement.id.desc())
                .limit(count)
                .all()
            ):
                try:  # pragma: no cover
                    enqueue_for_nc_search(r_item.id, bg_db)
                except Exception:  # pragma: no cover
                    logger.debug("NC enqueue failed for requirement %s", r_item.id, exc_info=True)
        finally:
            bg_db.close()

    if created:
        background_tasks.add_task(_nc_enqueue_uploaded, req_id, created)

    return {"created": created, "total_rows": len(rows)}


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
    if data.primary_mpn is not None:
        r.primary_mpn = normalize_mpn(data.primary_mpn) or data.primary_mpn.strip()
        r.normalized_mpn = normalize_mpn_key(data.primary_mpn)
        from ...search_service import resolve_material_card

        try:
            nested = db.begin_nested()
            mat_card = resolve_material_card(data.primary_mpn, db)
            r.material_card_id = mat_card.id if mat_card else None
            nested.commit()
        except Exception:
            nested.rollback()
            r.material_card_id = None
            logger.debug("resolve_material_card failed for %s", data.primary_mpn, exc_info=True)
    if data.target_qty is not None:
        r.target_qty = data.target_qty
    if data.substitutes is not None:
        seen_keys = {normalize_mpn_key(r.primary_mpn)}
        deduped = []
        for s in data.substitutes:
            ns = normalize_mpn(s) or s.strip()
            key = normalize_mpn_key(ns)
            if key and key not in seen_keys:
                seen_keys.add(key)
                deduped.append(ns)
        r.substitutes = deduped[:20]
    if data.target_price is not None:
        r.target_price = data.target_price
    if data.firmware is not None:
        r.firmware = data.firmware.strip()
    if data.date_codes is not None:
        r.date_codes = data.date_codes.strip()
    if data.hardware_codes is not None:
        r.hardware_codes = data.hardware_codes.strip()
    if data.packaging is not None:
        r.packaging = normalize_packaging(data.packaging) or data.packaging.strip()
    if data.condition is not None:
        r.condition = normalize_condition(data.condition) or data.condition.strip()
    if data.notes is not None:
        r.notes = data.notes.strip()
    if data.sale_notes is not None:
        r.sale_notes = data.sale_notes.strip()
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
    db.commit()
    return {"ok": True}


# ── Search ───────────────────────────────────────────────────────────────


@router.post("/api/requisitions/{req_id}/search")
@limiter.limit("20/minute")
async def search_all(
    req_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    body: SearchOptions | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from . import _enrich_with_vendor_cards, search_requirement

    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")

    requirement_ids = body.requirement_ids if body else None
    reqs_to_search = [r for r in req.requirements if not requirement_ids or r.id in requirement_ids]

    search_tasks = [search_requirement(r, db) for r in reqs_to_search]
    search_results = await asyncio.gather(*search_tasks, return_exceptions=True)

    results = {}
    merged_source_stats: dict[str, dict] = {}
    for r, search_result in zip(reqs_to_search, search_results):
        if isinstance(search_result, Exception):
            logger.error(f"Search failed for requirement {r.id}: {search_result}")
            sightings = []
            req_stats = []
        else:
            sightings = search_result["sightings"]
            req_stats = search_result["source_stats"]
        label = r.primary_mpn or f"Req #{r.id}"
        results[str(r.id)] = {"label": label, "sightings": sightings}
        for stat in req_stats:
            name = stat["source"]
            if name not in merged_source_stats:
                merged_source_stats[name] = dict(stat)
            else:
                existing = merged_source_stats[name]
                existing["results"] += stat["results"]
                existing["ms"] = max(existing["ms"], stat["ms"])
                if stat["error"] and not existing["error"]:
                    existing["error"] = stat["error"]
                    existing["status"] = stat["status"]

    req.last_searched_at = datetime.now(timezone.utc)
    if req.status in ("draft", "archived"):
        from ...services.requisition_state import transition

        try:
            transition(req, "active", user, db)
        except ValueError:
            pass  # status may already be active
    db.commit()

    req_ids = [r.id for r in reqs_to_search]
    background_tasks.add_task(_enqueue_ics_nc_batch, req_ids)

    _enrich_with_vendor_cards(results, db)
    _annotate_buyer_outcomes(req, results, db)
    _attach_lead_data(reqs_to_search, results, db)

    results["source_stats"] = list(merged_source_stats.values())
    return results


@router.post("/api/requirements/{item_id}/search")
@limiter.limit("20/minute")
async def search_one(
    item_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from . import _enrich_with_vendor_cards, search_requirement

    r = db.get(Requirement, item_id)
    if not r:
        raise HTTPException(404, "Requirement not found")
    req = get_req_for_user(db, user, r.requisition_id)
    if not req:
        raise HTTPException(403, "Access denied")
    search_result = await search_requirement(r, db)
    sightings = search_result["sightings"]
    source_stats = search_result["source_stats"]
    results = {str(r.id): {"label": r.primary_mpn or f"Req #{r.id}", "sightings": sightings}}
    _enrich_with_vendor_cards(results, db)
    _annotate_buyer_outcomes(req, results, db)
    _attach_lead_data([r], results, db)

    background_tasks.add_task(_enqueue_ics_nc_batch, [r.id])

    return {
        "sightings": results[str(r.id)]["sightings"],
        "lead_cards": results[str(r.id)].get("lead_cards", []),
        "lead_summary": results[str(r.id)].get("lead_summary", {}),
        "source_stats": source_stats,
    }


# ── Saved sightings (no re-search) ──────────────────────────────────────
@router.get("/api/requisitions/{req_id}/sightings")
async def get_saved_sightings(
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return previously saved sightings from DB without triggering a new search."""
    from . import (
        _deduplicate_sightings,
        _enrich_with_vendor_cards,
        _get_material_history,
        _history_to_result,
        sighting_to_dict,
    )

    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    now = datetime.now(timezone.utc)
    results: dict = {}
    req_ids = [r.id for r in req.requirements]
    all_sightings = (
        (
            db.query(Sighting)
            .filter(Sighting.requirement_id.in_(req_ids))
            .order_by(Sighting.created_at.desc())
            .limit(5000)
            .all()
        )
        if req_ids
        else []
    )
    sightings_by_req: dict[int, list] = {}
    for s in all_sightings:
        sightings_by_req.setdefault(s.requirement_id, []).append(s)

    req_card_map: dict[int, set[int]] = {}
    all_card_ids: set[int] = set()
    primary_card_ids: dict[int, int | None] = {}

    all_sub_keys: set[str] = set()
    req_sub_keys: dict[int, list[str]] = {}
    for r in req.requirements:
        primary_card_ids[r.id] = r.material_card_id
        sub_keys = []
        for sub in r.substitutes or []:
            sub_str = (sub if isinstance(sub, str) else "").strip()
            if sub_str:
                sub_key = normalize_mpn_key(sub_str)
                if sub_key:
                    sub_keys.append(sub_key)
                    all_sub_keys.add(sub_key)
        req_sub_keys[r.id] = sub_keys

    sub_card_lookup: dict[str, int] = {}
    if all_sub_keys:
        rows = (
            db.query(MaterialCard.id, MaterialCard.normalized_mpn)
            .filter(MaterialCard.normalized_mpn.in_(all_sub_keys))
            .all()
        )
        sub_card_lookup = {row.normalized_mpn: row.id for row in rows}

    for r in req.requirements:
        card_ids: set[int] = set()
        if r.material_card_id:
            card_ids.add(r.material_card_id)
        for sub_key in req_sub_keys.get(r.id, []):
            card_id = sub_card_lookup.get(sub_key)
            if card_id:  # pragma: no cover
                card_ids.add(card_id)
        req_card_map[r.id] = card_ids
        all_card_ids |= card_ids

    hist_by_req: dict[int, list] = {}
    if all_card_ids:
        hist_query = (
            db.query(Offer)
            .filter(
                Offer.requisition_id != req_id,
                Offer.material_card_id.in_(all_card_ids),
                Offer.status.in_(["active", "won"]),
            )
            .options(joinedload(Offer.entered_by))
            .order_by(Offer.created_at.desc())
            .limit(100)
            .all()
        )
        for ho in hist_query:  # pragma: no cover
            for r in req.requirements:
                if ho.material_card_id in req_card_map.get(r.id, set()):
                    if r.id not in hist_by_req:
                        hist_by_req[r.id] = []
                    is_sub = ho.material_card_id != primary_card_ids.get(r.id)
                    hist_by_req[r.id].append(
                        {
                            "id": ho.id,
                            "vendor_name": ho.vendor_name,
                            "mpn": ho.mpn,
                            "manufacturer": ho.manufacturer,
                            "qty_available": ho.qty_available,
                            "unit_price": float(ho.unit_price) if ho.unit_price else None,
                            "lead_time": ho.lead_time,
                            "condition": ho.condition,
                            "source": ho.source,
                            "status": ho.status,
                            "entered_by": ho.entered_by.name if ho.entered_by else None,
                            "created_at": ho.created_at.isoformat() if ho.created_at else None,
                            "from_requisition_id": ho.requisition_id,
                            "is_substitute": is_sub,
                        }
                    )
                    break

    for r in req.requirements:
        rows = sightings_by_req.get(r.id, [])
        label = r.primary_mpn or f"Req #{r.id}"
        sighting_dicts = []
        for s in rows:
            d = sighting_to_dict(s)
            d["is_historical"] = False
            d["is_material_history"] = False
            sighting_dicts.append(d)

        fresh_vendors = {s.vendor_name.lower() for s in rows}
        card_ids = list(req_card_map.get(r.id, set()))
        history = _get_material_history(card_ids, fresh_vendors, db)
        for h in history:
            sighting_dicts.append(_history_to_result(h, now))

        hist_offers = hist_by_req.get(r.id, [])
        sighting_dicts = _deduplicate_sightings(sighting_dicts)
        if not sighting_dicts and not hist_offers:
            continue
        sighting_dicts.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        results[str(r.id)] = {
            "label": label,
            "sightings": sighting_dicts,
            "historical_offers": hist_offers,
        }
    _enrich_with_vendor_cards(results, db)
    _annotate_buyer_outcomes(req, results, db)
    _attach_lead_data(req.requirements, results, db)
    return results


@router.get("/api/requisitions/{req_id}/leads", response_model=list[LeadOut])
async def list_requisition_leads(
    req_id: int,
    statuses: str | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List canonical sourcing leads for buyer follow-up queue."""
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    status_list = [s.strip().lower() for s in (statuses or "").split(",") if s.strip()] if statuses else None
    return get_requisition_leads(db, req_id, status_list)


@router.get("/api/leads/{lead_id}", response_model=LeadDetailOut)
async def get_lead_detail(
    lead_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get full lead detail with evidence and feedback history."""
    lead = (
        db.query(SourcingLead)
        .options(
            joinedload(SourcingLead.evidence),
            joinedload(SourcingLead.feedback_events),
        )
        .filter(SourcingLead.id == lead_id)
        .first()
    )
    if not lead:
        raise HTTPException(404, "Lead not found")
    if not get_req_for_user(db, user, lead.requisition_id):
        raise HTTPException(403, "Not authorized for this lead")
    return lead


@router.patch("/api/leads/{lead_id}/status")
async def patch_lead_status(
    lead_id: int,
    payload: LeadStatusUpdateIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update lead workflow status and append buyer outcome event."""
    lead = db.get(SourcingLead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    if not get_req_for_user(db, user, lead.requisition_id):
        raise HTTPException(403, "Not authorized for this lead")

    try:
        updated = update_lead_status(
            db,
            lead_id,
            payload.status,
            note=payload.note,
            reason_code=payload.reason_code,
            contact_method=payload.contact_method,
            contact_attempt_count=payload.contact_attempt_count,
            actor_user_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not updated:
        raise HTTPException(404, "Lead not found")
    return {
        "ok": True,
        "lead_id": updated.id,
        "status": updated.buyer_status,
        "confidence_score": updated.confidence_score,
        "confidence_band": updated.confidence_band,
        "vendor_safety_score": updated.vendor_safety_score,
        "vendor_safety_band": updated.vendor_safety_band,
        "buyer_feedback_summary": updated.buyer_feedback_summary,
    }


@router.post("/api/leads/{lead_id}/feedback")
async def add_lead_feedback(
    lead_id: int,
    payload: LeadFeedbackIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Append buyer feedback without changing workflow state."""
    lead = db.get(SourcingLead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    if not get_req_for_user(db, user, lead.requisition_id):
        raise HTTPException(403, "Not authorized for this lead")

    updated = append_lead_feedback(
        db,
        lead.id,
        note=payload.note,
        reason_code=payload.reason_code,
        contact_method=payload.contact_method,
        contact_attempt_count=payload.contact_attempt_count,
        actor_user_id=user.id,
    )
    if not updated:
        raise HTTPException(404, "Lead not found")
    return {"ok": True, "lead_id": updated.id, "status": updated.buyer_status}


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
        raise HTTPException(404, "Sighting not found")
    req = db.query(Requisition).join(Requirement).filter(Requirement.id == s.requirement_id).first()
    if not req or not get_req_for_user(db, user, req.id):
        raise HTTPException(403, "Not authorized for this sighting")
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
        raise HTTPException(404, "Requisition not found")

    form = await request.form()
    file = form.get("file")
    vendor_name = form.get("vendor_name", "Manual Import")
    if not file:
        raise HTTPException(400, "No file uploaded")

    content = await file.read()
    if len(content) > 10_000_000:
        raise HTTPException(413, "File too large — 10MB maximum")
    fname = file.filename.lower()

    from ...file_utils import parse_tabular_file

    rows = parse_tabular_file(content, fname)

    from ...file_utils import normalize_stock_row
    from ...search_service import resolve_material_card
    from ...utils.normalization import normalize_condition as norm_cond
    from ...utils.normalization import normalize_date_code, normalize_lead_time
    from ...utils.normalization import normalize_packaging as norm_pkg

    req_mpns = {}
    for r in req.requirements:
        all_mpns = [r.primary_mpn] if r.primary_mpn else []
        for sub in r.substitutes or []:
            if sub and sub.strip():
                all_mpns.append(sub.strip())
        for m in all_mpns:
            req_mpns[normalize_mpn_key(m)] = r

    matched = 0
    imported = 0

    try:
        for row in rows:
            parsed = normalize_stock_row(row)
            if not parsed:
                continue
            mpn = parsed["mpn"]
            imported += 1

            r = req_mpns.get(normalize_mpn_key(mpn))
            if not r:
                continue

            display_mpn = normalize_mpn(mpn) or mpn

            mat_card = resolve_material_card(mpn, db)

            s = Sighting(
                requirement_id=r.id,
                material_card_id=mat_card.id if mat_card else None,
                vendor_name=vendor_name.strip(),
                vendor_name_normalized=normalize_vendor_name(vendor_name),
                mpn_matched=display_mpn,
                manufacturer=parsed.get("manufacturer"),
                qty_available=parsed.get("qty"),
                unit_price=parsed.get("price"),
                currency=parsed.get("currency", "USD"),
                condition=norm_cond(parsed.get("condition")),
                packaging=norm_pkg(parsed.get("packaging")),
                date_code=normalize_date_code(parsed.get("date_code")),
                lead_time_days=normalize_lead_time(parsed.get("lead_time")),
                source_type="stock_list",
                confidence=70,
                raw_data=row,
                created_at=datetime.now(timezone.utc),
            )
            s.score = 50  # Neutral score for manual imports
            db.add(s)
            matched += 1

        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Stock import failed for requisition %s", req_id)
        raise HTTPException(500, "Stock import failed — no data was saved")
    return {"imported_rows": imported, "matched_sightings": matched}


# ── Part-level endpoints (requirement-scoped) ─────────────────────────
# Used by the part-number expansion panel in the frontend.


@router.get("/api/requirements/{requirement_id}/sightings")
async def list_requirement_sightings(
    requirement_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return saved sightings for one requirement, enriched for part-level UI."""
    from . import (
        _deduplicate_sightings,
        _enrich_with_vendor_cards,
        _get_material_history,
        _history_to_result,
        sighting_to_dict,
    )

    req_item = db.query(Requirement).filter(Requirement.id == requirement_id).first()
    if not req_item:
        raise HTTPException(404, "Requirement not found")
    if not get_req_for_user(db, user, req_item.requisition_id):
        raise HTTPException(403, "Not authorized")

    rows = (
        db.query(Sighting)
        .filter(Sighting.requirement_id == requirement_id)
        .order_by(Sighting.created_at.desc())
        .limit(1000)
        .all()
    )

    sighting_dicts = []
    for s in rows:
        d = sighting_to_dict(s)
        d["is_historical"] = False
        d["is_material_history"] = False
        sighting_dicts.append(d)

    # Include material history for this part and substitutes so part-level
    # sourcing context mirrors the requisition-level sourcing view.
    card_ids: set[int] = set()
    if req_item.material_card_id:
        card_ids.add(req_item.material_card_id)
    sub_keys = []
    for sub in req_item.substitutes or []:
        sub_str = (sub if isinstance(sub, str) else "").strip()
        if sub_str:
            sub_key = normalize_mpn_key(sub_str)
            if sub_key:
                sub_keys.append(sub_key)
    if sub_keys:
        sub_rows = db.query(MaterialCard.id).filter(MaterialCard.normalized_mpn.in_(sub_keys)).all()
        card_ids |= {row[0] for row in sub_rows}

    fresh_vendors = {s.vendor_name.lower() for s in rows if s.vendor_name}
    for h in _get_material_history(list(card_ids), fresh_vendors, db):
        sighting_dicts.append(_history_to_result(h, datetime.now(timezone.utc)))

    sighting_dicts = _deduplicate_sightings(sighting_dicts)
    sighting_dicts.sort(key=lambda x: x.get("created_at") or "", reverse=True)

    payload = {
        str(requirement_id): {
            "label": req_item.primary_mpn or f"Req #{requirement_id}",
            "sightings": sighting_dicts,
        }
    }
    _enrich_with_vendor_cards(payload, db)
    _attach_lead_data([req_item], payload, db)
    return payload[str(requirement_id)]


@router.get("/api/requirements/{requirement_id}/offers")
async def list_requirement_offers(
    requirement_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List current + historical offers for a single requirement.

    Returns a unified list mixing current offers (on this requisition) and
    historical offers (from other requisitions for the same material card).
    Each row carries is_historical and is_substitute flags for the UI.
    """
    req_item = db.query(Requirement).filter(Requirement.id == requirement_id).first()
    if not req_item:
        raise HTTPException(404, "Requirement not found")

    def _offer_dict(o, *, is_historical=False, is_substitute=False, source_req_id=None):
        age_days = 0
        if o.created_at:
            age_days = (datetime.now(timezone.utc) - o.created_at.replace(tzinfo=timezone.utc)).days
        return {
            "id": o.id,
            "vendor_name": o.vendor_name,
            "mpn": o.mpn,
            "manufacturer": o.manufacturer,
            "qty_available": o.qty_available,
            "unit_price": float(o.unit_price) if o.unit_price else None,
            "currency": o.currency or "USD",
            "lead_time": o.lead_time,
            "date_code": o.date_code,
            "condition": o.condition,
            "packaging": o.packaging,
            "firmware": o.firmware,
            "moq": o.moq,
            "warranty": o.warranty,
            "country_of_origin": o.country_of_origin,
            "hardware_code": o.hardware_code,
            "valid_until": o.valid_until.isoformat() if o.valid_until else None,
            "source": o.source,
            "status": o.status,
            "notes": o.notes,
            "expires_at": o.expires_at.isoformat() if o.expires_at else None,
            "attribution_status": o.attribution_status,
            "selected_for_quote": o.selected_for_quote or False,
            "selected_at": o.selected_at.isoformat() if o.selected_at else None,
            "created_at": o.created_at.isoformat() if o.created_at else None,
            "age_days": age_days,
            "is_historical": is_historical,
            "is_substitute": is_substitute,
            "from_requisition_id": source_req_id,
            "entered_by": o.entered_by.name if o.entered_by else None,
        }

    # Current offers on this requirement
    current = (
        db.query(Offer)
        .options(joinedload(Offer.entered_by))
        .filter(Offer.requirement_id == requirement_id, Offer.status.in_(["active", "won"]))
        .order_by(Offer.created_at.desc())
        .all()
    )
    results = [_offer_dict(o) for o in current]

    # Historical offers from other requisitions via material card
    card_ids = set()
    if req_item.material_card_id:
        card_ids.add(req_item.material_card_id)
    # Also check substitute material cards
    from ...utils.normalization import normalize_mpn_key

    for sub in req_item.substitutes or []:
        sub_str = (sub if isinstance(sub, str) else "").strip()
        if sub_str:
            sub_key = normalize_mpn_key(sub_str)
            if sub_key:
                mc = db.query(MaterialCard.id).filter(MaterialCard.normalized_mpn == sub_key).first()
                if mc:
                    card_ids.add(mc[0])

    if card_ids:
        hist_offers = (
            db.query(Offer)
            .options(joinedload(Offer.entered_by))
            .filter(
                Offer.requisition_id != req_item.requisition_id,
                Offer.material_card_id.in_(card_ids),
                Offer.status.in_(["active", "won"]),
            )
            .order_by(Offer.created_at.desc())
            .limit(50)
            .all()
        )
        for ho in hist_offers:
            is_sub = ho.material_card_id != req_item.material_card_id
            results.append(_offer_dict(ho, is_historical=True, is_substitute=is_sub, source_req_id=ho.requisition_id))

    return results


@router.post("/api/offers/{offer_id}/toggle-quote-selection")
async def toggle_quote_selection(
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Toggle whether an offer is selected for quoting."""
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    req = get_req_for_user(db, user, offer.requisition_id)
    if not req:
        raise HTTPException(403, "Not authorized")
    offer.selected_for_quote = not offer.selected_for_quote
    offer.selected_at = datetime.now(timezone.utc) if offer.selected_for_quote else None
    db.commit()
    return {"ok": True, "selected_for_quote": offer.selected_for_quote}


@router.get("/api/requirements/{requirement_id}/notes")
async def list_requirement_notes(
    requirement_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the requirement's own notes plus notes from its offers."""
    req = db.query(Requirement).filter(Requirement.id == requirement_id).first()
    if not req:
        raise HTTPException(404, "Requirement not found")
    # Gather notes from offers that have non-empty notes
    offer_notes = (
        db.query(Offer)
        .filter(Offer.requirement_id == requirement_id, Offer.notes.isnot(None), Offer.notes != "")
        .order_by(Offer.created_at.desc())
        .all()
    )
    return {
        "requirement_notes": req.notes or "",
        "notes": [
            {
                "vendor_name": o.vendor_name,
                "note": o.notes,
                "created_at": o.created_at.isoformat() if o.created_at else None,
                "offer_id": o.id,
            }
            for o in offer_notes
        ],
    }


@router.post("/api/requirements/{requirement_id}/notes")
async def add_requirement_note(
    requirement_id: int,
    body: dict,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Append text to the requirement's notes field."""
    req = db.query(Requirement).filter(Requirement.id == requirement_id).first()
    if not req:
        raise HTTPException(404, "Requirement not found")
    new_text = (body.get("text") or "").strip()
    if not new_text:
        raise HTTPException(422, "Note text is required")
    # Append to existing notes with timestamp
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    entry = f"[{timestamp} {user.email}] {new_text}"
    req.notes = f"{req.notes}\n{entry}" if req.notes else entry
    db.commit()
    return {"ok": True, "notes": req.notes}


@router.get("/api/requirements/{requirement_id}/tasks")
async def list_requirement_tasks(
    requirement_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List tasks linked to this requirement or its offers via source_ref.

    Merges part-level tasks (source_ref=requirement:{id}) and offer-level tasks
    (source_ref=offer:{offer_id} where the offer belongs to this requirement).
    """
    from ...models import RequisitionTask

    req_item = db.query(Requirement).filter(Requirement.id == requirement_id).first()
    if not req_item:
        raise HTTPException(404, "Requirement not found")

    # Part-level tasks
    part_ref = f"requirement:{requirement_id}"

    # Offer-level tasks: find offer IDs for this requirement
    offer_ids = [oid for (oid,) in db.query(Offer.id).filter(Offer.requirement_id == requirement_id).all()]
    offer_refs = [f"offer:{oid}" for oid in offer_ids]

    all_refs = [part_ref] + offer_refs
    tasks = (
        db.query(RequisitionTask)
        .filter(
            RequisitionTask.requisition_id == req_item.requisition_id,
            RequisitionTask.source_ref.in_(all_refs),
        )
        .order_by(RequisitionTask.created_at.desc())
        .all()
    )

    # Look up assignee names
    user_ids = {t.assigned_to_id for t in tasks if t.assigned_to_id}
    user_ids |= {t.created_by for t in tasks if t.created_by}
    user_map = {}
    if user_ids:
        for u in db.query(User).filter(User.id.in_(user_ids)).all():
            user_map[u.id] = u.name or u.email

    return [
        {
            "id": t.id,
            "title": t.title,
            "description": t.description,
            "task_type": t.task_type,
            "status": t.status,
            "priority": t.priority,
            "assigned_to": user_map.get(t.assigned_to_id, ""),
            "created_by_name": user_map.get(t.created_by, ""),
            "source_ref": t.source_ref,
            "ai_risk_flag": t.ai_risk_flag,
            "source": t.source,
            "due_date": t.due_at.isoformat() if t.due_at else None,
            "completed_at": t.completed_at.isoformat() if t.completed_at else None,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in tasks
    ]


@router.post("/api/requirements/{requirement_id}/tasks")
async def create_requirement_task(
    requirement_id: int,
    body: dict,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a task linked to a specific requirement."""
    from ...models import RequisitionTask

    req = db.query(Requirement).filter(Requirement.id == requirement_id).first()
    if not req:
        raise HTTPException(404, "Requirement not found")
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(422, "Task title is required")
    from datetime import datetime as _dt

    assigned_to_id = body.get("assigned_to_id")
    due_at_raw = body.get("due_at")
    due_at = None
    if due_at_raw:
        try:
            due_at = _dt.fromisoformat(due_at_raw)
        except (ValueError, TypeError):
            pass

    task = RequisitionTask(
        requisition_id=req.requisition_id,
        title=title,
        task_type="general",
        status="todo",
        source="manual",
        source_ref=f"requirement:{requirement_id}",
        created_by=user.id,
        assigned_to_id=assigned_to_id,
        due_at=due_at,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return {
        "id": task.id,
        "title": task.title,
        "status": task.status,
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }


@router.get("/api/requirements/{requirement_id}/history")
async def list_requirement_history(
    requirement_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Part-level history timeline: change logs, contacts, offers, tasks.

    Returns a unified timeline of events for the selected part.
    """
    from ...models import RequisitionTask

    req_item = db.query(Requirement).filter(Requirement.id == requirement_id).first()
    if not req_item:
        raise HTTPException(404, "Requirement not found")

    events = []

    # Change log entries for this requirement
    changes = (
        db.query(ChangeLog)
        .filter(ChangeLog.entity_type == "requirement", ChangeLog.entity_id == requirement_id)
        .order_by(ChangeLog.created_at.desc())
        .limit(50)
        .all()
    )
    user_ids = {c.user_id for c in changes if c.user_id}
    # Offer changes for offers on this requirement
    offer_ids = [oid for (oid,) in db.query(Offer.id).filter(Offer.requirement_id == requirement_id).all()]
    offer_changes = []
    if offer_ids:
        offer_changes = (
            db.query(ChangeLog)
            .filter(ChangeLog.entity_type == "offer", ChangeLog.entity_id.in_(offer_ids))
            .order_by(ChangeLog.created_at.desc())
            .limit(50)
            .all()
        )
        user_ids |= {c.user_id for c in offer_changes if c.user_id}

    user_map = {}
    if user_ids:
        for u in db.query(User).filter(User.id.in_(user_ids)).all():
            user_map[u.id] = u.name or u.email

    for c in changes:
        events.append(
            {
                "type": "change",
                "entity": "requirement",
                "field": c.field_name,
                "old_value": c.old_value,
                "new_value": c.new_value,
                "user": user_map.get(c.user_id, ""),
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
        )
    for c in offer_changes:
        events.append(
            {
                "type": "change",
                "entity": "offer",
                "field": c.field_name,
                "old_value": c.old_value,
                "new_value": c.new_value,
                "user": user_map.get(c.user_id, ""),
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
        )

    # Offers created for this part
    offers = db.query(Offer).filter(Offer.requirement_id == requirement_id).order_by(Offer.created_at.desc()).all()
    for o in offers:
        events.append(
            {
                "type": "offer_created",
                "vendor_name": o.vendor_name,
                "mpn": o.mpn,
                "unit_price": float(o.unit_price) if o.unit_price else None,
                "qty_available": o.qty_available,
                "created_at": o.created_at.isoformat() if o.created_at else None,
            }
        )

    # Contacts (RFQs sent) that included this part's MPN
    mpn = req_item.primary_mpn
    contacts = (
        db.query(Contact)
        .filter(Contact.requisition_id == req_item.requisition_id)
        .order_by(Contact.created_at.desc())
        .limit(50)
        .all()
    )
    for ct in contacts:
        parts = ct.parts_included or []
        if mpn and mpn in parts:
            events.append(
                {
                    "type": "rfq_sent",
                    "vendor_name": ct.vendor_name,
                    "contact_type": ct.contact_type,
                    "status": ct.status,
                    "created_at": ct.created_at.isoformat() if ct.created_at else None,
                }
            )

    # Tasks completed for this part
    part_ref = f"requirement:{requirement_id}"
    offer_refs = [f"offer:{oid}" for oid in offer_ids]
    all_refs = [part_ref] + offer_refs
    done_tasks = (
        db.query(RequisitionTask)
        .filter(
            RequisitionTask.requisition_id == req_item.requisition_id,
            RequisitionTask.source_ref.in_(all_refs),
            RequisitionTask.status == "done",
        )
        .all()
    )
    for t in done_tasks:
        events.append(
            {
                "type": "task_done",
                "title": t.title,
                "completed_at": t.completed_at.isoformat() if t.completed_at else None,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
        )

    # Sort all events by created_at descending
    events.sort(key=lambda e: e.get("created_at") or "", reverse=True)
    return events
