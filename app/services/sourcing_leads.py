"""Sourcing lead service — lead upsert, evidence append, and buyer workflow events.

Purpose:
- Convert raw sightings into canonical sourcing leads.
- Keep confidence and vendor safety as separate, explainable dimensions.
- Preserve evidence attribution and buyer outcome history.

Business Rules Enforced:
- One lead per requirement + vendor + matched part.
- Evidence is append-only and never replaces raw source attribution.
- Buyer status changes are persisted as feedback events.

Called by:
- app.search_service
- app.routers.requisitions.requirements

Depends on:
- app.models.sourcing_lead
- app.models.vendors
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.sourcing import Requirement, Sighting
from app.models.sourcing_lead import LeadEvidence, LeadFeedbackEvent, SourcingLead
from app.models.vendors import VendorCard
from app.scoring import explain_lead

BUYER_STATUSES = {
    "new",
    "contacted",
    "replied",
    "no_stock",
    "has_stock",
    "bad_lead",
    "do_not_contact",
}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def normalize_mpn(mpn: str | None) -> str:
    if not mpn:
        return ""
    return (
        mpn.upper()
        .replace("-", "")
        .replace("_", "")
        .replace(" ", "")
        .replace("/", "")
        .replace(".", "")
    )


def normalize_vendor_name(name: str | None) -> str:
    if not name:
        return ""
    return " ".join((name.lower().strip()).split())


def _clamp(value: float, minimum: float = 0.0, maximum: float = 100.0) -> float:
    return max(minimum, min(maximum, value))


def _confidence_band(score: float) -> str:
    if score >= 80:
        return "high"
    if score >= 60:
        return "medium"
    if score >= 35:
        return "low"
    return "very_low"


def _safety_band(score: float) -> str:
    if score >= 75:
        return "low_risk"
    if score >= 50:
        return "medium_risk"
    return "high_risk"


def _source_reliability(source_type: str, evidence_tier: str | None) -> float:
    source_type = (source_type or "").lower()
    if source_type in {"digikey", "mouser", "farnell", "element14", "nexar", "octopart"}:
        base = 90
    elif source_type in {"netcomponents", "icsource", "brokerbin", "sourcengine"}:
        base = 72
    elif source_type in {"salesforce", "avail_history"}:
        base = 85
    elif source_type in {"ai", "web"}:
        base = 40
    else:
        base = 60

    if evidence_tier and evidence_tier.upper().startswith("T"):
        tier = evidence_tier.upper()
        tier_bonus = {"T1": 8, "T2": 5, "T3": 2, "T4": 0, "T5": -5, "T6": -10, "T7": -15}.get(tier, 0)
        base += tier_bonus
    return _clamp(float(base))


def _freshness_score(created_at: datetime | None) -> float:
    if not created_at:
        return 45.0
    age_days = max(((_now_utc() - created_at).total_seconds() / 86400.0), 0.0)
    if age_days <= 1:
        return 95.0
    if age_days <= 3:
        return 85.0
    if age_days <= 7:
        return 72.0
    if age_days <= 14:
        return 58.0
    if age_days <= 30:
        return 42.0
    return 25.0


def _contactability_score(sighting: Sighting, vendor_card: VendorCard | None) -> float:
    score = 0.0
    if sighting.contact_email:
        score += 45
    if sighting.contact_phone:
        score += 35
    if (sighting.raw_data or {}).get("website") or (sighting.raw_data or {}).get("vendor_url"):
        score += 20
    if vendor_card:
        if vendor_card.emails:
            score = max(score, 70.0)
        if vendor_card.phones:
            score = max(score, 60.0)
    return _clamp(score)


def _historical_success_score(vendor_card: VendorCard | None) -> float:
    if not vendor_card:
        return 45.0
    score = 55.0
    if getattr(vendor_card, "vendor_score", None) is not None:
        score = float(vendor_card.vendor_score)
    if getattr(vendor_card, "is_blacklisted", False):
        score -= 40
    return _clamp(score)


def _compute_confidence(
    sighting: Sighting,
    source_reliability: float,
    freshness: float,
    contactability: float,
    historical: float,
) -> float:
    base = float(sighting.score if sighting.score is not None else (sighting.confidence or 0.0) * 100.0)
    weighted = (base * 0.5) + (source_reliability * 0.2) + (freshness * 0.15) + (contactability * 0.1) + (historical * 0.05)
    return round(_clamp(weighted), 1)


def _compute_vendor_safety(vendor_card: VendorCard | None, contactability: float) -> tuple[float, list[str], str]:
    score = 50.0
    flags: list[str] = []
    if vendor_card:
        score += 12
        if getattr(vendor_card, "vendor_score", None) is not None:
            score += (float(vendor_card.vendor_score) - 50.0) * 0.25
        if getattr(vendor_card, "is_blacklisted", False):
            score -= 45
            flags.append("internal_do_not_contact_history")
        if not getattr(vendor_card, "website", None):
            score -= 8
            flags.append("limited_business_footprint")
    else:
        flags.append("no_internal_vendor_profile")

    if contactability < 40:
        score -= 12
        flags.append("limited_verified_contact_channels")

    score = round(_clamp(score), 1)
    band = _safety_band(score)
    if band == "high_risk":
        summary = "Caution advised: verify identity and stock before outreach."
    elif band == "medium_risk":
        summary = "Moderate caution: confirm business footprint and contact path."
    else:
        summary = "Lower risk from current data, but still verify stock in outreach."
    return score, flags, summary


def _source_reference(sighting: Sighting) -> str:
    raw = sighting.raw_data or {}
    return (
        raw.get("vendor_url")
        or raw.get("url")
        or raw.get("click_url")
        or raw.get("octopart_url")
        or f"sighting:{sighting.id}"
    )


def _source_name(source_type: str) -> str:
    st = (source_type or "").strip()
    return st.title() if st else "Unknown"


def _suggested_next_action(confidence_score: float, safety_score: float, contactability: float) -> str:
    if safety_score < 50:
        return "Verify vendor identity/contact first, then decide on outreach."
    if contactability < 35:
        return "Enrich verified contact details before buyer outreach."
    if confidence_score >= 75:
        return "Contact now and verify live stock + date code."
    return "Contact with caution and request current stock confirmation."


def _find_vendor_card(db: Session, normalized_vendor: str) -> VendorCard | None:
    if not normalized_vendor:
        return None
    return db.query(VendorCard).filter(VendorCard.normalized_name == normalized_vendor).first()


def _lead_key(requirement_id: int, vendor_normalized: str, matched_part: str) -> str:
    return f"{requirement_id}:{vendor_normalized}:{matched_part}".lower()


def upsert_lead_from_sighting(db: Session, requirement: Requirement, sighting: Sighting) -> SourcingLead:
    vendor_name = (sighting.vendor_name or "").strip() or "Unknown Vendor"
    vendor_normalized = (sighting.vendor_name_normalized or normalize_vendor_name(vendor_name) or "").strip()
    matched_part = (sighting.mpn_matched or sighting.mpn or requirement.primary_mpn or "").strip()
    requested_part = (requirement.primary_mpn or "").strip()
    if not matched_part:
        matched_part = requested_part

    matched_part_norm = normalize_mpn(matched_part) or matched_part.upper()
    vendor_card = _find_vendor_card(db, vendor_normalized)
    source_reliability = _source_reliability(sighting.source_type, sighting.evidence_tier)
    freshness = _freshness_score(sighting.created_at)
    contactability = _contactability_score(sighting, vendor_card)
    historical = _historical_success_score(vendor_card)
    confidence_score = _compute_confidence(sighting, source_reliability, freshness, contactability, historical)
    confidence_band = _confidence_band(confidence_score)
    safety_score, safety_flags, safety_summary = _compute_vendor_safety(vendor_card, contactability)

    lead = (
        db.query(SourcingLead)
        .filter(
            SourcingLead.requirement_id == requirement.id,
            SourcingLead.vendor_name_normalized == vendor_normalized,
            SourcingLead.part_number_matched == matched_part_norm,
        )
        .first()
    )

    if lead is None:
        lead = SourcingLead(
            lead_id=f"ld_{uuid.uuid4().hex[:24]}",
            requirement_id=requirement.id,
            requisition_id=requirement.requisition_id,
            part_number_requested=requested_part,
            part_number_matched=matched_part_norm,
            match_type="exact" if matched_part_norm == normalize_mpn(requested_part) else "near",
            vendor_name=vendor_name,
            vendor_name_normalized=vendor_normalized,
            vendor_card_id=vendor_card.id if vendor_card else None,
            primary_source_type=(sighting.source_type or "unknown"),
            primary_source_name=_source_name(sighting.source_type or ""),
            source_reference=_source_reference(sighting),
            source_first_seen_at=sighting.created_at or _now_utc(),
            buyer_status="new",
        )
        db.add(lead)

    lead.requisition_id = requirement.requisition_id
    lead.vendor_name = vendor_name
    lead.vendor_name_normalized = vendor_normalized
    lead.vendor_card_id = vendor_card.id if vendor_card else None
    lead.part_number_requested = requested_part
    lead.part_number_matched = matched_part_norm
    lead.source_last_seen_at = sighting.created_at or _now_utc()
    lead.primary_source_type = lead.primary_source_type or (sighting.source_type or "unknown")
    lead.primary_source_name = lead.primary_source_name or _source_name(sighting.source_type or "")
    lead.source_reference = lead.source_reference or _source_reference(sighting)
    lead.contact_email = lead.contact_email or sighting.contact_email
    lead.contact_phone = lead.contact_phone or sighting.contact_phone
    lead.contact_url = lead.contact_url or ((sighting.raw_data or {}).get("website") or (sighting.raw_data or {}).get("vendor_url"))

    lead.freshness_score = freshness
    lead.source_reliability_score = source_reliability
    lead.contactability_score = contactability
    lead.historical_success_score = historical
    lead.confidence_score = confidence_score
    lead.confidence_band = confidence_band
    lead.vendor_safety_score = safety_score
    lead.vendor_safety_band = _safety_band(safety_score)
    lead.vendor_safety_summary = safety_summary
    lead.vendor_safety_flags = safety_flags
    lead.vendor_safety_last_checked_at = _now_utc()
    lead.reason_summary = explain_lead(
        {
            "source_type": sighting.source_type,
            "qty_available": sighting.qty_available,
            "price": sighting.unit_price,
            "contact_email": lead.contact_email,
            "contact_phone": lead.contact_phone,
        }
    )
    lead.suggested_next_action = _suggested_next_action(confidence_score, safety_score, contactability)
    lead.risk_flags = _build_lead_risk_flags(confidence_score, source_reliability, freshness, contactability)
    lead.updated_at = _now_utc()
    return lead


def _build_lead_risk_flags(
    confidence_score: float,
    source_reliability: float,
    freshness_score: float,
    contactability_score: float,
) -> list[str]:
    flags: list[str] = []
    if source_reliability < 50:
        flags.append("lower_reliability_source")
    if freshness_score < 45:
        flags.append("stale_signal")
    if contactability_score < 35:
        flags.append("limited_contactability")
    if confidence_score < 40:
        flags.append("low_stock_confidence")
    return flags


def append_evidence_from_sighting(db: Session, lead: SourcingLead, sighting: Sighting) -> None:
    source_ref = _source_reference(sighting)
    exists = (
        db.query(LeadEvidence.id)
        .filter(
            LeadEvidence.lead_id == lead.id,
            LeadEvidence.source_type == (sighting.source_type or ""),
            LeadEvidence.source_reference == source_ref,
            LeadEvidence.part_number_observed == (sighting.mpn_matched or sighting.mpn or ""),
        )
        .first()
    )
    if exists:
        return

    freshness_days = None
    if sighting.created_at:
        freshness_days = max(((_now_utc() - sighting.created_at).total_seconds() / 86400.0), 0.0)

    evidence = LeadEvidence(
        evidence_id=f"ev_{uuid.uuid4().hex[:24]}",
        lead_id=lead.id,
        signal_type="stock_listing",
        source_type=sighting.source_type or "unknown",
        source_name=_source_name(sighting.source_type or ""),
        source_reference=source_ref,
        part_number_observed=(sighting.mpn_matched or sighting.mpn or ""),
        vendor_name_observed=sighting.vendor_name,
        observed_text=(sighting.raw_data or {}).get("description") or (sighting.raw_data or {}).get("evidence_note"),
        observed_at=sighting.created_at,
        freshness_age_days=freshness_days,
        weight=lead.source_reliability_score,
        confidence_impact=lead.confidence_score,
        explanation=lead.reason_summary,
        source_reliability_band=_confidence_band(lead.source_reliability_score or 0),
        verification_state="raw",
    )
    db.add(evidence)


def _refresh_lead_evidence_rollups(db: Session, lead: SourcingLead) -> None:
    evidence_count = db.query(func.count(LeadEvidence.id)).filter(LeadEvidence.lead_id == lead.id).scalar() or 0
    source_count = (
        db.query(func.count(func.distinct(LeadEvidence.source_type))).filter(LeadEvidence.lead_id == lead.id).scalar() or 0
    )
    lead.evidence_count = int(evidence_count)
    lead.corroborated = source_count >= 2
    if lead.corroborated and lead.confidence_score is not None:
        lead.confidence_score = _clamp(float(lead.confidence_score) + 5.0)
        lead.confidence_band = _confidence_band(float(lead.confidence_score))


def sync_leads_for_sightings(db: Session, requirement: Requirement, sightings: list[Sighting]) -> int:
    if not sightings:
        return 0
    synced = 0
    for sighting in sightings:
        if not sighting.vendor_name:
            continue
        lead = upsert_lead_from_sighting(db, requirement, sighting)
        db.flush()
        append_evidence_from_sighting(db, lead, sighting)
        db.flush()
        _refresh_lead_evidence_rollups(db, lead)
        synced += 1
    try:
        db.commit()
    except Exception as exc:
        logger.warning("Sourcing lead sync failed for requirement {}: {}", requirement.id, exc)
        db.rollback()
        return 0
    return synced


def attach_lead_metadata_to_results(db: Session, results_by_requirement: dict[int, list[dict]]) -> None:
    if not results_by_requirement:
        return
    req_ids = [rid for rid in results_by_requirement.keys() if rid]
    if not req_ids:
        return

    leads = db.query(SourcingLead).filter(SourcingLead.requirement_id.in_(req_ids)).all()
    by_key = {
        _lead_key(lead.requirement_id, lead.vendor_name_normalized or "", lead.part_number_matched or ""): lead for lead in leads
    }
    for requirement_id, rows in results_by_requirement.items():
        for row in rows:
            vendor_norm = normalize_vendor_name((row.get("vendor_name") or "").strip()) or ""
            part_norm = normalize_mpn((row.get("mpn_matched") or row.get("mpn") or "").strip()) or ""
            key = _lead_key(requirement_id, vendor_norm, part_norm)
            lead = by_key.get(key)
            if not lead:
                continue
            row["lead_id"] = lead.id
            row["lead_public_id"] = lead.lead_id
            row["buyer_status"] = lead.buyer_status
            row["confidence_score"] = lead.confidence_score
            row["confidence_band"] = lead.confidence_band
            row["vendor_safety_score"] = lead.vendor_safety_score
            row["vendor_safety_band"] = lead.vendor_safety_band
            row["vendor_safety_summary"] = lead.vendor_safety_summary
            row["suggested_next_action"] = lead.suggested_next_action
            row["risk_flags"] = lead.risk_flags or []
            row["lead_reason_summary"] = lead.reason_summary


def get_requisition_leads(db: Session, requisition_id: int, statuses: list[str] | None = None) -> list[SourcingLead]:
    query = db.query(SourcingLead).filter(SourcingLead.requisition_id == requisition_id)
    if statuses:
        clean_statuses = [s for s in statuses if s in BUYER_STATUSES]
        if clean_statuses:
            query = query.filter(SourcingLead.buyer_status.in_(clean_statuses))
    return query.order_by(SourcingLead.confidence_score.desc(), SourcingLead.updated_at.desc()).all()


def update_lead_status(
    db: Session,
    lead_id: int,
    status: str,
    *,
    note: str | None = None,
    reason_code: str | None = None,
    contact_method: str | None = None,
    contact_attempt_count: int = 0,
    actor_user_id: int | None = None,
) -> SourcingLead | None:
    status = (status or "").strip().lower()
    if status not in BUYER_STATUSES:
        raise ValueError(f"Unsupported lead status: {status}")

    lead = db.query(SourcingLead).filter(SourcingLead.id == lead_id).first()
    if not lead:
        return None

    lead.buyer_status = status
    lead.last_buyer_action_at = _now_utc()
    if note:
        lead.buyer_feedback_summary = note

    if status == "has_stock":
        lead.confidence_score = _clamp((lead.confidence_score or 0.0) + 12)
    elif status == "no_stock":
        lead.confidence_score = _clamp((lead.confidence_score or 0.0) - 14)
    elif status == "bad_lead":
        lead.confidence_score = _clamp((lead.confidence_score or 0.0) - 18)
        lead.vendor_safety_score = _clamp((lead.vendor_safety_score or 0.0) - 8)
    elif status == "do_not_contact":
        lead.vendor_safety_score = _clamp((lead.vendor_safety_score or 0.0) - 30)
        lead.vendor_safety_flags = sorted(set((lead.vendor_safety_flags or []) + ["buyer_marked_do_not_contact"]))

    lead.confidence_band = _confidence_band(float(lead.confidence_score or 0.0))
    if lead.vendor_safety_score is not None:
        lead.vendor_safety_band = _safety_band(float(lead.vendor_safety_score))

    event = LeadFeedbackEvent(
        lead_id=lead.id,
        status=status,
        note=note,
        reason_code=reason_code,
        contact_method=contact_method,
        contact_attempt_count=max(int(contact_attempt_count or 0), 0),
        created_by_user_id=actor_user_id,
    )
    db.add(event)
    db.commit()
    db.refresh(lead)
    return lead


def append_lead_feedback(
    db: Session,
    lead_id: int,
    *,
    note: str | None = None,
    reason_code: str | None = None,
    contact_method: str | None = None,
    contact_attempt_count: int = 0,
    actor_user_id: int | None = None,
) -> SourcingLead | None:
    lead = db.query(SourcingLead).filter(SourcingLead.id == lead_id).first()
    if not lead:
        return None

    event = LeadFeedbackEvent(
        lead_id=lead.id,
        status=lead.buyer_status,
        note=note,
        reason_code=reason_code,
        contact_method=contact_method,
        contact_attempt_count=max(int(contact_attempt_count or 0), 0),
        created_by_user_id=actor_user_id,
    )
    if note:
        lead.buyer_feedback_summary = note
    lead.last_buyer_action_at = _now_utc()
    db.add(event)
    db.commit()
    db.refresh(lead)
    return lead
