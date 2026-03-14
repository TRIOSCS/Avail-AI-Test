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
from sqlalchemy.orm import Session

from app.models.sourcing import Requirement, Sighting
from app.models.sourcing_lead import LeadEvidence, LeadFeedbackEvent, SourcingLead
from app.models.vendors import VendorCard
from app.scoring import explain_lead
from app.vendor_utils import normalize_vendor_name

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


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


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


def _normalize_phone(phone: str | None) -> str:
    """Strip a phone number to digits only for dedup comparison."""
    if not phone:
        return ""
    return "".join(c for c in phone if c.isdigit())



def _clamp(value: float, minimum: float = 0.0, maximum: float = 100.0) -> float:
    return max(minimum, min(maximum, value))


def _confidence_band(score: float) -> str:
    if score >= 75:
        return "high"
    if score >= 50:
        return "medium"
    return "low"


def _safety_band(score: float, has_vendor_data: bool = True) -> str:
    if not has_vendor_data:
        return "unknown"
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
    created = _as_utc(created_at)
    age_days = max(((_now_utc() - created).total_seconds() / 86400.0), 0.0)
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
    if sighting.vendor_email:
        score += 45
    if sighting.vendor_phone:
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
    """Compute vendor safety score, flags, and summary.

    Uses VendorCard enrichment data to surface identity/trust signals per the
    vendor safety model spec. Uses caution language — signals, not accusations.
    """
    score = 50.0
    flags: list[str] = []

    if vendor_card:
        score += 10  # baseline bump for having internal profile

        # Unified vendor score integration
        if getattr(vendor_card, "vendor_score", None) is not None:
            score += (float(vendor_card.vendor_score) - 50.0) * 0.25

        # Blacklist / do-not-contact
        if getattr(vendor_card, "is_blacklisted", False):
            score -= 45
            flags.append("internal_do_not_contact_history")

        # Business footprint signals
        has_website = bool(getattr(vendor_card, "website", None))
        has_domain = bool(getattr(vendor_card, "domain", None))
        has_address = bool(getattr(vendor_card, "hq_city", None) or getattr(vendor_card, "hq_country", None))
        has_legal = bool(getattr(vendor_card, "legal_name", None))

        if not has_website and not has_domain:
            score -= 10
            flags.append("no_business_footprint")
        elif not has_website:
            score -= 5
            flags.append("limited_business_footprint")

        if not has_address:
            score -= 5
            flags.append("unverifiable_address")

        # Contact verification signals
        has_emails = bool(getattr(vendor_card, "emails", None))
        has_phones = bool(getattr(vendor_card, "phones", None))
        if not has_emails and not has_phones:
            score -= 8
            flags.append("conflicting_contact_info")

        # Engagement history signals
        if getattr(vendor_card, "is_new_vendor", True) and not getattr(vendor_card, "sighting_count", 0):
            score -= 5
            flags.append("new_domain")

        ghost_rate = getattr(vendor_card, "ghost_rate", None)
        if ghost_rate is not None and ghost_rate > 0.5:
            score -= 10
            flags.append("repeated_bad_feedback")

        cancel_rate = getattr(vendor_card, "cancellation_rate", None)
        if cancel_rate is not None and cancel_rate > 0.2:
            score -= 8
            flags.append("high_cancellation_rate")

        # Positive signals — boost score and record as positive: prefixed flags
        if has_legal and has_address and has_website:
            score += 8
            flags.append("positive:verified_business_footprint")
        if has_website and has_domain:
            flags.append("positive:business_website_exists")
        if has_emails and has_domain and has_website:
            domain = getattr(vendor_card, "domain", "") or ""
            email_list = getattr(vendor_card, "emails", []) or []
            if email_list and domain and any(domain in (e or "") for e in email_list):
                flags.append("positive:email_domain_matches_website")
        if has_emails or has_phones:
            flags.append("positive:contact_channels_present")
        if getattr(vendor_card, "relationship_months", None) and vendor_card.relationship_months >= 6:
            score += 5
            flags.append("positive:established_relationship")
        if getattr(vendor_card, "total_wins", 0) and vendor_card.total_wins >= 3:
            score += 5
            flags.append("positive:proven_success_history")
        if getattr(vendor_card, "sighting_count", 0) and vendor_card.sighting_count >= 5:
            flags.append("positive:marketplace_listing_found")
    else:
        flags.append("no_internal_vendor_profile")
        flags.append("marketplace_trust_unknown")

    if contactability < 40:
        score -= 10
        if "conflicting_contact_info" not in flags:
            flags.append("limited_verified_contact_channels")

    has_vendor_data = vendor_card is not None
    score = round(_clamp(score), 1)
    band = _safety_band(score, has_vendor_data=has_vendor_data)

    if band == "unknown":
        summary = "Unknown vendor: no internal history available. Verify identity and stock before outreach."
    elif band == "high_risk":
        summary = "Caution advised: multiple risk signals detected. Verify identity, contact details, and stock before proceeding."
    elif band == "medium_risk":
        summary = "Moderate caution: some signals are incomplete. Confirm business footprint and contact path before relying on inventory claims."
    else:
        summary = "Lower risk based on current data, but always verify stock and terms in outreach."
    return score, flags, summary


def _source_category(source_type: str) -> str:
    """Map a connector source_type to a handoff-spec source category.

    Per evidence.schema.yaml, source categories are:
    api, marketplace, salesforce_history, avail_history, web_ai, safety_review, buyer_feedback.
    This groups individual connectors into these categories for corroboration checks.
    """
    st = (source_type or "").lower()
    if st in {"digikey", "mouser", "element14", "farnell", "nexar", "octopart"}:
        return "api"
    if st in {"brokerbin", "sourcengine", "oemsecrets", "ebay", "netcomponents", "icsource"}:
        return "marketplace"
    if st in {"salesforce", "salesforce_history"}:
        return "salesforce_history"
    if st in {"material_history", "sighting_history", "avail_history", "vendor_affinity"}:
        return "avail_history"
    if st in {"ai", "web", "ai_live_web", "web_ai"}:
        return "web_ai"
    if st in {"email_mining"}:
        return "marketplace"  # email mining produces stock signals similar to marketplace
    if st in {"safety_review"}:
        return "safety_review"
    if st in {"buyer_feedback"}:
        return "buyer_feedback"
    return "marketplace"


def _signal_type_for_source(source_type: str) -> str:
    """Map a connector source_type to a handoff-spec signal_type.

    Per evidence.schema.yaml, signal_type describes what kind of evidence
    this is (stock listing, vendor history, vendor affinity, etc.).
    """
    st = (source_type or "").lower()
    if st in {"digikey", "mouser", "element14", "farnell", "nexar", "octopart", "brokerbin",
              "sourcengine", "oemsecrets", "ebay", "netcomponents", "icsource"}:
        return "stock_listing"
    if st in {"salesforce", "salesforce_history"}:
        return "vendor_history"
    if st in {"vendor_affinity"}:
        return "vendor_affinity"
    if st in {"material_history", "sighting_history", "avail_history"}:
        return "historical_activity"
    if st in {"ai", "web", "ai_live_web", "web_ai"}:
        return "web_discovery"
    if st in {"email_mining"}:
        return "email_signal"
    return "stock_listing"


def _reliability_band(score: float) -> str:
    """Reliability band for evidence items per evidence.schema.yaml.

    Separate from confidence_band — this rates the source itself, not the lead.
    """
    if score >= 75:
        return "high"
    if score >= 50:
        return "medium"
    return "low"


def _match_type_for_parts(requested: str, matched: str, substitutes: list | None = None) -> str:
    """Determine match_type per lead.schema.yaml enum: exact/normalized/fuzzy/cross_ref."""
    if not requested or not matched:
        return "exact"
    req_norm = normalize_mpn(requested)
    match_norm = normalize_mpn(matched)
    if req_norm == match_norm:
        return "exact"
    if req_norm and match_norm and (req_norm in match_norm or match_norm in req_norm):
        return "normalized"
    # Check if matched part is a known substitute / cross-reference
    if substitutes:
        sub_norms = set()
        for sub in substitutes:
            if isinstance(sub, str):
                sub_norms.add(normalize_mpn(sub))
            elif isinstance(sub, dict):
                sub_norms.add(normalize_mpn(sub.get("mpn") or sub.get("part_number") or ""))
        sub_norms.discard("")
        if match_norm in sub_norms:
            return "cross_ref"
    return "fuzzy"


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
    vendor_normalized = (normalize_vendor_name(vendor_name) or sighting.vendor_name_normalized or "").strip()
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
            match_type=_match_type_for_parts(requested_part, matched_part, getattr(requirement, "substitutes", None)),
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
    lead.contact_email = lead.contact_email or sighting.vendor_email
    lead.contact_phone = lead.contact_phone or sighting.vendor_phone
    lead.contact_url = lead.contact_url or ((sighting.raw_data or {}).get("website") or (sighting.raw_data or {}).get("vendor_url"))

    lead.freshness_score = freshness
    lead.source_reliability_score = source_reliability
    lead.contactability_score = contactability
    lead.historical_success_score = historical
    lead.confidence_score = confidence_score
    lead.confidence_band = confidence_band
    lead.vendor_safety_score = safety_score
    lead.vendor_safety_band = _safety_band(safety_score, has_vendor_data=vendor_card is not None)
    lead.vendor_safety_summary = safety_summary
    lead.vendor_safety_flags = safety_flags
    lead.vendor_safety_last_checked_at = _now_utc()
    lead.reason_summary = explain_lead(
        vendor_name=vendor_name,
        is_authorized=bool(sighting.is_authorized),
        vendor_score=getattr(vendor_card, "vendor_score", None) if vendor_card else None,
        unit_price=sighting.unit_price,
        qty_available=sighting.qty_available,
        target_qty=requirement.target_qty,
        has_contact=bool(lead.contact_email or lead.contact_phone),
        evidence_tier=sighting.evidence_tier,
        source_type=sighting.source_type,
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


def _auto_merge_leads(db: Session, survivor: SourcingLead, duplicate: SourcingLead) -> None:
    """Merge a duplicate lead into the survivor by moving evidence and events.

    Only merges if the duplicate has not been acted on by a buyer (status = 'new').
    Preserves all source attribution per the dedup spec guardrails.
    """
    if duplicate.buyer_status != "new":
        # Buyer has already acted on this lead — flag instead of merging
        _add_risk_flag(survivor, "duplicate_candidate")
        _add_risk_flag(duplicate, "duplicate_candidate")
        return

    # Move evidence rows from duplicate to survivor
    db.query(LeadEvidence).filter(LeadEvidence.lead_id == duplicate.id).update(
        {"lead_id": survivor.id}
    )

    # Move feedback events from duplicate to survivor (if any)
    db.query(LeadFeedbackEvent).filter(LeadFeedbackEvent.lead_id == duplicate.id).update(
        {"lead_id": survivor.id}
    )

    # Delete the duplicate lead
    db.delete(duplicate)
    db.flush()

    # Refresh survivor rollups with the merged evidence
    _refresh_lead_evidence_rollups(db, survivor)
    logger.info(
        "Auto-merged duplicate lead {} into survivor {} for requirement {}",
        duplicate.lead_id, survivor.lead_id, survivor.requirement_id,
    )


def _count_dedup_signals(
    db: Session,
    lead: SourcingLead,
    other: SourcingLead,
    vendor_card: VendorCard | None,
) -> int:
    """Count how many dedup signals match between two leads.

    Returns count of matching signals:
    - vendor_card_id match = 2 (strong — counts as exact duplicate per spec)
    - domain match = 1
    - phone match = 1
    - email domain match = 1
    """
    signals = 0

    if vendor_card and vendor_card.id and other.vendor_card_id == vendor_card.id:
        signals += 2  # exact duplicate per spec

    if not other.vendor_card_id:
        return signals

    other_card = db.query(VendorCard).filter(VendorCard.id == other.vendor_card_id).first()
    if not other_card:
        return signals

    # Domain match
    if vendor_card and getattr(vendor_card, "domain", None):
        lead_domain = (vendor_card.domain or "").strip().lower()
        other_domain = (getattr(other_card, "domain", None) or "").strip().lower()
        if lead_domain and other_domain and lead_domain == other_domain:
            signals += 1

    # Phone match
    if vendor_card and getattr(vendor_card, "phones", None):
        lead_phones = {_normalize_phone(p) for p in (vendor_card.phones or []) if p}
        other_phones = {_normalize_phone(p) for p in (getattr(other_card, "phones", None) or []) if p}
        lead_phones.discard("")
        other_phones.discard("")
        if lead_phones & other_phones:
            signals += 1

    # Email domain match
    if vendor_card and getattr(vendor_card, "emails", None):
        lead_email_domains = {(e or "").split("@")[-1].strip().lower() for e in (vendor_card.emails or []) if e and "@" in e}
        other_email_domains = {(e or "").split("@")[-1].strip().lower() for e in (getattr(other_card, "emails", None) or []) if e and "@" in e}
        lead_email_domains.discard("")
        other_email_domains.discard("")
        if lead_email_domains & other_email_domains:
            signals += 1

    return signals


def _check_duplicate_candidates(
    db: Session,
    lead: SourcingLead,
    vendor_card: VendorCard | None,
) -> None:
    """Detect and handle duplicate leads for the same part.

    Per the handoff dedup spec:
    - Exact duplicate (vendor_card_id/domain/phone match): auto-merge
    - Strong likely duplicate (2+ medium signals agree): auto-merge
    - Possible duplicate (1 signal): flag as duplicate_candidate, no merge

    Auto-merge only happens when the weaker lead is still in 'new' status
    (buyer has not acted on it). Otherwise, flags both as duplicate_candidate.
    """
    if not lead.requirement_id:
        return

    other_leads = (
        db.query(SourcingLead)
        .filter(
            SourcingLead.requirement_id == lead.requirement_id,
            SourcingLead.part_number_matched == lead.part_number_matched,
            SourcingLead.id != lead.id,
        )
        .all()
    )
    if not other_leads:
        return

    for other in other_leads:
        signals = _count_dedup_signals(db, lead, other, vendor_card)

        if signals >= 2:
            # Exact or strong likely duplicate — auto-merge
            # Survivor = lead with higher confidence or more evidence
            if (lead.confidence_score or 0) >= (other.confidence_score or 0):
                _auto_merge_leads(db, lead, other)
            else:
                _auto_merge_leads(db, other, lead)
            return
        elif signals == 1:
            # Possible duplicate — flag only
            _add_risk_flag(lead, "duplicate_candidate")
            _add_risk_flag(other, "duplicate_candidate")


def _add_risk_flag(lead: SourcingLead, flag: str) -> None:
    """Add a risk flag to a lead if not already present."""
    existing = lead.risk_flags or []
    if flag not in existing:
        lead.risk_flags = sorted(set(existing + [flag]))


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
        observed = _as_utc(sighting.created_at)
        freshness_days = max(((_now_utc() - observed).total_seconds() / 86400.0), 0.0)

    src_type = sighting.source_type or "unknown"
    src_reliability = _source_reliability(src_type, sighting.evidence_tier)

    evidence = LeadEvidence(
        evidence_id=f"ev_{uuid.uuid4().hex[:24]}",
        lead_id=lead.id,
        signal_type=_signal_type_for_source(src_type),
        source_type=src_type,
        source_name=_source_name(src_type),
        source_reference=source_ref,
        part_number_observed=(sighting.mpn_matched or sighting.mpn or ""),
        vendor_name_observed=sighting.vendor_name,
        observed_text=(sighting.raw_data or {}).get("description") or (sighting.raw_data or {}).get("evidence_note"),
        observed_at=sighting.created_at,
        freshness_age_days=freshness_days,
        weight=src_reliability,
        confidence_impact=round(src_reliability * 0.2, 1),  # this evidence's scoring contribution
        explanation=f"{_source_name(src_type)} {_signal_type_for_source(src_type).replace('_', ' ')} for {sighting.vendor_name or 'vendor'}",
        source_reliability_band=_reliability_band(src_reliability),
        verification_state="raw",
    )
    db.add(evidence)


def _refresh_lead_evidence_rollups(db: Session, lead: SourcingLead) -> None:
    evidence_rows = (
        db.query(LeadEvidence.source_type)
        .filter(LeadEvidence.lead_id == lead.id)
        .all()
    )
    evidence_count = len(evidence_rows)
    # Corroboration requires evidence from 2+ distinct source CATEGORIES
    # (e.g., api + marketplace), not just 2 different connectors within the same category
    categories = {_source_category(row.source_type) for row in evidence_rows}
    lead.evidence_count = evidence_count
    lead.corroborated = len(categories) >= 2
    if lead.corroborated and lead.confidence_score is not None:
        lead.confidence_score = _clamp(float(lead.confidence_score) + 5.0)
        lead.confidence_band = _confidence_band(float(lead.confidence_score))
        # Promote raw evidence to inferred when corroborated by multiple source categories
        db.query(LeadEvidence).filter(
            LeadEvidence.lead_id == lead.id,
            LeadEvidence.verification_state == "raw",
        ).update({"verification_state": "inferred"})


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
        vc = db.query(VendorCard).filter(VendorCard.id == lead.vendor_card_id).first() if lead.vendor_card_id else None
        _check_duplicate_candidates(db, lead, vc)
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
            row["contactability_score"] = lead.contactability_score
            row["historical_success_score"] = lead.historical_success_score
            row["corroborated"] = lead.corroborated


def get_requisition_leads(db: Session, requisition_id: int, statuses: list[str] | None = None) -> list[SourcingLead]:
    query = db.query(SourcingLead).filter(SourcingLead.requisition_id == requisition_id)
    if statuses:
        clean_statuses = [s for s in statuses if s in BUYER_STATUSES]
        if clean_statuses:
            query = query.filter(SourcingLead.buyer_status.in_(clean_statuses))
    return query.order_by(SourcingLead.confidence_score.desc(), SourcingLead.updated_at.desc()).all()


def _propagate_outcome_to_vendor(db: Session, lead: SourcingLead, status: str) -> None:
    """Propagate buyer outcome to VendorCard to improve future lead ranking.

    When a buyer confirms stock or flags a bad lead, the vendor's aggregate
    score is adjusted so future leads from the same vendor reflect real-world
    outcomes. Uses conservative increments to avoid runaway drift.
    """
    if not lead.vendor_card_id:
        return
    vendor_card = db.query(VendorCard).filter(VendorCard.id == lead.vendor_card_id).first()
    if not vendor_card:
        return

    if status == "has_stock":
        vendor_card.total_wins = (vendor_card.total_wins or 0) + 1
        if vendor_card.vendor_score is not None:
            vendor_card.vendor_score = min(100.0, vendor_card.vendor_score + 2.0)
    elif status == "bad_lead":
        if vendor_card.vendor_score is not None:
            vendor_card.vendor_score = max(0.0, vendor_card.vendor_score - 3.0)
    elif status == "do_not_contact":
        vendor_card.is_blacklisted = True
        if vendor_card.vendor_score is not None:
            vendor_card.vendor_score = max(0.0, vendor_card.vendor_score - 10.0)


def _update_evidence_verification_state(db: Session, lead_id: int, buyer_status: str) -> None:
    """Transition evidence verification_state based on buyer outcome.

    Per evidence.schema.yaml: raw → buyer_confirmed (has_stock),
    raw → rejected (bad_lead, do_not_contact).
    """
    target_state = None
    if buyer_status == "has_stock":
        target_state = "buyer_confirmed"
    elif buyer_status in ("bad_lead", "do_not_contact"):
        target_state = "rejected"

    if target_state:
        db.query(LeadEvidence).filter(
            LeadEvidence.lead_id == lead_id,
            LeadEvidence.verification_state == "raw",
        ).update({"verification_state": target_state})


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
        has_vendor_data = lead.vendor_card_id is not None
        lead.vendor_safety_band = _safety_band(float(lead.vendor_safety_score), has_vendor_data=has_vendor_data)

    # Update evidence verification_state based on buyer outcome
    _update_evidence_verification_state(db, lead.id, status)

    # Propagate buyer outcome to VendorCard for feedback loop
    _propagate_outcome_to_vendor(db, lead, status)

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
