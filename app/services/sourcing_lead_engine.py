"""Sourcing lead projection service.

Builds buyer-facing lead cards from requirement search results while keeping
the existing sighting persistence model unchanged.

What this does:
- Collapses multiple result rows into one lead per vendor per part.
- Preserves source attribution as evidence items.
- Separates stock confidence from vendor safety/trust scoring.
- Maps operational workflow signals into buyer-oriented lead statuses.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models import Contact, Requirement, VendorCard, VendorResponse
from app.utils.normalization import normalize_mpn_key
from app.vendor_utils import normalize_vendor_name

_LIVE_SOURCES = {
    "nexar",
    "octopart",
    "brokerbin",
    "digikey",
    "mouser",
    "element14",
    "oemsecrets",
    "sourcengine",
    "ebay",
    "netcomponents",
    "icsource",
}
_HISTORICAL_SOURCES = {"material_history", "stock_list", "excess_list", "historical"}
_AFFINITY_SOURCES = {"vendor_affinity"}
_AI_SOURCES = {"ai_live_web"}


def _safe_parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            if value.endswith("Z"):
                value = value[:-1] + "+00:00"
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _pct_band(pct: int) -> str:
    if pct >= 75:
        return "high"
    if pct >= 50:
        return "medium"
    return "low"


def _source_label(source_type: str) -> str:
    src = (source_type or "").lower()
    if src in _LIVE_SOURCES:
        return "Live Stock"
    if src in _HISTORICAL_SOURCES:
        return "Historical"
    if src in _AFFINITY_SOURCES:
        return "Vendor Match"
    if src in _AI_SOURCES:
        return "AI Found"
    return (source_type or "Unknown").replace("_", " ").title()


def _evidence_signal_type(source_type: str) -> str:
    src = (source_type or "").lower()
    if src in _LIVE_SOURCES:
        return "live_stock_signal"
    if src in _HISTORICAL_SOURCES:
        return "historical_signal"
    if src in _AFFINITY_SOURCES:
        return "affinity_signal"
    if src in _AI_SOURCES:
        return "ai_web_signal"
    return "market_signal"


def _verification_state(sighting: dict[str, Any], age_days: int | None) -> str:
    src = (sighting.get("source_type") or "").lower()
    has_qty = sighting.get("qty_available") is not None and sighting.get("qty_available") != 0
    has_price = sighting.get("unit_price") is not None
    if src in _LIVE_SOURCES and has_qty and has_price:
        return "verified_signal"
    if src in _HISTORICAL_SOURCES:
        return "historical_reference" if (age_days is None or age_days <= 180) else "stale_reference"
    if src in _AFFINITY_SOURCES or src in _AI_SOURCES:
        return "needs_verification"
    return "unverified_signal"


def _build_evidence_items(
    requirement: Requirement,
    sightings: list[dict[str, Any]],
    now: datetime,
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for s in sightings:
        created_at = _safe_parse_dt(s.get("created_at"))
        age_days = None
        if created_at:
            age_days = max(0, (now - created_at).days)
        src = s.get("source_type") or "unknown"
        evidence.append(
            {
                "signal_type": _evidence_signal_type(src),
                "source_type": src,
                "source_name": _source_label(src),
                "source_ref": s.get("vendor_url")
                or s.get("click_url")
                or s.get("octopart_url")
                or (f"sighting:{s.get('id')}" if s.get("id") else None),
                "observed_values": {
                    "requested_mpn": requirement.primary_mpn,
                    "matched_mpn": s.get("mpn_matched") or requirement.primary_mpn,
                    "qty_available": s.get("qty_available"),
                    "unit_price": s.get("unit_price"),
                    "currency": s.get("currency"),
                    "lead_time": s.get("lead_time"),
                    "condition": s.get("condition"),
                    "vendor_email": s.get("vendor_email"),
                    "vendor_phone": s.get("vendor_phone"),
                },
                "freshness": {
                    "observed_at": created_at.isoformat() if created_at else None,
                    "age_days": age_days,
                },
                "scoring_contribution": {
                    "confidence_pct": int(s.get("confidence_pct") or 0),
                    "score": float(s.get("score") or 0),
                    "evidence_tier": s.get("evidence_tier"),
                },
                "explanation": s.get("reasoning") or s.get("lead_explanation") or "",
                "verification_state": _verification_state(s, age_days),
            }
        )
    evidence.sort(
        key=lambda e: (
            e["scoring_contribution"].get("confidence_pct", 0),
            -(e["freshness"].get("age_days") or 10_000),
        ),
        reverse=True,
    )
    return evidence


def _compute_stock_confidence(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    if not evidence:
        return {"pct": 0, "band": "low"}
    confs = [int(e["scoring_contribution"].get("confidence_pct", 0) or 0) for e in evidence]
    base = max(confs)
    corroborating = sum(1 for c in confs if c >= 40)
    corroboration_bonus = min(12, max(0, corroborating - 1) * 4)
    ages = [e["freshness"].get("age_days") for e in evidence if e["freshness"].get("age_days") is not None]
    freshness_adj = 0
    if ages:
        newest = min(ages)
        if newest <= 7:
            freshness_adj += 4
        elif newest >= 180:
            freshness_adj -= 8
    pct = max(0, min(99, base + corroboration_bonus + freshness_adj))
    return {
        "pct": int(pct),
        "band": _pct_band(int(pct)),
        "components": {
            "base_confidence": base,
            "corroboration_bonus": corroboration_bonus,
            "freshness_adjustment": freshness_adj,
        },
    }


def _compute_vendor_safety(
    vendor_card: VendorCard | None,
    evidence: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str], str]:
    score = 50.0
    risk_flags: list[str] = []
    notes: list[str] = []

    if vendor_card is None:
        score -= 5
        risk_flags.append("limited_internal_vendor_history")
        notes.append("No internal vendor profile found")
    else:
        if vendor_card.vendor_score is not None:
            score += (float(vendor_card.vendor_score) - 50.0) * 0.6
            notes.append(f"Internal vendor score {round(float(vendor_card.vendor_score), 1)}")
        if vendor_card.is_blacklisted:
            score -= 45
            risk_flags.append("do_not_contact_flag")
            notes.append("Vendor is marked as do-not-contact internally")
        if vendor_card.domain:
            score += 5
        else:
            risk_flags.append("missing_company_domain")
        if vendor_card.emails:
            score += 5
        else:
            risk_flags.append("missing_company_email")
        if vendor_card.phones:
            score += 3
        else:
            risk_flags.append("missing_company_phone")

    source_emails = {
        e["observed_values"].get("vendor_email") for e in evidence if e["observed_values"].get("vendor_email")
    }
    source_phones = {
        e["observed_values"].get("vendor_phone") for e in evidence if e["observed_values"].get("vendor_phone")
    }
    if source_emails or source_phones:
        score += 4
    else:
        risk_flags.append("no_contact_details_in_signals")

    source_types = {(e.get("source_type") or "").lower() for e in evidence}
    if source_types and source_types.issubset(_AFFINITY_SOURCES | _AI_SOURCES):
        score -= 8
        risk_flags.append("unverified_source_mix")
        notes.append("Signals are suggestions/web findings and need direct verification")

    ages = [e["freshness"].get("age_days") for e in evidence if e["freshness"].get("age_days") is not None]
    if ages and min(ages) > 180:
        score -= 12
        risk_flags.append("all_signals_stale")
        notes.append("All available signals are older than 6 months")

    score = max(0, min(100, int(round(score))))
    caution_summary = (
        "Use caution: " + "; ".join(notes[:3])
        if notes
        else "Use caution: limited safety evidence, validate vendor identity before commitment"
    )
    return {"pct": score, "band": _pct_band(score)}, sorted(set(risk_flags)), caution_summary


def _status_from_signals(
    stock_confidence_pct: int,
    risk_flags: list[str],
    sightings: list[dict[str, Any]],
    contact_count: int,
    latest_response_classification: str | None,
) -> str:
    if "do_not_contact_flag" in risk_flags:
        return "Do Not Contact"

    outcomes = {str(s.get("buyer_outcome") or "").lower() for s in sightings if s.get("buyer_outcome")}
    if "offer_logged" in outcomes:
        return "Has Stock"
    if "unavailable_confirmed" in outcomes:
        return "No Stock"

    cls = (latest_response_classification or "").lower()
    if cls in {"no_stock", "declined"}:
        return "No Stock"
    if cls:
        return "Replied"
    if contact_count > 0:
        return "Contacted"
    if stock_confidence_pct < 35 and all((s.get("qty_available") in (None, 0)) for s in sightings):
        return "Bad Lead"
    return "New"


def _suggested_next_action(status: str, risk_flags: list[str]) -> str:
    if status == "Do Not Contact":
        return "Keep blocked and escalate only with manager review."
    if status == "Has Stock":
        return "Log/confirm offer details and move to quote preparation."
    if status == "No Stock":
        return "Pivot to substitutes and alternate vendors."
    if status == "Replied":
        return "Review vendor reply and convert valid details into an offer."
    if status == "Contacted":
        return "Follow up on the open RFQ and request qty/price confirmation."
    if status == "Bad Lead":
        return "Deprioritize this lead unless corroborated by stronger sources."
    if "unverified_source_mix" in risk_flags:
        return "Verify identity and stock directly by email or phone before acting."
    return "Send RFQ with exact qty, target price, and required condition/date code."


def build_requirement_lead_cards(
    requirement: Requirement,
    sightings: list[dict[str, Any]],
    db: Session,
) -> list[dict[str, Any]]:
    """Build one lead card per vendor per part for a requirement."""
    if not sightings:
        return []

    now = datetime.now(timezone.utc)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for s in sightings:
        vendor_name = (s.get("vendor_name") or "").strip()
        if not vendor_name:
            continue
        vendor_norm = normalize_vendor_name(vendor_name) or vendor_name.lower()
        mpn_key = normalize_mpn_key(s.get("mpn_matched") or requirement.primary_mpn or "")
        if not mpn_key:
            continue
        grouped.setdefault((vendor_norm, mpn_key), []).append(s)

    if not grouped:
        return []

    vendor_norms = sorted({k[0] for k in grouped})
    vendor_cards = db.query(VendorCard).filter(VendorCard.normalized_name.in_(vendor_norms)).all()
    cards_by_norm = {v.normalized_name: v for v in vendor_cards}

    req_contacts = db.query(Contact).filter(Contact.requisition_id == requirement.requisition_id).all()
    contacts_by_vendor: dict[str, list[Contact]] = {}
    for c in req_contacts:
        key = normalize_vendor_name(c.vendor_name_normalized or c.vendor_name or "")
        if key:
            contacts_by_vendor.setdefault(key, []).append(c)

    req_responses = db.query(VendorResponse).filter(VendorResponse.requisition_id == requirement.requisition_id).all()
    responses_by_vendor: dict[str, list[VendorResponse]] = {}
    for r in req_responses:
        key = normalize_vendor_name(r.vendor_name or "")
        if key:
            responses_by_vendor.setdefault(key, []).append(r)
    for values in responses_by_vendor.values():
        values.sort(
            key=lambda x: x.received_at or x.created_at or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )

    lead_cards: list[dict[str, Any]] = []
    for (vendor_norm, mpn_key), group in grouped.items():
        group_sorted = sorted(
            group,
            key=lambda s: (int(s.get("confidence_pct") or 0), float(s.get("score") or 0.0)),
            reverse=True,
        )
        representative = group_sorted[0]
        evidence = _build_evidence_items(requirement, group_sorted, now)
        stock_conf = _compute_stock_confidence(evidence)

        vendor_card = cards_by_norm.get(vendor_norm)
        safety, risk_flags, caution_summary = _compute_vendor_safety(vendor_card, evidence)

        vendor_contacts = contacts_by_vendor.get(vendor_norm, [])
        vendor_responses = responses_by_vendor.get(vendor_norm, [])
        latest_response_cls = vendor_responses[0].classification if vendor_responses else None
        buyer_status = _status_from_signals(
            stock_confidence_pct=stock_conf["pct"],
            risk_flags=risk_flags,
            sightings=group_sorted,
            contact_count=len(vendor_contacts),
            latest_response_classification=latest_response_cls,
        )

        action = _suggested_next_action(buyer_status, risk_flags)
        source_types = sorted({(s.get("source_type") or "unknown") for s in group_sorted})
        reasons = [e["explanation"] for e in evidence if e["explanation"]]
        reason_summary = (
            reasons[0]
            if reasons
            else (f"{_source_label(representative.get('source_type') or 'unknown')} signal found for this vendor.")
        )
        if len(source_types) > 1:
            reason_summary = f"{reason_summary} Corroborated by {len(source_types)} source types."

        created_times = [
            _safe_parse_dt(s.get("created_at")) for s in group_sorted if _safe_parse_dt(s.get("created_at")) is not None
        ]
        first_seen = min(created_times).isoformat() if created_times else None
        last_seen = max(created_times).isoformat() if created_times else None

        sighting_emails = sorted({s.get("vendor_email") for s in group_sorted if s.get("vendor_email")})
        sighting_phones = sorted({s.get("vendor_phone") for s in group_sorted if s.get("vendor_phone")})
        card_emails = list(vendor_card.emails or []) if vendor_card else []
        card_phones = list(vendor_card.phones or []) if vendor_card else []
        emails = sorted({*card_emails, *sighting_emails})
        phones = sorted({*card_phones, *sighting_phones})

        lead_cards.append(
            {
                "vendor_name": representative.get("vendor_name") or (vendor_card.display_name if vendor_card else ""),
                "vendor_name_normalized": vendor_norm,
                "vendor_card_id": vendor_card.id if vendor_card else None,
                "part_requested": requirement.primary_mpn,
                "part_matched": representative.get("mpn_matched") or requirement.primary_mpn,
                "source_attribution": source_types,
                "lead_confidence_pct": stock_conf["pct"],
                "lead_confidence_band": stock_conf["band"],
                "vendor_safety_pct": safety["pct"],
                "vendor_safety_band": safety["band"],
                "reason_summary": reason_summary,
                "risk_flags": risk_flags,
                "safety_summary": caution_summary,
                "contact": {
                    "emails": emails,
                    "phones": phones,
                    "website": vendor_card.website if vendor_card else None,
                    "domain": vendor_card.domain if vendor_card else None,
                },
                "suggested_next_action": action,
                "buyer_status": buyer_status,
                "timestamps": {
                    "first_seen_at": first_seen,
                    "last_seen_at": last_seen,
                    "updated_at": now.isoformat(),
                },
                "feedback_outcome_history": {
                    "buyer_outcomes": sorted({s.get("buyer_outcome") for s in group_sorted if s.get("buyer_outcome")}),
                    "contact_count": len(vendor_contacts),
                    "response_count": len(vendor_responses),
                    "latest_response_classification": latest_response_cls,
                },
                "evidence": evidence,
            }
        )

    lead_cards.sort(
        key=lambda lead: (
            int(lead.get("lead_confidence_pct") or 0),
            int(lead.get("vendor_safety_pct") or 0),
            lead.get("vendor_name") or "",
        ),
        reverse=True,
    )
    return lead_cards
