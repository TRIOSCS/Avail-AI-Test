"""
rfq.py — RFQ, Contacts, Responses, Activity & Follow-ups Router

Batch RFQ email sending, inbox polling, vendor response tracking,
requisition activity feed, and follow-up management.

Business Rules:
- RFQ sends via M365 Graph API on behalf of logged-in buyer
- Responses matched by conversationId → headers → subject → domain
- Follow-ups track vendors who haven't replied within threshold
- Activity feed merges emails, calls, and manual entries chronologically

Called by: main.py (router mount)
Depends on: models, email_service, vendor_utils, engagement_scoring
"""

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from ..http_client import http
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload

from ..database import get_db
from ..dependencies import (
    get_req_for_user,
    require_buyer,
    require_fresh_token,
    require_user,
)
from ..models import (
    ActivityLog,
    Contact,
    Requisition,
    User,
    VendorCard,
    VendorContact,
    VendorResponse,
    VendorReview,
)
from ..schemas.rfq import BatchRfqSend, FollowUpEmail, PhoneCallLog, RfqPrepare
from ..email_service import log_phone_contact, send_batch_rfq, poll_inbox
from ..vendor_utils import normalize_vendor_name

router = APIRouter(tags=["rfq"])


@router.post("/api/contacts/phone")
async def log_call(
    payload: PhoneCallLog,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    return log_phone_contact(
        db=db,
        user_id=user.id,
        requisition_id=payload.requisition_id,
        vendor_name=payload.vendor_name,
        vendor_phone=payload.vendor_phone,
        parts=payload.parts,
    )


@router.get("/api/requisitions/{req_id}/contacts")
async def list_contacts(
    req_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    contacts = (
        db.query(Contact)
        .options(joinedload(Contact.user))
        .filter_by(requisition_id=req_id)
        .order_by(Contact.created_at.desc())
        .all()
    )
    return [
        {
            "id": c.id,
            "contact_type": c.contact_type,
            "vendor_name": c.vendor_name,
            "vendor_contact": c.vendor_contact,
            "parts_included": c.parts_included,
            "subject": c.subject,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "user_name": c.user.name if c.user else "",
        }
        for c in contacts
    ]


# ── Batch RFQ ────────────────────────────────────────────────────────────
from ..rate_limit import limiter


@router.post("/api/requisitions/{req_id}/rfq")
@limiter.limit("5/minute")
async def send_rfq(
    req_id: int,
    payload: BatchRfqSend,
    request: Request,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    token = await require_fresh_token(request, db)
    results = await send_batch_rfq(
        token=token,
        db=db,
        user_id=user.id,
        requisition_id=req_id,
        vendor_groups=[g.model_dump() for g in payload.groups],
    )
    return {"results": results}


# ── Inbox Polling ────────────────────────────────────────────────────────
@router.post("/api/requisitions/{req_id}/poll")
async def poll(
    req_id: int,
    request: Request,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    token = await require_fresh_token(request, db)
    results = await poll_inbox(
        token, db, requisition_id=req_id, scanned_by_user_id=user.id
    )
    return {"responses": results}


@router.get("/api/requisitions/{req_id}/responses")
async def list_responses(
    req_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    resps = (
        db.query(VendorResponse)
        .filter_by(requisition_id=req_id)
        .order_by(VendorResponse.created_at.desc())
        .all()
    )
    return [
        {
            "id": r.id,
            "vendor_name": r.vendor_name,
            "vendor_email": r.vendor_email,
            "subject": r.subject,
            "status": r.status,
            "parsed_data": r.parsed_data,
            "confidence": r.confidence,
            "received_at": r.received_at.isoformat()
            if isinstance(r.received_at, datetime)
            else r.received_at,
        }
        for r in resps
    ]


@router.get("/api/requisitions/{req_id}/activity")
async def get_activity(
    req_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Combined activity view: contacts + responses + tracking, grouped by vendor."""
    loop = asyncio.get_running_loop()

    def _q_contacts():
        from sqlalchemy.orm import joinedload
        return db.query(Contact).options(
            joinedload(Contact.user),
        ).filter_by(requisition_id=req_id).order_by(Contact.created_at.desc()).all()

    def _q_responses():
        return db.query(VendorResponse).filter_by(requisition_id=req_id).order_by(VendorResponse.received_at.desc()).all()

    def _q_activities():
        from sqlalchemy.orm import joinedload
        return db.query(ActivityLog).options(
            joinedload(ActivityLog.user),
        ).filter(
            ActivityLog.requisition_id == req_id,
            ActivityLog.vendor_card_id.isnot(None),
        ).order_by(ActivityLog.created_at.desc()).limit(500).all()

    contacts, responses, manual_activities = await asyncio.gather(
        loop.run_in_executor(None, _q_contacts),
        loop.run_in_executor(None, _q_responses),
        loop.run_in_executor(None, _q_activities),
    )

    # Build a vendor_card_id → vendor_name lookup for activities
    activity_vendor_ids = {a.vendor_card_id for a in manual_activities}
    vendor_name_map = {}
    if activity_vendor_ids:
        cards = db.query(VendorCard).filter(VendorCard.id.in_(activity_vendor_ids)).all()
        vendor_name_map = {c.id: c.display_name for c in cards}

    # Group by normalized vendor name
    vendors = {}
    for c in contacts:
        vk = normalize_vendor_name(c.vendor_name)
        if vk not in vendors:
            vendors[vk] = {
                "vendor_name": c.vendor_name,
                "contacts": [],
                "responses": [],
                "activities": [],
                "all_parts": set(),
                "contact_types": set(),
            }
        vendors[vk]["contacts"].append(
            {
                "id": c.id,
                "contact_type": c.contact_type,
                "vendor_contact": c.vendor_contact,
                "subject": c.subject,
                "body": c.details or "",
                "parts_included": c.parts_included or [],
                "created_at": c.created_at.isoformat() if c.created_at else None,
                "user_name": c.user.name if c.user else "",
                "status": c.status or "sent",
                "status_updated_at": c.status_updated_at.isoformat()
                if c.status_updated_at
                else None,
            }
        )
        vendors[vk]["contact_types"].add(c.contact_type)
        for p in c.parts_included or []:
            vendors[vk]["all_parts"].add(p)

    # Build contact_id → vendor_name lookup so responses group with their outbound contact
    contact_vendor_map = {c.id: c.vendor_name for c in contacts}

    for r in responses:
        # Group under the linked contact's vendor name when available,
        # so replies from "John Smith" appear under "Acme Corp" thread
        group_name = (
            contact_vendor_map.get(r.contact_id, r.vendor_name)
            if r.contact_id
            else r.vendor_name
        )
        vk = normalize_vendor_name(group_name)
        if vk not in vendors:
            vendors[vk] = {
                "vendor_name": group_name,
                "contacts": [],
                "responses": [],
                "activities": [],
                "all_parts": set(),
                "contact_types": set(),
            }
        vendors[vk]["responses"].append(
            {
                "id": r.id,
                "vendor_email": r.vendor_email,
                "subject": r.subject,
                "body": r.body or "",
                "status": r.status,
                "parsed_data": r.parsed_data,
                "confidence": r.confidence,
                "classification": r.classification,
                "received_at": r.received_at.isoformat()
                if isinstance(r.received_at, datetime)
                else r.received_at,
            }
        )

    # Add manual activities into vendor groups
    for a in manual_activities:
        vname = vendor_name_map.get(a.vendor_card_id, f"Vendor #{a.vendor_card_id}")
        vk = normalize_vendor_name(vname)
        if vk not in vendors:
            vendors[vk] = {
                "vendor_name": vname,
                "contacts": [],
                "responses": [],
                "activities": [],
                "all_parts": set(),
                "contact_types": set(),
            }
        vendors[vk]["activities"].append(
            {
                "id": a.id,
                "activity_type": a.activity_type,
                "channel": a.channel,
                "contact_name": a.contact_name,
                "contact_phone": a.contact_phone,
                "notes": a.notes,
                "duration_seconds": a.duration_seconds,
                "user_name": a.user.name if a.user else "",
                "created_at": a.created_at.isoformat() if a.created_at else None,
                "vendor_card_id": a.vendor_card_id,
            }
        )

    # Resolve vendor_card_id for each vendor group (for call/note buttons)
    # First try from activities, then look up by normalized name
    vendor_card_ids = {}
    for vk, v in vendors.items():
        if v["activities"]:
            vendor_card_ids[vk] = v["activities"][0]["vendor_card_id"]
    # Bulk lookup remaining by normalized name
    unresolved = [vk for vk in vendors if vk not in vendor_card_ids]
    if unresolved:
        name_list = [vk for vk in unresolved]
        cards = (
            db.query(VendorCard)
            .filter(VendorCard.normalized_name.in_(name_list))
            .all()
        )
        for c in cards:
            nk = c.normalized_name
            if nk in vendors and nk not in vendor_card_ids:
                vendor_card_ids[nk] = c.id

    # Collect phone numbers from VendorCard and VendorContact for resolved vendors
    vendor_phones = {}
    all_card_ids = [cid for cid in vendor_card_ids.values() if cid]
    if all_card_ids:
        phone_cards = (
            db.query(VendorCard)
            .filter(VendorCard.id.in_(all_card_ids))
            .all()
        )
        for pc in phone_cards:
            phones = []
            if pc.phones:
                phones.extend(pc.phones)
            vendor_phones[pc.id] = phones
        # Also check VendorContact records for phone numbers
        vcontacts = (
            db.query(VendorContact)
            .filter(
                VendorContact.vendor_card_id.in_(all_card_ids),
                VendorContact.phone.isnot(None),
            )
            .all()
        )
        for vc in vcontacts:
            if vc.phone and vc.phone not in vendor_phones.get(vc.vendor_card_id, []):
                vendor_phones.setdefault(vc.vendor_card_id, []).append(vc.phone)

    # Build result list
    result = []
    for vk, v in vendors.items():
        last_contact = v["contacts"][0] if v["contacts"] else None
        has_response = len(v["responses"]) > 0

        # Derive vendor-level status from best contact status + responses
        # Priority: quoted > declined > replied > responded > opened > awaiting
        contact_statuses = {c.get("status", "sent") for c in v["contacts"]}
        if has_response or "quoted" in contact_statuses:
            if "quoted" in contact_statuses:
                vendor_status = "quoted"
            elif "declined" in contact_statuses:
                # Has responses, at least one declined
                vendor_status = (
                    "replied"
                    if (contact_statuses - {"declined", "sent", "opened"})
                    else "declined"
                )
            else:
                vendor_status = "replied"
        elif "responded" in contact_statuses:
            vendor_status = "replied"
        elif "declined" in contact_statuses:
            vendor_status = "declined"
        elif "opened" in contact_statuses:
            vendor_status = "opened"
        else:
            vendor_status = "awaiting"

        card_id = vendor_card_ids.get(vk)
        result.append(
            {
                "vendor_name": v["vendor_name"],
                "vendor_card_id": card_id,
                "vendor_phones": vendor_phones.get(card_id, []) if card_id else [],
                "status": vendor_status,
                "contact_count": len(v["contacts"]),
                "contact_types": sorted(v["contact_types"]),
                "all_parts": sorted(v["all_parts"]),
                "last_contacted_at": last_contact["created_at"]
                if last_contact
                else None,
                "last_contacted_by": last_contact["user_name"]
                if last_contact
                else None,
                "last_contact_email": last_contact["vendor_contact"]
                if last_contact
                else None,
                "contacts": v["contacts"],
                "responses": v["responses"],
                "activities": v["activities"],
            }
        )

    # Sort by most recent contact
    result.sort(key=lambda x: x["last_contacted_at"] or "", reverse=True)

    # Summary counts
    sent = len(result)
    replied = sum(1 for r in result if r["status"] in ("replied", "quoted"))
    opened = sum(1 for r in result if r["status"] == "opened")
    declined = sum(1 for r in result if r["status"] == "declined")
    awaiting = sent - replied - opened - declined

    return {
        "vendors": result,
        "summary": {
            "sent": sent,
            "replied": replied,
            "opened": opened,
            "awaiting": awaiting,
        },
    }


# ── RFQ Prepare ─────────────────────────────────────────────────────────
@router.post("/api/requisitions/{req_id}/rfq-prepare")
async def rfq_prepare(
    req_id: int,
    payload: RfqPrepare,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    """Return vendor card data + exhaustion info for selected vendors before RFQ send."""
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404)
    vendors = payload.vendors

    # All MPNs on this requisition + substitutes map
    all_parts = [r.primary_mpn for r in req.requirements if r.primary_mpn]
    subs_map = {}
    for r in req.requirements:
        if r.primary_mpn and r.substitutes:
            subs_map[r.primary_mpn] = [s for s in r.substitutes if s]

    # Build exhaustion map: {normalized_vendor: [parts_already_asked]}
    contacts = db.query(Contact).filter_by(requisition_id=req_id).all()
    exhaustion = {}
    for c in contacts:
        vk = normalize_vendor_name(c.vendor_name)
        if vk not in exhaustion:
            exhaustion[vk] = set()
        for p in c.parts_included or []:
            exhaustion[vk].add(p.upper())

    results = []
    for v in vendors[:50]:
        vendor_name = v.vendor_name
        norm = normalize_vendor_name(vendor_name)
        card = db.query(VendorCard).filter_by(normalized_name=norm).first()
        already_asked = sorted(exhaustion.get(norm, set()))

        base = {
            "vendor_name": vendor_name,
            "display_name": card.display_name if card else vendor_name,
            "card_id": card.id if card else None,
            "already_asked": already_asked,
        }
        if card and card.emails:
            base.update(
                {
                    "emails": card.emails or [],
                    "phones": card.phones or [],
                    "needs_lookup": False,
                    "contact_source": "cached",
                }
            )
        else:
            base.update({"emails": [], "phones": [], "needs_lookup": True})
        results.append(base)

    return {"vendors": results, "all_parts": all_parts, "subs_map": subs_map}


# ── Follow-Up Detection ───────────────────────────────────────────────
@router.get("/api/follow-ups")
async def get_follow_ups(
    user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Return contacts that need follow-up: sent/opened > 3 days ago with no response."""

    three_days_ago = datetime.now(timezone.utc) - timedelta(days=3)

    # Find contacts with stale status and no matching vendor response
    stale_contacts = db.query(Contact).filter(
        Contact.contact_type == "email",
        Contact.status.in_(["sent", "opened"]),
        Contact.created_at < three_days_ago,
    )
    # Sales/trader sees only their own reqs' follow-ups
    if user.role in ("sales", "trader"):
        stale_contacts = stale_contacts.join(Requisition).filter(
            Requisition.created_by == user.id
        )

    stale = stale_contacts.order_by(Contact.created_at.asc()).limit(500).all()

    # Pre-fetch requisition names (avoids N+1 query per contact)
    req_ids = {c.requisition_id for c in stale}
    req_names: dict[int, str] = {}
    if req_ids:
        name_rows = (
            db.query(Requisition.id, Requisition.name)
            .filter(Requisition.id.in_(req_ids))
            .all()
        )
        req_names = {r.id: r.name for r in name_rows}

    results = []
    now = datetime.now(timezone.utc)
    for c in stale:
        ca = c.created_at.replace(tzinfo=None) if c.created_at else now.replace(tzinfo=None)
        days_waiting = (now.replace(tzinfo=None) - ca).days
        results.append(
            {
                "contact_id": c.id,
                "requisition_id": c.requisition_id,
                "requisition_name": req_names.get(c.requisition_id, "Unknown"),
                "vendor_name": c.vendor_name,
                "vendor_email": c.vendor_contact,
                "parts": c.parts_included or [],
                "status": c.status,
                "sent_at": c.created_at.isoformat() if c.created_at else None,
                "days_waiting": days_waiting,
                "subject": c.subject,
            }
        )

    return {"follow_ups": results, "count": len(results)}


@router.get("/api/follow-ups/summary")
async def follow_up_summary(
    user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Cross-req follow-up counts for badge display."""
    three_days_ago = datetime.now(timezone.utc) - timedelta(days=3)

    query = (
        db.query(
            Requisition.id,
            Requisition.name,
            sqlfunc.count(Contact.id).label("stale_count"),
        )
        .join(Contact)
        .filter(
            Contact.contact_type == "email",
            Contact.status.in_(["sent", "opened"]),
            Contact.created_at < three_days_ago,
        )
        .group_by(Requisition.id, Requisition.name)
    )

    if user.role in ("sales", "trader"):
        query = query.filter(Requisition.created_by == user.id)

    rows = query.all()
    total = sum(r.stale_count for r in rows)
    by_req = [
        {"req_id": r.id, "req_name": r.name, "count": r.stale_count} for r in rows
    ]

    return {"total": total, "by_requisition": by_req}


@router.post("/api/follow-ups/{contact_id}/send")
async def send_follow_up(
    contact_id: int,
    payload: FollowUpEmail,
    request: Request,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    """Send a follow-up email for a stale contact."""
    token = await require_fresh_token(request, db)

    contact = db.query(Contact).filter_by(id=contact_id).first()
    if not contact:
        raise HTTPException(404, "Contact not found")

    body = payload.body
    if not body:
        # Default follow-up template
        parts_str = ", ".join(contact.parts_included or ["your parts"])
        body = f"Hi, following up on our RFQ below regarding {parts_str}. Please advise on availability and pricing at your earliest convenience. Thank you."

    from ..email_service import _graph_post, _build_html_body, GRAPH

    html_body = _build_html_body(body)

    subject = (
        f"Re: {contact.subject}"
        if contact.subject
        else "Follow-Up — RFQ from TRIO Supply Chain"
    )

    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": contact.vendor_contact}}],
        },
        "saveToSentItems": "true",
    }

    resp = await _graph_post(http, f"{GRAPH}/me/sendMail", token, payload)

    if resp.status_code not in (200, 202):
        raise HTTPException(502, f"Failed to send follow-up: {resp.status_code}")

    # Update contact status
    contact.status = "sent"  # Reset to sent — new follow-up cycle
    contact.status_updated_at = datetime.now(timezone.utc)
    db.commit()

    return {"ok": True, "message": f"Follow-up sent to {contact.vendor_contact}"}


# ── Search enrichment with vendor cards ────────────────────────────────
def _enrich_with_vendor_cards(results: dict, db: Session):
    """Add vendor card rating info to search results. No contact lookup."""
    all_vendor_names = set()
    for group in results.values():
        for s in group.get("sightings", []):
            if s.get("vendor_name"):
                all_vendor_names.add(s["vendor_name"])
    if not all_vendor_names:
        return

    # Build normalized name map
    norm_map = {}
    for name in all_vendor_names:
        norm = normalize_vendor_name(name)
        norm_map.setdefault(norm, []).append(name)

    cards = (
        db.query(VendorCard)
        .filter(VendorCard.normalized_name.in_(norm_map.keys()))
        .all()
    )
    card_by_norm = {c.normalized_name: c for c in cards}

    # Auto-create cards for vendors we haven't seen before
    new_cards_added = False
    for norm, names in norm_map.items():
        if norm not in card_by_norm and norm:
            card = VendorCard(
                normalized_name=norm,
                display_name=names[0],
                emails=[],
                phones=[],
                sighting_count=0,
            )
            db.add(card)
            cards.append(card)
            card_by_norm[norm] = card
            new_cards_added = True
    if new_cards_added:
        db.flush()  # Assign IDs to new cards

    # Batch fetch reviews
    card_ids = [c.id for c in cards]
    all_reviews = (
        db.query(VendorReview).filter(VendorReview.vendor_card_id.in_(card_ids)).all()
        if card_ids
        else []
    )
    reviews_by_card = {}
    for r in all_reviews:
        reviews_by_card.setdefault(r.vendor_card_id, []).append(r)

    # Build summary cache
    summary_cache = {}
    for norm, card in card_by_norm.items():
        revs = reviews_by_card.get(card.id, [])
        avg = round(sum(r.rating for r in revs) / len(revs), 1) if revs else None
        summary_cache[norm] = {
            "card_id": card.id,
            "avg_rating": avg,
            "review_count": len(revs),
            "engagement_score": round(card.engagement_score, 1)
            if card.engagement_score
            else None,
            "has_emails": bool(card.emails),
            "email_count": len(card.emails or []),
            "is_blacklisted": card.is_blacklisted or False,
        }

    # Count distinct MPNs per vendor (not raw result count, avoids inflation on re-search)
    mpns_by_norm = {}
    emails_by_norm = {}
    phones_by_norm = {}
    websites_by_norm = {}
    for group in results.values():
        for s in group.get("sightings", []):
            if (
                not s.get("is_historical")
                and not s.get("is_material_history")
                and s.get("vendor_name")
            ):
                n = normalize_vendor_name(s["vendor_name"])
                mpns_by_norm.setdefault(n, set()).add(
                    (s.get("mpn_matched") or "").lower()
                )
                if s.get("vendor_email"):
                    emails_by_norm.setdefault(n, set()).add(
                        s["vendor_email"].strip().lower()
                    )
                if s.get("vendor_phone"):
                    phones_by_norm.setdefault(n, set()).add(s["vendor_phone"].strip())
                if s.get("vendor_url"):
                    websites_by_norm.setdefault(n, s["vendor_url"])

    cards_dirty = False
    from ..vendor_utils import merge_emails_into_card, merge_phones_into_card

    for card in cards:
        # Update sighting count (distinct MPNs seen, not raw result count)
        mpn_set = mpns_by_norm.get(card.normalized_name, set())
        count = len(mpn_set - {""})  # Exclude empty MPN matches
        if count > 0:
            card.sighting_count = (card.sighting_count or 0) + count
            cards_dirty = True

        # Merge harvested emails into vendor card
        new_emails = list(emails_by_norm.get(card.normalized_name, set()))
        if merge_emails_into_card(card, new_emails) > 0:
            cards_dirty = True

        # Merge harvested phones into vendor card
        new_phones = list(phones_by_norm.get(card.normalized_name, set()))
        if merge_phones_into_card(card, new_phones) > 0:
            cards_dirty = True

        # Set website if we don't have one
        if not card.website and card.normalized_name in websites_by_norm:
            card.website = websites_by_norm[card.normalized_name]
            cards_dirty = True

    if cards_dirty:
        db.commit()
        # Refresh summary cache with updated email counts
        for norm, card in card_by_norm.items():
            if norm in summary_cache:
                summary_cache[norm]["has_emails"] = bool(card.emails)
                summary_cache[norm]["email_count"] = len(card.emails or [])

    # Enrich each sighting + filter blacklisted + filter garbage vendors
    _GARBAGE_VENDORS = {"no seller listed", "no seller", "n/a", "unknown", ""}
    empty_summary = {
        "card_id": None,
        "avg_rating": None,
        "review_count": 0,
        "engagement_score": None,
        "has_emails": False,
        "email_count": 0,
        "is_blacklisted": False,
    }
    for group in results.values():
        enriched = []
        for s in group.get("sightings", []):
            vname = (s.get("vendor_name") or "").strip()
            if vname.lower() in _GARBAGE_VENDORS:
                continue  # Skip garbage vendor names
            norm = normalize_vendor_name(vname)
            summary = summary_cache.get(norm, empty_summary)
            if summary.get("is_blacklisted"):
                continue  # Skip blacklisted vendors
            s["vendor_card"] = summary
            enriched.append(s)
        group["sightings"] = enriched
