"""Phase 7 — Claim workflow with deep enrichment and AI briefing.

Handles what happens when a salesperson claims a prospect:
1. Atomic claim (dual-path: SF-migrated vs new discovery)
2. Domain collision detection (prevent duplicate Companies)
3. Contact reveal (unmask emails from enrichment_data)
4. Background deep enrichment (create SiteContacts, verify emails, AI briefing)
5. Enrichment status polling
"""

from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.constants import ProspectAccountStatus
from app.models import Company, User
from app.models.crm import CustomerSite, SiteContact
from app.models.prospect_account import ProspectAccount

# Max active accounts (owned Companies) a single rep may hold — the anti-hoarding cap.
# Enforced here in the service so every claim entry point (HTMX tab + any future caller)
# inherits it identically. Claim assigns *company-level* ownership
# (Company.account_owner_id), so the cap counts that axis. Counting CustomerSite.owner_id
# was a dead no-op — claim never sets a site owner, so the guard never tripped (audit H9).
ACCOUNT_CAP = 200


def _active_account_count(db: Session, user_id: int) -> int:
    """Count the active Companies a rep owns — the axis a claim actually assigns."""
    return (
        db.query(func.count(Company.id))
        .filter(Company.account_owner_id == user_id, Company.is_active.is_(True))
        .scalar()
        or 0
    )


def _split_hq_location(hq_location: str | None) -> tuple[str | None, str | None]:
    """Split a 'City, State' HQ location into (city, state).

    Returns (None, None) when there's no comma to split on. Mirrors the prior
    inline logic: the first comma-delimited field is the city and the second is
    the state (any further fields are ignored).
    """
    if not hq_location or "," not in hq_location:
        return None, None
    parts = hq_location.split(",")
    return parts[0].strip(), parts[1].strip()


def _format_similar_names(similar: list, limit: int) -> str:
    """Join the first `limit` similar customers into a comma-separated string.

    Each entry may be a dict (use its ``name``) or a bare string/value.
    """
    return ", ".join(s.get("name", s) if isinstance(s, dict) else str(s) for s in similar[:limit])


# ── Claim ────────────────────────────────────────────────────────────


def _link_or_create_company(prospect: ProspectAccount, owner_id: int, db: Session) -> tuple[str, str | None]:
    """Point a Company at ``owner_id`` for this prospect and link it back.

    The shared company-linking core of both claim_prospect (owner = the claiming rep) and
    assign_prospect (owner = the manager-chosen rep). Three mutually-exclusive paths, all
    setting ``Company.account_owner_id = owner_id`` so the account surfaces on the CRM
    (Customers) tab under that owner:

    - ``existing_company``   — prospect.company_id already set: transfer ownership.
    - ``domain_collision``   — another Company shares the domain: link + transfer.
    - ``new_company``        — create a Company (+ default HQ site) from the prospect data.

    Returns ``(path, warning)`` — ``warning`` is set only on a domain collision (the claim
    merged into a DIFFERENT existing account). Raises ``ValueError`` when the target Company
    is already owned by a different user (never silently steal an owned account).
    """
    if prospect.company_id:
        # PATH A: SF-migrated / already-linked — update existing Company
        company = db.query(Company).filter(Company.id == prospect.company_id).with_for_update().first()
        if company:
            if company.account_owner_id and company.account_owner_id != owner_id:
                raise ValueError(f"Company '{company.name}' is already owned by another user.")
            company.account_owner_id = owner_id
        return "existing_company", None

    # PATH B: New discovery — check for domain collision first
    existing = (
        db.query(Company).filter(Company.domain == prospect.domain).with_for_update().first()
        if prospect.domain
        else None
    )

    if existing:
        # Domain collision: link to existing Company instead of creating
        if existing.account_owner_id and existing.account_owner_id != owner_id:
            raise ValueError(f"Company '{existing.name}' (same domain) is already owned by another user.")
        existing.account_owner_id = owner_id
        prospect.company_id = existing.id
        logger.warning(
            "Domain collision on claim/assign: prospect {} matched company {} ({})",
            prospect.id,
            existing.id,
            existing.domain,
        )
        return "domain_collision", f"Linked to existing company '{existing.name}' (same domain)"

    # Create new Company from prospect data
    hq_city, hq_state = _split_hq_location(prospect.hq_location)
    company = Company(
        name=prospect.name,
        domain=prospect.domain,
        website=prospect.website,
        industry=prospect.industry,
        hq_city=hq_city,
        hq_state=hq_state,
        employee_size=prospect.employee_count_range,
        is_active=True,
        account_owner_id=owner_id,
        source="prospecting",
    )
    db.add(company)
    db.flush()
    # Auto-create default HQ site so company appears in pickers
    default_site = CustomerSite(company_id=company.id, site_name="HQ")
    db.add(default_site)
    db.flush()
    prospect.company_id = company.id
    return "new_company", None


def claim_prospect(prospect_id: int, user_id: int, db: Session) -> dict:
    """Claim a prospect account — atomic, handles both paths.

    PATH A (SF-migrated, company_id set): transfer ownership of existing Company.
    PATH B (new discovery, company_id NULL): create Company, link it.
    Domain collision: if another Company with same domain exists, link to it.

    Returns: {prospect_id, company_id, company_name, status, path, warning}
    Raises: ValueError for invalid state transitions or cooldown block.
    """

    from ..dependencies import is_manager_or_admin

    prospect = db.query(ProspectAccount).filter(ProspectAccount.id == prospect_id).with_for_update().first()
    if not prospect:
        raise LookupError("Prospect not found")

    if prospect.status == ProspectAccountStatus.CLAIMED:
        raise ValueError("Already claimed")

    if prospect.status not in (ProspectAccountStatus.SUGGESTED,):
        raise ValueError(f"Cannot claim prospect with status '{prospect.status}'")

    user = db.get(User, user_id)
    if not user:
        raise LookupError("User not found")

    # Phase 4: former owner cannot self-serve around the 30-day reclaim cooldown.
    # Managers/admins are exempt — they should use reassign but claim is allowed too.
    if prospect.swept_from_owner_id == user_id and not is_manager_or_admin(user):
        blocked_until = prospect.reclaim_blocked_until
        if blocked_until is not None:
            if blocked_until.tzinfo is None:
                blocked_until = blocked_until.replace(tzinfo=UTC)
            if blocked_until > datetime.now(UTC):
                raise ValueError("This account is in a 30-day cooldown; ask a manager to reassign it.")

    if _active_account_count(db, user_id) >= ACCOUNT_CAP:
        raise ValueError(
            f"You own {ACCOUNT_CAP} or more active accounts (the cap). "
            "Release inactive accounts before claiming new ones."
        )

    path, warning = _link_or_create_company(prospect, user_id, db)

    # Update prospect status
    prospect.status = ProspectAccountStatus.CLAIMED
    prospect.claimed_by = user_id
    prospect.claimed_at = datetime.now(UTC)

    # Set enrichment status to pending
    ed = dict(prospect.enrichment_data or {})
    ed["claim_enrichment_status"] = "pending"
    prospect.enrichment_data = ed

    db.commit()

    logger.info(
        "User {} claimed prospect {} ({}) via {}",
        user.name if user else user_id,
        prospect.name,
        prospect.id,
        path,
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


def assign_prospect(prospect_id: int, to_user_id: int, by_user: User, db: Session) -> dict:
    """Manager/admin assigns a pool prospect to a chosen rep (the O-rework Assign
    action).

    The supervisor counterpart of claim_prospect and the single successor to the retired
    reclaim/reassign controls: instead of a rep grabbing an account for themselves, a manager
    hands ANY suggested pool account to a chosen rep. It links/creates the CRM Company owned
    by that rep (same three paths as a claim, via _link_or_create_company) and removes the
    account from the pool (status -> CLAIMED, claimed_by = to_user_id).

    Because a manager action is authoritative it deliberately bypasses the two rep-facing
    guards claim_prospect enforces: the former-owner 30-day sweep cooldown (so a swept account
    can be handed back to ANY rep, incl. the original owner, at any time — this subsumes the
    old "put-it-back" reclaim) and the per-rep anti-hoarding cap (mirroring the CRM
    assign-owner + legacy reassign_account, which never checked it). Any lingering sweep
    cooldown is cleared since the account has left the pool.

    Gate: is_manager_or_admin(by_user) -> PermissionError.
    Returns: {prospect_id, company_id, company_name, to_user_id, status:"claimed", path, warning?}
    Raises: PermissionError (not a supervisor), LookupError (prospect / target user missing),
            ValueError (prospect not SUGGESTED / target inactive / company owned by another).
    """
    from ..dependencies import is_manager_or_admin

    if not is_manager_or_admin(by_user):
        raise PermissionError("Only a manager or admin can assign an account")

    prospect = db.query(ProspectAccount).filter(ProspectAccount.id == prospect_id).with_for_update().first()
    if not prospect:
        raise LookupError("Prospect not found")

    if prospect.status != ProspectAccountStatus.SUGGESTED:
        raise ValueError(f"Only a suggested prospect can be assigned (status='{prospect.status}').")

    target = db.get(User, to_user_id)
    if not target:
        raise LookupError("Target user not found")
    if not target.is_active:
        raise ValueError("Target user is inactive")

    path, warning = _link_or_create_company(prospect, to_user_id, db)

    prospect.status = ProspectAccountStatus.CLAIMED
    prospect.claimed_by = to_user_id
    prospect.claimed_at = datetime.now(UTC)
    # A manager assignment ends any sweep cooldown — the account has left the pool.
    prospect.reclaim_blocked_until = None
    ed = dict(prospect.enrichment_data or {})
    ed["claim_enrichment_status"] = "pending"
    prospect.enrichment_data = ed

    db.commit()

    logger.info(
        "Manager {} assigned prospect {} ({}) to user {} via {}",
        by_user.id,
        prospect.name,
        prospect.id,
        to_user_id,
        path,
    )

    result = {
        "prospect_id": prospect.id,
        "company_id": prospect.company_id,
        "company_name": prospect.name,
        "to_user_id": to_user_id,
        "status": "claimed",
        "enrichment_status": "pending",
        "path": path,
    }
    if warning:
        result["warning"] = warning
    return result


def release_prospect(prospect_id: int, user_id: int, db: Session, *, is_admin: bool = False) -> dict:
    """Release a claimed prospect back to the suggested pool.

    The inverse of claim_prospect: status -> SUGGESTED, clear claimed_by/claimed_at,
    and relinquish ownership of the linked Company (account_owner_id -> NULL). The
    Company row itself is kept (re-claiming re-owns it). Only the claimer or an admin
    may release; only CLAIMED prospects can be released.

    Returns: {prospect_id, company_name, status}
    Raises: LookupError if missing, ValueError on bad status / ownership.
    """
    prospect = db.query(ProspectAccount).filter(ProspectAccount.id == prospect_id).with_for_update().first()
    if not prospect:
        raise LookupError("Prospect not found")

    if prospect.status != ProspectAccountStatus.CLAIMED:
        raise ValueError("Only a claimed prospect can be released")

    if not is_admin and prospect.claimed_by != user_id:
        raise ValueError("Only the owner or an admin can release this prospect")

    if prospect.company_id:
        company = db.query(Company).filter(Company.id == prospect.company_id).with_for_update().first()
        if company and company.account_owner_id == prospect.claimed_by:
            company.account_owner_id = None

    prospect.status = ProspectAccountStatus.SUGGESTED
    prospect.claimed_by = None
    prospect.claimed_at = None
    ed = dict(prospect.enrichment_data or {})
    ed.pop("claim_enrichment_status", None)
    prospect.enrichment_data = ed
    db.commit()

    logger.info("Prospect {} ({}) released back to the pool by user {}", prospect.name, prospect.id, user_id)

    return {
        "prospect_id": prospect.id,
        "company_name": prospect.name,
        "status": "suggested",
    }


def dismiss_prospect(prospect_id: int, user_id: int, db: Session, *, reason: str = "other") -> dict:
    """Dismiss a SUGGESTED prospect out of the pool.

    The transition the dismiss button used to inline in the router (audit M17 — "keep
    routers thin"): status -> DISMISSED, stamp who/when, and record the ``reason`` (defaults
    to ``"other"``; the field is ``String(255)`` so it is trimmed). Claimed prospects use
    Release instead — only a SUGGESTED prospect can be dismissed. Row is locked for update
    so a concurrent claim/dismiss can't race the status.

    Returns: {prospect_id, company_name, status}
    Raises: LookupError if missing, ValueError if not SUGGESTED.
    """
    prospect = db.query(ProspectAccount).filter(ProspectAccount.id == prospect_id).with_for_update().first()
    if not prospect:
        raise LookupError("Prospect not found")

    if prospect.status != ProspectAccountStatus.SUGGESTED:
        raise ValueError("Only suggested prospects can be dismissed.")

    prospect.status = ProspectAccountStatus.DISMISSED
    prospect.dismissed_by = user_id
    prospect.dismissed_at = datetime.now(UTC)
    prospect.dismiss_reason = (reason or "other").strip()[:255]
    db.commit()

    logger.info(
        "Prospect {} ({}) dismissed by user {} (reason={})",
        prospect.name,
        prospect.id,
        user_id,
        prospect.dismiss_reason,
    )
    return {
        "prospect_id": prospect.id,
        "company_name": prospect.name,
        "status": "dismissed",
    }


# ── Convert to opportunity ───────────────────────────────────────────


def mark_prospect_converted(prospect_id: int, user_id: int, db: Session) -> bool:
    """Flip a CLAIMED prospect to CONVERTED after a requisition is created from it.

    The terminal "became a real opportunity" state (H1/M4): a won account leaves the
    claimed bucket for good. Called by requisition_import_save when the create-
    requisition modal was launched from a prospect's "Create Requisition" button (it
    rides a hidden prospect_id through the save). Best-effort and idempotent — only a
    CLAIMED prospect converts; a missing / already-converted / non-claimed prospect is a
    silent no-op, so conversion never blocks the requisition that already committed.

    Returns True when the status actually flipped.
    """
    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        return False
    if prospect.status != ProspectAccountStatus.CLAIMED:
        logger.info(
            "mark_prospect_converted: prospect {} not CLAIMED (status={}); skipping",
            prospect_id,
            prospect.status,
        )
        return False

    # Authorization (IDOR guard): only the buyer who CLAIMED this prospect may convert it.
    # ``prospect_id`` rides in as a client-controlled hidden form field on the requisition
    # save, so without this check an authenticated user could forge another user's claimed
    # prospect id and flip it CONVERTED — yanking it out of that buyer's pipeline. Not the
    # claimer → silent no-op (best-effort, consistent with the rest of this function).
    if prospect.claimed_by != user_id:
        logger.warning(
            "mark_prospect_converted: user {} is not the claimer of prospect {} (claimed_by={}); refusing",
            user_id,
            prospect_id,
            prospect.claimed_by,
        )
        return False

    prospect.status = ProspectAccountStatus.CONVERTED
    ed = dict(prospect.enrichment_data or {})
    ed["converted_at"] = datetime.now(UTC).isoformat()
    ed["converted_by"] = user_id
    prospect.enrichment_data = ed
    db.commit()

    logger.info("Prospect {} ({}) converted to opportunity by user {}", prospect.name, prospect_id, user_id)
    return True


def send_company_to_prospecting(
    company_id: int, user_id: int, db: Session, *, is_admin: bool = False, swept: dict | None = None
) -> dict:
    """Send an owned Company back to the prospecting pool.

    Disposition counterpart of release_prospect, but keyed off the Company (not a
    ProspectAccount): relinquish ownership (account_owner_id -> NULL,
    ownership_cleared_at=now) and surface the account in the pool as a SUGGESTED
    ProspectAccount keyed by domain (find-or-create dedupe, like
    add_prospect_manually). When the Company has no domain we cannot key a pool
    row — fall back to ownership-clear only. The Company row itself is always kept.

    Perms: owner-or-admin. An admin may force-clear another owner's account
    (mirrors release_prospect's is_admin override).

    ``swept`` (SP4 sweep only): when supplied, the pool row is stamped with the park
    provenance — ``discovery_source="auto_sweep"`` plus ``swept_from_owner_id`` / ``swept_at``
    / ``reclaim_blocked_until`` — inside the SAME transaction as the ownership-clear (audit
    M10). Previously the sweep committed the park, then committed the swept stamp separately;
    a crash between the two left the account unowned-and-pooled but with no cooldown, so the
    former owner could instantly re-claim it. Expected keys:
    ``{"from_owner_id", "at", "reclaim_blocked_until"}``.

    Returns: {company_id, company_name, prospect_id|None, pooled: bool}
    Raises: LookupError if the company is missing; ValueError on permission.
    """
    company = db.query(Company).filter(Company.id == company_id).with_for_update().first()
    if not company:
        raise LookupError("Company not found")

    if not is_admin and company.account_owner_id != user_id:
        raise ValueError("Only the owner or an admin can send this account to prospecting")

    try:
        company.account_owner_id = None
        company.ownership_cleared_at = datetime.now(UTC)

        prospect_id: int | None = None
        pool_prospect: ProspectAccount | None = None
        domain = (company.domain or "").strip().lower()
        if domain:
            existing = db.query(ProspectAccount).filter(ProspectAccount.domain == domain).first()
            if existing:
                pool_prospect = existing
            else:
                # ProspectAccount.domain is UNIQUE NOT NULL. Insert inside a SAVEPOINT
                # so a concurrent send claiming the same domain rolls back ONLY the
                # failed insert (the ownership-clear above survives); then adopt the
                # row the other writer created instead of bubbling a 500.
                try:
                    with db.begin_nested():
                        pool_prospect = ProspectAccount(
                            name=company.name,
                            domain=domain,
                            discovery_source="sent_back",
                            status=ProspectAccountStatus.SUGGESTED,
                            fit_score=0,
                            readiness_score=0,
                            company_id=company.id,
                            enrichment_data={"sent_back_by": user_id},
                        )
                        db.add(pool_prospect)
                        db.flush()
                except IntegrityError:
                    pool_prospect = db.query(ProspectAccount).filter(ProspectAccount.domain == domain).first()

            # Stamp SP4 park provenance in THIS transaction (audit M10) so a parked account
            # always carries its cooldown — never lands as a plain re-claimable prospect.
            if swept and pool_prospect is not None:
                pool_prospect.swept_from_owner_id = swept.get("from_owner_id")
                pool_prospect.swept_at = swept.get("at")
                pool_prospect.reclaim_blocked_until = swept.get("reclaim_blocked_until")
                pool_prospect.discovery_source = "auto_sweep"

            prospect_id = pool_prospect.id if pool_prospect else None

        db.commit()
    except Exception:
        db.rollback()
        raise

    logger.info(
        "Company {} ({}) sent to prospecting by user {} (pooled={})",
        company.name,
        company_id,
        user_id,
        prospect_id is not None,
    )

    return {
        "company_id": company_id,
        "company_name": company.name,
        "prospect_id": prospect_id,
        "pooled": prospect_id is not None,
    }


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
    site = db.query(CustomerSite).filter(CustomerSite.company_id == prospect.company_id).first()
    if not site:
        hq_city, hq_state = _split_hq_location(prospect.hq_location)
        site = CustomerSite(
            company_id=prospect.company_id,
            site_name=f"{prospect.name} - HQ",
            city=hq_city,
            state=hq_state,
            is_active=True,
        )
        db.add(site)
        db.flush()

    created = []
    existing_emails = {
        c.email.lower() for c in db.query(SiteContact).filter(SiteContact.customer_site_id == site.id).all() if c.email
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
        created.append(
            {
                "name": contact.get("name"),
                "title": contact.get("title"),
                "email": email,
                "verified": contact.get("verified", False),
                "seniority": contact.get("seniority", "other"),
            }
        )

    if created:
        db.commit()
        logger.info(
            "Revealed {} contacts for prospect {} (company {})",
            len(created),
            prospect.id,
            prospect.company_id,
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
    similar_names = _format_similar_names(similar, 5)

    prompt = f"""Generate a concise account briefing for a salesperson about to contact this prospect.

Company: {prospect.name}
Domain: {prospect.domain}
Industry: {prospect.industry or "Unknown"}
Size: {prospect.employee_count_range or "Unknown"}
Revenue: {prospect.revenue_range or "Unknown"}
Location: {prospect.hq_location or "Unknown"}
Fit Score: {prospect.fit_score}/100
Readiness Score: {prospect.readiness_score}/100

Intent Signals: {signals.get("intent", "None detected")}
Hiring Signals: {signals.get("hiring", "None detected")}
Recent Events: {signals.get("events", "None detected")}

Similar Existing Customers: {similar_names or "None identified"}

AI Writeup: {prospect.ai_writeup or "Not available"}

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
        names = _format_similar_names(similar, 3)
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
        if not prospect:
            logger.error("Deep enrichment: prospect {} disappeared after async steps", prospect_id)
            return
        ed = dict(prospect.enrichment_data or {})
        ed["claim_enrichment_status"] = "complete"
        ed["contacts_created_count"] = len(contacts_created)
        ed["contacts_created"] = contacts_created
        if briefing:
            ed["briefing"] = briefing
        ed["deep_enrichment_at"] = datetime.now(UTC).isoformat()
        prospect.enrichment_data = ed
        prospect.last_enriched_at = datetime.now(UTC)
        db.commit()

        # Also update the Company's deep_enrichment_at
        if prospect.company_id:
            company = db.get(Company, prospect.company_id)
            if company:
                company.deep_enrichment_at = datetime.now(UTC)
                company.last_enriched_at = datetime.now(UTC)
                db.commit()

        logger.info(
            "Deep enrichment complete for prospect {}: {} contacts, briefing={}",
            prospect_id,
            len(contacts_created),
            bool(briefing),
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
    existing = db.query(ProspectAccount).filter(ProspectAccount.domain == domain).first()
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
        status=ProspectAccountStatus.SUGGESTED,
        fit_score=0,
        readiness_score=0,
        enrichment_data={"submitted_by": user_id},
    )
    # The first()-check above is a TOCTOU window: a concurrent add of the same domain can
    # slip between it and the insert. ProspectAccount.domain is UNIQUE, so insert inside a
    # SAVEPOINT and adopt the winner's row on IntegrityError instead of 500ing (audit M13 —
    # same race-safe pattern as send_company_to_prospecting).
    try:
        with db.begin_nested():
            db.add(prospect)
            db.flush()
    except IntegrityError:
        dup = db.query(ProspectAccount).filter(ProspectAccount.domain == domain).first()
        if dup:
            logger.info("Manual prospect add race: adopted existing {} ({})", dup.name, domain)
            return {
                "prospect_id": dup.id,
                "name": dup.name,
                "domain": dup.domain,
                "status": dup.status,
                "is_new": False,
            }
        raise
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
