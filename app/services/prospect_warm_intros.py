"""Warm intro detection — cross-reference prospects against email/contact history.

Checks if Trio has interacted with a prospect's domain before via:
1. VendorCard domain match (email mining / sightings)
2. SiteContact emails matching the prospect domain
3. EmailSignatureExtract company matches
4. Sighting vendor emails from the domain

Returns warm intro data and generates a "why this account" one-liner.

Called by: prospect_suggested router (serialization), prospect_signals (enrichment)
Depends on: models (VendorCard, SiteContact, EmailSignatureExtract, Sighting)
"""

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Company, VendorCard, VendorContact
from app.models.crm import CustomerSite, SiteContact
from app.models.prospect_account import ProspectAccount


def detect_warm_intros(prospect: ProspectAccount, db: Session) -> dict:
    """Check if Trio has prior interactions with a prospect's domain.

    Returns:
        {
            "has_warm_intro": bool,
            "warmth": "hot" | "warm" | "cold",
            "vendor_card_id": int | None,
            "contacts": [{name, email, title, relationship_score}],
            "engagement_score": float | None,
            "last_contact_at": str | None,
            "internal_contacts": [{name, email, company}],
            "sighting_count": int,
        }
    """
    domain = (prospect.domain or "").strip().lower()
    if not domain:
        return {"has_warm_intro": False, "warmth": "cold"}

    result = {
        "has_warm_intro": False,
        "warmth": "cold",
        "vendor_card_id": None,
        "contacts": [],
        "engagement_score": None,
        "last_contact_at": None,
        "internal_contacts": [],
        "sighting_count": 0,
    }

    # 1. VendorCard domain match
    vc = db.query(VendorCard).filter(VendorCard.domain == domain).first()
    if vc:
        result["vendor_card_id"] = vc.id
        result["engagement_score"] = vc.engagement_score
        if vc.last_contact_at:
            result["last_contact_at"] = vc.last_contact_at.isoformat()

        # Get vendor contacts with relationship scores
        contacts = (
            db.query(VendorContact)
            .filter(VendorContact.vendor_card_id == vc.id)
            .order_by(VendorContact.relationship_score.desc().nullsfirst())
            .limit(5)
            .all()
        )
        for c in contacts:
            result["contacts"].append(
                {
                    "name": c.full_name,
                    "email": c.email,
                    "title": c.title,
                    "relationship_score": c.relationship_score,
                    "activity_trend": c.activity_trend,
                }
            )

        # Determine warmth from engagement
        eng = vc.engagement_score or 0
        if eng >= 60 or (contacts and any((c.relationship_score or 0) >= 60 for c in contacts)):
            result["warmth"] = "hot"
            result["has_warm_intro"] = True
        elif eng >= 30 or contacts:
            result["warmth"] = "warm"
            result["has_warm_intro"] = True

    # 2. SiteContact emails from this domain
    domain_pattern = f"%@{domain}"
    internal = (
        db.query(
            SiteContact.full_name,
            SiteContact.email,
            Company.name.label("company_name"),
        )
        .join(CustomerSite, CustomerSite.id == SiteContact.customer_site_id)
        .join(Company, Company.id == CustomerSite.company_id)
        .filter(SiteContact.email.ilike(domain_pattern))
        .limit(5)
        .all()
    )
    for row in internal:
        result["internal_contacts"].append(
            {
                "name": row.full_name,
                "email": row.email,
                "company": row.company_name,
            }
        )
        if not result["has_warm_intro"]:
            result["has_warm_intro"] = True
            result["warmth"] = "warm"

    # 3. Sighting count from this domain
    try:
        from app.models.sourcing import Sighting

        sighting_count = (
            db.query(func.count(Sighting.id)).filter(Sighting.vendor_email.ilike(domain_pattern)).scalar() or 0
        )
        result["sighting_count"] = sighting_count
        if sighting_count > 0 and not result["has_warm_intro"]:
            result["has_warm_intro"] = True
            result["warmth"] = "warm"
    except Exception as e:
        logger.debug("Warm intro sighting lookup failed: %s", e)

    return result


def generate_one_liner(prospect: ProspectAccount, warm_intro: dict | None = None) -> str:
    """Generate a short one-liner reason for why this account is interesting.

    Priority order:
    1. Warm intro (prior Trio relationship)
    2. Historical context (bought/quoted before)
    3. Strong intent signal
    4. Recent company event (funding, expansion)
    5. Hiring signal
    6. Similar to existing customer
    7. Industry/size match fallback
    """
    signals = prospect.readiness_signals or {}
    similar = prospect.similar_customers or []
    historical = prospect.historical_context or {}

    # 1. Warm intro
    if warm_intro and warm_intro.get("has_warm_intro"):
        if warm_intro.get("warmth") == "hot":
            contacts = warm_intro.get("contacts", [])
            if contacts:
                name = contacts[0].get("name", "")
                return f"Active relationship — {name} has prior engagement with Trio"
            return "Active vendor relationship — prior email engagement with Trio"
        elif warm_intro.get("sighting_count", 0) > 0:
            return f"Received {warm_intro['sighting_count']} stock offers from this domain"
        elif warm_intro.get("internal_contacts"):
            c = warm_intro["internal_contacts"][0]
            return f"Known contact: {c['name']} at {c['company']}"
        return "Prior email interaction with this domain detected"

    # 2. Historical context (SF imports)
    if historical.get("bought_before"):
        return "Previous Trio customer — purchased before"
    if historical.get("quoted_before") or historical.get("quote_count", 0) > 0:
        count = historical.get("quote_count", 0)
        return f"Previously quoted — {count} quote{'s' if count != 1 else ''} on record"

    # 3. Strong intent signal
    intent = signals.get("intent", {})
    if isinstance(intent, dict) and intent.get("strength") == "strong":
        topics = intent.get("component_topics", [])
        if topics:
            return f"Strong buying intent — actively sourcing {topics[0]}"
        return "Strong buying intent for electronic components detected"

    # 4. Recent event
    events = signals.get("events", [])
    if isinstance(events, list) and events:
        ev = events[0] if isinstance(events[0], dict) else {}
        ev_type = ev.get("type", "").lower()
        desc = ev.get("description", "")
        if "funding" in ev_type:
            return f"Recently funded — {desc[:80]}" if desc else "New funding round announced"
        if "expansion" in ev_type or "office" in ev_type:
            return "Expanding operations — new facilities or locations"
        if "product" in ev_type or "launch" in ev_type:
            return "New product launch — likely needs components"
        if "acquisition" in ev_type or "m&a" in ev_type:
            return "Recent M&A activity — procurement consolidation likely"

    # 5. Hiring
    hiring = signals.get("hiring", {})
    if isinstance(hiring, dict) and hiring.get("type") == "procurement":
        return "Hiring procurement staff — expanding supply chain team"
    if isinstance(hiring, dict) and hiring.get("type") == "engineering":
        return "Hiring engineers — likely increasing production"

    # 6. Similar to existing customer
    if similar:
        first = similar[0] if isinstance(similar[0], dict) else {}
        name = first.get("name", "")
        if name:
            return f"Similar profile to existing customer {name}"

    # 7. Fallback — industry/size
    parts = []
    if prospect.industry:
        parts.append(prospect.industry)
    if prospect.employee_count_range:
        parts.append(f"{prospect.employee_count_range} employees")
    if prospect.fit_score and prospect.fit_score >= 70:
        parts.append(f"high ICP fit ({prospect.fit_score}/100)")
    if parts:
        return " · ".join(parts)

    return ""


def enrich_warm_intros_batch(db: Session, min_fit_score: int = 40) -> dict:
    """Run warm intro detection across qualifying suggested prospects.

    Stores results in enrichment_data JSONB and updates one-liner.

    Returns: {processed, warm_found, errors}
    """
    prospects = (
        db.query(ProspectAccount)
        .filter(
            ProspectAccount.status == "suggested",
            ProspectAccount.fit_score >= min_fit_score,
        )
        .all()
    )

    summary = {"processed": 0, "warm_found": 0, "errors": 0}

    for prospect in prospects:
        try:
            warm = detect_warm_intros(prospect, db)
            one_liner = generate_one_liner(prospect, warm)

            ed = dict(prospect.enrichment_data or {})
            ed["warm_intro"] = warm
            ed["one_liner"] = one_liner
            prospect.enrichment_data = ed

            summary["processed"] += 1
            if warm.get("has_warm_intro"):
                summary["warm_found"] += 1
        except Exception as e:
            logger.error("Warm intro error for prospect {}: {}", prospect.id, e)
            summary["errors"] += 1

    db.commit()
    logger.info("Warm intro batch complete: {}", summary)
    return summary
