"""services/account_summary_service.py -- AI-generated account summary.

Gathers context from company data, requisitions, activities, and contacts
to produce a strategic account summary via Claude. Called from the companies
router and rendered on the account overview tab.
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ..models import (
    Activity,
    Company,
    CustomerSite,
    Requirement,
    Requisition,
    SiteContact,
)


async def generate_account_summary(company_id: int, db: Session) -> dict:
    """Build context and ask Claude for a strategic account summary.

    Returns dict with keys: situation, development, next_steps.
    """
    from ..utils.claude_client import claude_json

    company = db.get(Company, company_id)
    if not company:
        return {}

    # ── Gather context ───────────────────────────────────────────────

    # Sites
    sites = (
        db.query(CustomerSite)
        .filter(CustomerSite.company_id == company_id, CustomerSite.is_active == True)  # noqa: E712
        .all()
    )
    site_ids = [s.id for s in sites]

    # Contacts across all sites
    contacts = []
    if site_ids:
        contacts = (
            db.query(SiteContact)
            .filter(SiteContact.site_id.in_(site_ids), SiteContact.is_active == True)  # noqa: E712
            .all()
        )

    # Requisitions and their statuses
    reqs = []
    if site_ids:
        reqs = (
            db.query(Requisition)
            .filter(Requisition.customer_site_id.in_(site_ids))
            .order_by(Requisition.created_at.desc())
            .limit(50)
            .all()
        )

    # Requirement count per req
    req_counts = {}
    if reqs:
        req_ids = [r.id for r in reqs]
        count_rows = (
            db.query(Requirement.requisition_id, sqlfunc.count(Requirement.id))
            .filter(Requirement.requisition_id.in_(req_ids))
            .group_by(Requirement.requisition_id)
            .all()
        )
        req_counts = {row[0]: row[1] for row in count_rows}

    # Recent activities
    activities = (
        db.query(Activity)
        .filter(Activity.company_id == company_id)
        .order_by(Activity.created_at.desc())
        .limit(20)
        .all()
    )

    # ── Build prompt context ─────────────────────────────────────────

    now = datetime.now(timezone.utc)

    # Company basics
    ctx_parts = [f"Company: {company.name}"]
    if company.industry:
        ctx_parts.append(f"Industry: {company.industry}")
    if company.employee_size:
        ctx_parts.append(f"Size: {company.employee_size} employees")
    if company.hq_city:
        loc = company.hq_city + (f", {company.hq_state}" if company.hq_state else "")
        ctx_parts.append(f"HQ: {loc}")
    if company.account_type:
        ctx_parts.append(f"Account type: {company.account_type}")
    if company.is_strategic:
        ctx_parts.append("Flagged as STRATEGIC account")
    if company.credit_terms:
        ctx_parts.append(f"Credit terms: {company.credit_terms}")
    if company.domain:
        ctx_parts.append(f"Domain: {company.domain}")

    owner_name = company.account_owner.name if company.account_owner else "Unassigned"
    ctx_parts.append(f"Account owner: {owner_name}")
    ctx_parts.append(f"Sites: {len(sites)}")
    ctx_parts.append(f"Contacts: {len(contacts)}")

    # Tags
    brands = company.brand_tags or []
    commodities = company.commodity_tags or []
    if brands:
        ctx_parts.append(f"Brand focus: {', '.join(brands)}")
    if commodities:
        ctx_parts.append(f"Commodity focus: {', '.join(commodities)}")

    # Notes
    if company.notes:
        ctx_parts.append(f"Account notes: {company.notes[:500]}")

    # Contact summary
    if contacts:
        contact_lines = []
        for c in contacts[:10]:
            parts = [c.full_name or "Unknown"]
            if c.title:
                parts.append(f"({c.title})")
            if c.is_primary:
                parts.append("[PRIMARY]")
            contact_lines.append(" ".join(parts))
        ctx_parts.append("Key contacts:\n" + "\n".join(f"  - {cl}" for cl in contact_lines))

    # Pipeline summary
    if reqs:
        status_counts = {}
        for r in reqs:
            st = (r.status or "open").lower()
            status_counts[st] = status_counts.get(st, 0) + 1
        pipeline_parts = [f"{st}: {cnt}" for st, cnt in status_counts.items()]
        ctx_parts.append(f"Pipeline ({len(reqs)} total): {', '.join(pipeline_parts)}")

        # Recent reqs detail
        recent_lines = []
        for r in reqs[:8]:
            mpn_count = req_counts.get(r.id, 0)
            age_days = (now - r.created_at).days if r.created_at else "?"
            recent_lines.append(f"  - REQ-{r.id} '{r.name}' ({r.status}, {mpn_count} MPNs, {age_days}d ago)")
        ctx_parts.append("Recent requisitions:\n" + "\n".join(recent_lines))

    # Activity summary
    if activities:
        type_counts = {}
        for a in activities:
            t = a.activity_type or "other"
            type_counts[t] = type_counts.get(t, 0) + 1
        act_parts = [f"{t}: {cnt}" for t, cnt in type_counts.items()]
        ctx_parts.append(f"Recent activity ({len(activities)} events): {', '.join(act_parts)}")

        last_act = activities[0]
        if last_act.created_at:
            days_since = (now - last_act.created_at).days
            ctx_parts.append(f"Last activity: {days_since} day(s) ago ({last_act.activity_type})")
    else:
        ctx_parts.append("No activity recorded yet")

    context = "\n".join(ctx_parts)

    prompt = (
        "You are an account strategist for an electronic component distribution company (Trio Supply Chain Solutions). "
        "Analyze this account and provide a concise strategic summary.\n\n"
        f"{context}\n\n"
        "Return JSON with exactly three keys:\n"
        '- "situation": 2-3 sentences summarizing what we know about this account — '
        "their industry, size, what they buy, who our contacts are, and current relationship status.\n"
        '- "development": 2-3 sentences on how account development is going — '
        "pipeline health, activity level, engagement trends, wins/losses.\n"
        '- "next_steps": 2-3 bullet points (as an array of strings) on concrete actions to '
        "penetrate deeper or farm the account — specific, actionable recommendations.\n\n"
        "Be direct and specific. Reference actual data (contact names, req counts, etc). "
        "If data is sparse, say so and recommend data-gathering actions.\n"
        "Return ONLY the JSON object."
    )

    try:
        result = await claude_json(
            prompt,
            system="You are a strategic account advisor for electronic component distribution. "
            "Your summaries are concise, data-driven, and actionable. "
            "Focus on account penetration and farming strategies.",
            model_tier="fast",
            max_tokens=600,
        )
    except Exception:
        logger.exception("Account summary generation failed for company %d", company_id)
        return {}

    if not result or not isinstance(result, dict):
        return {}

    return {
        "situation": str(result.get("situation", "")),
        "development": str(result.get("development", "")),
        "next_steps": result.get("next_steps", []),
    }
