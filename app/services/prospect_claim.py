"""Phase 7 — Claim workflow with deep enrichment and AI briefing.

Handles what happens when a salesperson claims a prospect:
1. Atomic claim (dual-path: SF-migrated vs new discovery)
2. Domain collision detection (prevent duplicate Companies)
3. Contact reveal (unmask emails from enrichment_data)
4. Background deep enrichment (create SiteContacts, verify emails, AI briefing)
5. Enrichment status polling
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.models import Company, User
from app.models.crm import CustomerSite, SiteContact
from app.models.prospect_account import ProspectAccount

# ── Claim ────────────────────────────────────────────────────────────


def claim_prospect(prospect_id: int, user_id: int, db: Session) -> dict:
    """Claim a prospect account — atomic, handles both paths.

    PATH A (SF-migrated, company_id set): transfer ownership of existing Company.
    PATH B (new discovery, company_id NULL): create Company, link it.
    Domain collision: if another Company with same domain exists, link to it.

    Returns: {prospect_id, company_id, company_name, status, path, warning}
    Raises: ValueError for invalid state transitions.
    """
    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        raise LookupError("Prospect not found")

    if prospect.status == "claimed":
        raise ValueError("Already claimed")

    if prospect.status not in ("suggested",):
        raise ValueError(f"Cannot claim prospect with status '{prospect.status}'")

    user = db.get(User, user_id)
    if not user:
        raise LookupError("User not found")

    warning = None
    path = None

    if prospect.company_id:
        # PATH A: SF-migrated — update existing Company
        company = db.get(Company, prospect.company_id)
        if company:
            company.account_owner_id = user_id
        path = "existing_company"
    else:
        # PATH B: New discovery — check for domain collision first
        existing = (
            db.query(Company)
            .filter(Company.domain == prospect.domain)
            .first()
        ) if prospect.domain else None

        if existing:
            # Domain collision: link to existing Company instead of creating
            existing.account_owner_id = user_id
            prospect.company_id = existing.id
            path = "domain_collision"
            warning = f"Linked to existing company '{existing.name}' (same domain)"
            logger.warning(
                "Domain collision on claim: prospect {} matched company {} ({})",
                prospect.id, existing.id, existing.domain,
            )
        else:
            # Create new Company from prospect data
            company = Company(
                name=prospect.name,
                domain=prospect.domain,
                website=prospect.website,
                industry=prospect.industry,
                hq_city=(
                    prospect.hq_location.split(",")[0].strip()
                    if prospect.hq_location and "," in prospect.hq_location
                    else None
                ),
                hq_state=(
                    prospect.hq_location.split(",")[1].strip()
                    if prospect.hq_location and "," in prospect.hq_location
                    else None
                ),
                employee_size=prospect.employee_count_range,
                is_active=True,
                account_owner_id=user_id,
                source="prospecting",
            )
            db.add(company)
            db.flush()
            prospect.company_id = company.id
            path = "new_company"

    # Update prospect status
    prospect.status = "claimed"
    prospect.claimed_by = user_id
    prospect.claimed_at = datetime.now(timezone.utc)

    # Set enrichment status to pending
    ed = dict(prospect.enrichment_data or {})
    ed["claim_enrichment_status"] = "pending"
    prospect.enrichment_data = ed

    db.commit()

    logger.info(
        "User {} claimed prospect {} ({}) via {}",
        user.name if user else user_id, prospect.name, prospect.id, path,
    )

    result = {
        "prospect_id": prospect.id,
        "company_id": prospect.company_id,
        "company_name": prospect.name,
        "status": "claimed",
        "enrichment_status": "pending",
        "path": path,
    }
    if warning:
        result["warning"] = warning
    return result


# ── Contact Reveal ───────────────────────────────────────────────────


def reveal_contacts(prospect: ProspectAccount, db: Session) -> list[dict]:
    """Unmask contacts from enrichment_data and create SiteContact records.

    Reads contacts_full from enrichment_data (stored by Phase 5 contact enrichment).
    Creates a CustomerSite + SiteContact records under the linked Company.

    Returns list of created contact dicts.
    """
    if not prospect.company_id:
        return []

    ed = prospect.enrichment_data or {}
    full_contacts = ed.get("contacts_full", [])

    if not full_contacts:
        return []

    # Create or find a CustomerSite for this company (HQ site)
    site = (
        db.query(CustomerSite)
        .filter(CustomerSite.company_id == prospect.company_id)
        .first()
    )
    if not site:
        site = CustomerSite(
            company_id=prospect.company_id,
            site_name=f"{prospect.name} - HQ",
            city=(
                prospect.hq_location.split(",")[0].strip()
                if prospect.hq_location and "," in prospect.hq_location
                else None
            ),
            state=(
                prospect.hq_location.split(",")[1].strip()
                if prospect.hq_location
                and "," in prospect.hq_location
                and len(prospect.hq_location.split(",")) > 1
                else None
            ),
            is_active=True,
        )
        db.add(site)
        db.flush()

    created = []
    existing_emails = {
        c.email.lower()
        for c in db.query(SiteContact)
        .filter(SiteContact.customer_site_id == site.id)
        .all()
        if c.email
    }

    for i, contact in enumerate(full_contacts):
        email = (contact.get("email") or "").lower().strip()
        if not email or email in existing_emails:
            continue

        sc = SiteContact(
            customer_site_id=site.id,
            full_name=contact.get("name", "Unknown"),
            title=contact.get("title", ""),
            email=email,
            is_primary=(i == 0),
            is_active=True,
            contact_status="new",
        )
        db.add(sc)
        existing_emails.add(email)
        created.append({
            "name": contact.get("name"),
            "title": contact.get("title"),
            "email": email,
            "verified": contact.get("verified", False),
            "seniority": contact.get("seniority", "other"),
        })

    if created:
        db.commit()
        logger.info(
            "Revealed {} contacts for prospect {} (company {})",
            len(created), prospect.id, prospect.company_id,
        )

    return created


# ── AI Briefing ──────────────────────────────────────────────────────


async def generate_account_briefing(prospect_id: int, db: Session) -> str | None:
    """Generate an AI account briefing for a claimed prospect.

    Uses Claude (smart tier) to create a concise briefing with:
    - Company overview, likely component needs, pain points
    - Conversation starters, similar Trio customers

    Falls back to a template briefing if the AI call fails.
    """
    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        return None

    signals = prospect.readiness_signals or {}
    similar = prospect.similar_customers or []

    # Build context for the AI
    similar_names = ", ".join(
        s.get("name", s) if isinstance(s, dict) else str(s)
        for s in similar[:5]
    )

    prompt = f"""Generate a concise account briefing for a salesperson about to contact this prospect.

Company: {prospect.name}
Domain: {prospect.domain}
Industry: {prospect.industry or 'Unknown'}
Size: {prospect.employee_count_range or 'Unknown'}
Revenue: {prospect.revenue_range or 'Unknown'}
Location: {prospect.hq_location or 'Unknown'}
Fit Score: {prospect.fit_score}/100
Readiness Score: {prospect.readiness_score}/100

Intent Signals: {signals.get('intent', 'None detected')}
Hiring Signals: {signals.get('hiring', 'None detected')}
Recent Events: {signals.get('events', 'None detected')}

Similar Existing Customers: {similar_names or 'None identified'}

AI Writeup: {prospect.ai_writeup or 'Not available'}

Write 300-500 words covering:
1. **Company Overview** — what they do and why they're a fit
2. **Likely Component Needs** — based on industry and signals
3. **Pain Points** — what challenges they likely face in procurement
4. **Conversation Starters** — 3 specific openers for the first call
5. **Similar Trio Customers** — how to reference existing relationships

Be specific to electronic component distribution. Write in a direct, actionable style."""

    try:
        from app.utils.claude_client import claude_text

        briefing = await claude_text(
            prompt,
            system="You are a sales intelligence analyst for Trio Supply Chain Solutions, an electronic component distributor. Write concise, actionable account briefings.",
            model_tier="smart",
            max_tokens=1500,
        )

        if briefing:
            return briefing
    except Exception as e:
        logger.error("AI briefing generation failed for prospect {}: {}", prospect_id, e)

    # Fallback: template-based briefing
    return _template_briefing(prospect, signals, similar)


def _template_briefing(prospect: ProspectAccount, signals: dict, similar: list) -> str:
    """Fallback template briefing when AI is unavailable."""
    parts = [f"## Account Briefing: {prospect.name}\n"]

    parts.append(f"**Industry:** {prospect.industry or 'Not specified'}")
    parts.append(f"**Size:** {prospect.employee_count_range or 'Not specified'}")
    parts.append(f"**Location:** {prospect.hq_location or 'Not specified'}")
    parts.append(f"**Fit Score:** {prospect.fit_score}/100 | **Readiness:** {prospect.readiness_score}/100\n")

    intent = signals.get("intent", {})
    if isinstance(intent, dict) and intent.get("strength"):
        parts.append(f"**Intent Signal:** {intent['strength']} — they may be actively sourcing components.")

    hiring = signals.get("hiring", {})
    if isinstance(hiring, dict) and hiring.get("type"):
        parts.append(f"**Hiring Signal:** Recruiting {hiring['type']} — indicates growth/expansion.")

    if similar:
        names = ", ".join(
            s.get("name", s) if isinstance(s, dict) else str(s)
            for s in similar[:3]
        )
        parts.append(f"\n**Similar Customers:** {names}")
        parts.append("Reference these relationships to build credibility on the first call.")

    if prospect.ai_writeup:
        parts.append(f"\n**Analysis:** {prospect.ai_writeup}")

    return "\n".join(parts)


# ── Background Deep Enrichment ───────────────────────────────────────


async def trigger_deep_enrichment_bg(prospect_id: int) -> None:
    """Run deep enrichment in the background after claim.

    Creates its own DB session. Safe to fail — doesn't affect the claim.

    Steps:
    1. Reveal contacts (create SiteContact records)
    2. Generate AI account briefing
    3. Update enrichment status
    """
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        prospect = db.get(ProspectAccount, prospect_id)
        if not prospect:
            logger.error("Deep enrichment: prospect {} not found", prospect_id)
            return

        # Mark as enriching
        ed = dict(prospect.enrichment_data or {})
        ed["claim_enrichment_status"] = "enriching"
        prospect.enrichment_data = ed
        db.commit()

        # Step 1: Reveal contacts
        contacts_created = reveal_contacts(prospect, db)

        # Step 2: Generate AI briefing
        briefing = await generate_account_briefing(prospect_id, db)

        # Step 3: Update prospect with results
        prospect = db.get(ProspectAccount, prospect_id)
        ed = dict(prospect.enrichment_data or {})
        ed["claim_enrichment_status"] = "complete"
        ed["contacts_created_count"] = len(contacts_created)
        ed["contacts_created"] = contacts_created
        if briefing:
            ed["briefing"] = briefing
        ed["deep_enrichment_at"] = datetime.now(timezone.utc).isoformat()
        prospect.enrichment_data = ed
        prospect.last_enriched_at = datetime.now(timezone.utc)
        db.commit()

        # Also update the Company's deep_enrichment_at
        if prospect.company_id:
            company = db.get(Company, prospect.company_id)
            if company:
                company.deep_enrichment_at = datetime.now(timezone.utc)
                company.last_enriched_at = datetime.now(timezone.utc)
                db.commit()

        logger.info(
            "Deep enrichment complete for prospect {}: {} contacts, briefing={}",
            prospect_id, len(contacts_created), bool(briefing),
        )

    except Exception as e:
        logger.error("Deep enrichment failed for prospect {}: {}", prospect_id, e)
        try:
            prospect = db.get(ProspectAccount, prospect_id)
            if prospect:
                ed = dict(prospect.enrichment_data or {})
                ed["claim_enrichment_status"] = "failed"
                ed["enrichment_error"] = str(e)
                prospect.enrichment_data = ed
                db.commit()
        except Exception:
            logger.error("Failed to update enrichment status for prospect {}", prospect_id)
    finally:
        db.close()


# ── Enrichment Status ────────────────────────────────────────────────


def check_enrichment_status(prospect_id: int, db: Session) -> dict:
    """Check the enrichment status after a claim.

    Returns: {status, contacts_created, briefing_ready, error}
    """
    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        raise LookupError("Prospect not found")

    ed = prospect.enrichment_data or {}
    status = ed.get("claim_enrichment_status", "none")

    return {
        "status": status,
        "contacts_created": ed.get("contacts_created_count", 0),
        "briefing_ready": bool(ed.get("briefing")),
        "error": ed.get("enrichment_error"),
    }


# ── Manual Domain Submission ─────────────────────────────────────────


def add_prospect_manually(domain: str, user_id: int, db: Session) -> dict:
    """Submit a domain manually for prospecting.

    Creates a ProspectAccount with source='manual', status='suggested'.
    Deduplicates against existing prospect_accounts by domain.

    Returns: {prospect_id, name, domain, status, is_new}
    """
    domain = domain.strip().lower()
    if not domain:
        raise ValueError("Domain is required")

    # Deduplicate
    existing = (
        db.query(ProspectAccount)
        .filter(ProspectAccount.domain == domain)
        .first()
    )
    if existing:
        return {
            "prospect_id": existing.id,
            "name": existing.name,
            "domain": existing.domain,
            "status": existing.status,
            "is_new": False,
        }

    # Extract company name from domain (basic: strip TLD)
    name_parts = domain.split(".")
    name = name_parts[0].replace("-", " ").replace("_", " ").title()

    prospect = ProspectAccount(
        name=name,
        domain=domain,
        discovery_source="manual",
        status="suggested",
        fit_score=0,
        readiness_score=0,
        enrichment_data={"submitted_by": user_id},
    )
    db.add(prospect)
    db.commit()
    db.refresh(prospect)

    logger.info("Manual prospect added: {} ({}) by user {}", name, domain, user_id)

    return {
        "prospect_id": prospect.id,
        "name": prospect.name,
        "domain": prospect.domain,
        "status": "suggested",
        "is_new": True,
    }
