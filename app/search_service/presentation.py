"""Search package — result-shaping for display: sighting/affinity dicts, the raw
streaming-hit scorer, and vendor-card HTML rendering for SSE.

W4.5a split of app/search_service.py — pure structural move (see cache.py header).
"""

from datetime import UTC, datetime

from ..models import Sighting
from ..scoring import classify_lead, confidence_color, explain_lead, score_sighting_v2, source_badge
from ..utils.normalization import (
    detect_currency,
    normalize_condition,
    normalize_date_code,
    normalize_lead_time,
    normalize_mpn,
    normalize_packaging,
    normalize_price,
    normalize_quantity,
)
from ..utils.normalization_helpers import fix_encoding
from ..vendor_utils import normalize_vendor_name


def _affinity_match_to_result(match: dict, mpn: str) -> dict:
    """Convert a single vendor-affinity match dict into a sighting-shaped result.

    Shared between the cached-only short-circuit path and the full search path
    in ``search_requirement`` so both surface affinity suggestions identically.
    """
    conf_pct = round(match.get("confidence", 0) * 100)
    return {
        "vendor_name": match.get("vendor_name", ""),
        "vendor_id": match.get("vendor_id"),
        "mpn": mpn,
        "mpn_matched": mpn,
        "source_type": "vendor_affinity",
        "source_badge": "Vendor Match",
        "is_historical": False,
        "is_material_history": False,
        "is_affinity": True,
        "confidence_pct": conf_pct,
        "confidence_color": confidence_color(conf_pct),
        "reasoning": match.get("reasoning", ""),
        "qty_available": None,
        "unit_price": None,
        # Spec §9: affinity rows carry confidence_pct only — no derived score
        # number (the old confidence*20 formula was a fourth scoring generation).
        "score": 0,
        "cross_references": [],
    }


def _render_search_vendor_cards_html(
    cards: list[dict],
    *,
    search_id: str,
    start_index: int = 0,
    swap_oob: bool = False,
) -> str:
    """Render vendor_card.html fragments for HTMX SSE (must be HTML, not JSON).

    Called by: stream_search_mpn (results + card-update events)
    Depends on: app.template_env.templates, htmx/partials/search/vendor_card.html
    """
    from ..template_env import templates

    tmpl = templates.get_template("htmx/partials/search/vendor_card.html")
    parts: list[str] = []
    for i, card in enumerate(cards):
        parts.append(
            tmpl.render(
                card=card,
                card_index=start_index + i,
                search_id=search_id,
                swap_oob=swap_oob,
            )
        )
    return "".join(parts)


def sighting_to_dict(s: Sighting) -> dict:
    raw: dict = s.raw_data or {}
    has_contact = bool(s.vendor_email or s.vendor_phone)
    has_price = s.unit_price is not None
    has_qty = s.qty_available is not None
    tier = getattr(s, "evidence_tier", None)
    score = s.score or 0

    age_days = None
    if s.created_at:
        ca = s.created_at.replace(tzinfo=UTC) if s.created_at.tzinfo is None else s.created_at
        age_days = (datetime.now(UTC) - ca).days

    quality = classify_lead(
        score=score,
        is_authorized=s.is_authorized,
        has_price=has_price,
        has_qty=has_qty,
        has_contact=has_contact,
        evidence_tier=tier,
    )
    explanation = explain_lead(
        vendor_name=s.vendor_name,
        is_authorized=s.is_authorized,
        vendor_score=None,
        unit_price=s.unit_price,
        qty_available=s.qty_available,
        has_contact=has_contact,
        evidence_tier=tier,
        source_type=s.source_type,
        age_days=age_days,
    )

    # Spec §9: display reads the PERSISTED v2 score — confidence_pct derives from
    # sightings.score verbatim (no display-time re-derivation; the old score_unified
    # call here recomputed with vendor_score=None, so the screen disagreed with the
    # stored value), the badge is a static source_type label.
    confidence_pct = int(round(score))

    return {
        "id": s.id,
        "requirement_id": s.requirement_id,
        "vendor_name": s.vendor_name,
        "vendor_email": s.vendor_email,
        "vendor_phone": s.vendor_phone,
        "mpn_matched": s.mpn_matched,
        "manufacturer": s.manufacturer,
        "qty_available": s.qty_available,
        "unit_price": s.unit_price,
        "currency": s.currency,
        "source_type": s.source_type,
        "is_authorized": s.is_authorized,
        "confidence": s.confidence,
        "score": score,
        "source_badge": source_badge(s.source_type),
        "confidence_pct": confidence_pct,
        "confidence_color": confidence_color(confidence_pct),
        "reasoning": None,
        "is_unavailable": getattr(s, "is_unavailable", False) or False,
        "octopart_url": raw.get("octopart_url"),
        "click_url": raw.get("click_url"),
        "vendor_url": raw.get("vendor_url"),
        "vendor_sku": raw.get("vendor_sku"),
        "condition": s.condition or raw.get("condition"),
        "country": raw.get("country"),
        "moq": s.moq,
        "date_code": s.date_code,
        "packaging": s.packaging,
        "lead_time_days": s.lead_time_days,
        "lead_time": s.lead_time,
        "evidence_tier": tier,
        "score_components": getattr(s, "score_components", None),
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "is_stale": (age_days or 0) > 90,
        "lead_quality": quality,
        "lead_explanation": explanation,
    }


def _score_raw_hit(r: dict, vendor_score_map: dict) -> dict:
    """Normalize and score a single raw connector result for streaming search.

    Scores with score_sighting_v2 — the SAME generation _save_sightings persists —
    so a streamed card shows what the DB will store (spec §9; the old v1 trust-only
    score made screen and DB disagree). median_price is None here (rows arrive
    incrementally, no batch median), which score_sighting_v2 already handles via
    its missing-data price factor. Produces fields needed by _incremental_dedup
    and vendor card rendering.

    Called by: stream_search_mpn
    Depends on: scoring, evidence_tiers, normalization utilities
    """
    from ..evidence_tiers import tier_for_sighting

    raw_vendor = r.get("vendor_name", "Unknown")
    clean_vendor = fix_encoding((raw_vendor or "").strip()) or raw_vendor
    raw_mpn = r.get("mpn_matched")
    clean_mpn_r = normalize_mpn(raw_mpn) or raw_mpn

    clean_qty = normalize_quantity(r.get("qty_available"))
    if clean_qty is None and isinstance(r.get("qty_available"), (int, float)) and r["qty_available"] > 0:
        clean_qty = int(r["qty_available"])

    clean_price = normalize_price(r.get("unit_price"))
    if clean_price is None and isinstance(r.get("unit_price"), (int, float)) and r["unit_price"] > 0:
        clean_price = float(r["unit_price"])

    raw_currency = r.get("currency") or "USD"
    clean_currency = detect_currency(raw_currency) if raw_currency else "USD"
    raw_conf = r.get("confidence", 0) or 0
    norm_conf = raw_conf / 5.0 if raw_conf > 1 else raw_conf
    is_auth = r.get("is_authorized", False)
    norm_name = normalize_vendor_name(clean_vendor)
    tier = tier_for_sighting(r.get("source_type"), is_auth)
    raw_moq = r.get("moq")

    lead_time_days = normalize_lead_time(r.get("lead_time"))
    condition = normalize_condition(r.get("condition"))
    v2_total, v2_comp = score_sighting_v2(
        vendor_score=vendor_score_map.get(norm_name),
        is_authorized=is_auth,
        unit_price=clean_price,
        median_price=None,
        qty_available=clean_qty,
        target_qty=None,
        age_hours=r.get("_source_age_hours", 0.0),
        has_price=clean_price is not None,
        has_qty=clean_qty is not None,
        has_lead_time=lead_time_days is not None,
        has_condition=condition is not None,
    )
    confidence_pct = int(round(v2_total))

    return {
        "vendor_name": clean_vendor,
        "vendor_email": r.get("vendor_email"),
        "vendor_phone": r.get("vendor_phone"),
        "mpn_matched": clean_mpn_r,
        "manufacturer": r.get("manufacturer"),
        "qty_available": clean_qty,
        "unit_price": clean_price,
        "currency": clean_currency,
        "source_type": r.get("source_type"),
        "is_authorized": is_auth,
        "confidence": norm_conf,
        "score": v2_total,
        "score_components": v2_comp,
        "confidence_pct": confidence_pct,
        "confidence_color": confidence_color(confidence_pct),
        "source_badge": source_badge(r.get("source_type")),
        "evidence_tier": tier,
        "octopart_url": r.get("octopart_url"),
        "click_url": r.get("click_url"),
        "vendor_url": r.get("vendor_url"),
        "vendor_sku": r.get("vendor_sku"),
        "condition": condition,
        "moq": raw_moq if raw_moq and raw_moq > 0 else None,
        "date_code": normalize_date_code(r.get("date_code")),
        "packaging": normalize_packaging(r.get("packaging")),
        "lead_time_days": lead_time_days,
        "lead_time": r.get("lead_time"),
        "country": r.get("country"),
        "lead_quality": classify_lead(
            score=v2_total,
            is_authorized=is_auth,
            has_price=clean_price is not None,
            has_qty=clean_qty is not None,
            has_contact=bool(r.get("vendor_email") or r.get("vendor_phone")),
            evidence_tier=tier,
        ),
    }
