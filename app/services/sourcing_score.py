"""Sourcing Activity Score — holistic effort indicator per requirement/requisition.

Computes a 0–100 score reflecting how thoroughly a requirement has been worked.
Signals: sightings, RFQs sent, vendor replies, offers, phone calls, emails.

Color bands:
  Red   0–25  (barely touched)
  Yellow 25–60 (solid work in progress)
  Green  60+   (exceptional effort — nearly unachievable)

Per-requirement scores average up to a consolidated requisition score.
All team effort counts (not per-buyer).

Called by: routers/requisitions.py
Depends on: models (Requirement, Sighting, Offer, Contact, VendorResponse, ActivityLog)
"""

from __future__ import annotations

import math

from sqlalchemy import func as sqlfunc
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ActivityLog,
    Contact,
    Offer,
    Requirement,
    Sighting,
    VendorResponse,
)


def _sigmoid(x: float, midpoint: float, steepness: float = 1.0) -> float:
    """Smooth 0–1 curve. Returns ~0.5 at midpoint, asymptotes at 0 and 1."""
    return 1 / (1 + math.exp(-steepness * (x - midpoint)))


def score_requirement(
    sighting_count: int,
    offer_count: int,
    rfqs_per_part: float,
    reply_rate: float,
    calls_per_part: float,
    emails_per_part: float,
) -> float:
    """Compute a single requirement's sourcing effort score (0–100).

    Each signal is normalized to 0–1 via sigmoid curves, then combined
    with equal-ish weighting into a holistic composite.

    Green (60+) is attainable with thorough, multi-channel effort — RFQs sent,
    replies received, calls made, and offers in hand. It represents "we worked
    this part well and can look the customer in the eye."
    """
    # Sightings: found sources at all? More = better, diminishing returns.
    # midpoint=2 means 2 sightings = 50% credit on this factor
    s_sightings = _sigmoid(sighting_count, midpoint=2, steepness=1.0)

    # Offers: having real offers in hand (midpoint=1)
    s_offers = _sigmoid(offer_count, midpoint=1, steepness=1.5)

    # RFQs sent per part: outreach effort (midpoint=1.5 RFQs per part)
    s_rfqs = _sigmoid(rfqs_per_part, midpoint=1.5, steepness=1.2)

    # Reply rate: vendor engagement (0–1 input; midpoint=0.3 = 30% reply rate)
    s_replies = _sigmoid(reply_rate * 5, midpoint=1.5, steepness=1.0)

    # Phone calls: incentivize picking up the phone (midpoint=0.3 per part)
    s_calls = _sigmoid(calls_per_part, midpoint=0.3, steepness=3.0)

    # Email exchanges: back-and-forth with vendors (midpoint=0.5 per part)
    s_emails = _sigmoid(emails_per_part, midpoint=0.5, steepness=2.0)

    # Weighted combination — all signals matter roughly equally
    # but calls get a slight boost to incentivize phone usage
    raw = (
        s_sightings * 0.15
        + s_offers * 0.15
        + s_rfqs * 0.20
        + s_replies * 0.20
        + s_calls * 0.15
        + s_emails * 0.15
    )

    # Scale to 0–100.
    score = raw * 100

    return round(min(score, 100), 1)


def compute_requisition_scores(requisition_id: int, db: Session) -> dict:
    """Compute per-requirement scores and consolidated requisition score.

    Returns:
        {
            "requisition_score": float,       # averaged from requirements
            "requisition_color": "red"|"yellow"|"green",
            "requirements": [
                {"requirement_id": int, "mpn": str, "score": float, "color": str},
                ...
            ]
        }
    """
    # Fetch all requirements for this requisition
    requirements = (
        db.query(Requirement)
        .filter(Requirement.requisition_id == requisition_id)
        .all()
    )
    if not requirements:
        return {
            "requisition_score": 0,
            "requisition_color": "red",
            "requirements": [],
        }

    req_ids = [r.id for r in requirements]
    num_parts = len(req_ids)

    # Batch-fetch per-requirement counts
    sighting_counts = dict(
        db.query(Sighting.requirement_id, sqlfunc.count(Sighting.id))
        .filter(Sighting.requirement_id.in_(req_ids))
        .group_by(Sighting.requirement_id)
        .all()
    )
    offer_counts = dict(
        db.query(Offer.requirement_id, sqlfunc.count(Offer.id))
        .filter(Offer.requirement_id.in_(req_ids))
        .group_by(Offer.requirement_id)
        .all()
    )

    # Requisition-level signals (shared across all parts)
    rfq_sent = (
        db.query(sqlfunc.count(Contact.id))
        .filter(Contact.requisition_id == requisition_id, Contact.status == "sent")
        .scalar()
        or 0
    )
    reply_count = (
        db.query(sqlfunc.count(VendorResponse.id))
        .filter(VendorResponse.requisition_id == requisition_id)
        .scalar()
        or 0
    )
    call_count = (
        db.query(sqlfunc.count(ActivityLog.id))
        .filter(
            ActivityLog.requisition_id == requisition_id,
            ActivityLog.channel == "phone",
        )
        .scalar()
        or 0
    )
    email_count = (
        db.query(sqlfunc.count(ActivityLog.id))
        .filter(
            ActivityLog.requisition_id == requisition_id,
            ActivityLog.channel == "email",
        )
        .scalar()
        or 0
    )

    # Shared ratios
    rfqs_per_part = rfq_sent / num_parts if num_parts else 0
    reply_rate = reply_count / rfq_sent if rfq_sent else 0
    calls_per_part = call_count / num_parts if num_parts else 0
    emails_per_part = email_count / num_parts if num_parts else 0

    # Score each requirement
    req_scores = []
    for r in requirements:
        sc = score_requirement(
            sighting_count=sighting_counts.get(r.id, 0),
            offer_count=offer_counts.get(r.id, 0),
            rfqs_per_part=rfqs_per_part,
            reply_rate=reply_rate,
            calls_per_part=calls_per_part,
            emails_per_part=emails_per_part,
        )
        req_scores.append(
            {
                "requirement_id": r.id,
                "mpn": r.primary_mpn or "",
                "score": sc,
                "color": _color(sc),
            }
        )

    # Consolidated = average of all requirement scores
    avg = sum(r["score"] for r in req_scores) / len(req_scores) if req_scores else 0
    avg = round(avg, 1)

    return {
        "requisition_score": avg,
        "requisition_color": _color(avg),
        "requirements": req_scores,
    }


def compute_requisition_score_fast(
    req_count: int,
    sourced_count: int,
    rfq_sent_count: int,
    reply_count: int,
    offer_count: int,
    call_count: int = 0,
    email_count: int = 0,
) -> tuple[float, str]:
    """Lightweight score for list views — no per-requirement breakdown.

    Uses requisition-level aggregates to estimate the score without
    querying individual requirements. Good enough for the row indicator.

    Returns: (score, color)
    """
    if req_count == 0:
        return (0, "red")

    # Approximate per-part values from aggregates
    sourced_ratio = sourced_count / req_count
    rfqs_per_part = rfq_sent_count / req_count
    reply_rate = reply_count / rfq_sent_count if rfq_sent_count else 0
    offers_per_part = offer_count / req_count
    calls_per_part = call_count / req_count
    emails_per_part = email_count / req_count

    # Use same scoring function with averaged signals
    sc = score_requirement(
        sighting_count=int(sourced_ratio * 5),  # approx: sourced_ratio maps to ~sighting density
        offer_count=int(offers_per_part * req_count) if offers_per_part > 0 else 0,
        rfqs_per_part=rfqs_per_part,
        reply_rate=reply_rate,
        calls_per_part=calls_per_part,
        emails_per_part=emails_per_part,
    )
    return (sc, _color(sc))


def _color(score: float) -> str:
    if score >= 60:
        return "green"
    if score >= 25:
        return "yellow"
    return "red"
