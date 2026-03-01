"""Phase 5 — Contact enrichment with email verification.

Finds real procurement contacts at prospect companies via Apollo,
verifies their emails via Hunter, classifies seniority, and flags new hires.

Quality gate: no unverified emails reach the sales team.
All functions are idempotent. Personal emails (gmail, etc.) are filtered out.
"""

import asyncio
import re
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.config import settings
from app.models.prospect_account import ProspectAccount

# ── Personal Email Filter ────────────────────────────────────────────

PERSONAL_DOMAINS = {
    "gmail.com",
    "yahoo.com",
    "hotmail.com",
    "outlook.com",
    "aol.com",
    "icloud.com",
    "mail.com",
    "protonmail.com",
    "zoho.com",
    "yandex.com",
    "live.com",
    "msn.com",
    "me.com",
    "qq.com",
    "163.com",
    "126.com",
    "gmx.com",
    "gmx.de",
    "web.de",
    "t-online.de",
    "comcast.net",
    "sbcglobal.net",
    "att.net",
    "verizon.net",
    "cox.net",
}

# ── Title Keywords for Apollo Search ─────────────────────────────────

PROCUREMENT_TITLE_KEYWORDS = [
    "procurement",
    "purchasing",
    "supply chain",
    "commodity manager",
    "component engineer",
    "VP operations",
    "director of sourcing",
    "buyer",
]

# ── Seniority Classification ────────────────────────────────────────

DECISION_MAKER_PATTERNS = [
    r"\bvp\b",
    r"\bvice\s+president\b",
    r"\bdirector\b",
    r"\bdir\.?\b",
    r"\bsvp\b",
    r"\bevp\b",
    r"\bc[- ]?suite\b",
    r"\bchief\b",
    r"\bceo\b",
    r"\bcoo\b",
    r"\bcfo\b",
    r"\bcpo\b",
    r"\bcto\b",
    r"\bhead\s+of\b",
    r"\bgm\b",
    r"\bgeneral\s+manager\b",
]

INFLUENCER_PATTERNS = [
    r"\bmanager\b",
    r"\bsenior\b",
    r"\bsr\.?\b",
    r"\blead\b",
    r"\bcommodity\s+manager\b",
    r"\bprincipal\b",
    r"\bteam\s+lead\b",
]

EXECUTOR_PATTERNS = [
    r"\bbuyer\b",
    r"\bpurchasing\s+agent\b",
    r"\bcoordinator\b",
    r"\banalyst\b",
    r"\bspecialist\b",
    r"\bplanner\b",
    r"\bassistant\b",
    r"\bclerk\b",
]


def classify_contact_seniority(title: str) -> str:
    """Classify contact seniority from job title.

    Returns: "decision_maker", "influencer", "executor", or "other".
    """
    if not title:
        return "other"

    t = title.lower().strip()

    # Check decision_maker first (VP/Director outranks Manager)
    for pattern in DECISION_MAKER_PATTERNS:
        if re.search(pattern, t):
            return "decision_maker"

    for pattern in INFLUENCER_PATTERNS:
        if re.search(pattern, t):
            return "influencer"

    for pattern in EXECUTOR_PATTERNS:
        if re.search(pattern, t):
            return "executor"

    return "other"


# ── Email Masking ────────────────────────────────────────────────────


def mask_email(email: str) -> str:
    """Mask an email for contacts_preview display.

    "john.smith@company.com" -> "j***@comp..."
    """
    if not email or "@" not in email:
        return ""

    local, domain = email.split("@", 1)
    masked_local = local[0] + "***" if local else "***"
    masked_domain = domain[:4] + "..." if len(domain) > 4 else domain
    return f"{masked_local}@{masked_domain}"


def _is_personal_email(email: str) -> bool:
    """Check if email is from a personal domain (gmail, etc.)."""
    if not email or "@" not in email:
        return False
    domain = email.split("@", 1)[1].lower()
    return domain in PERSONAL_DOMAINS


# ── New Hire Detection ───────────────────────────────────────────────


def _is_new_hire(started_at: str | None) -> bool:
    """Check if someone started their current role within the last 6 months."""
    if not started_at:
        return False
    try:
        start_date = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        six_months_ago = datetime.now(timezone.utc).replace(month=max(1, datetime.now(timezone.utc).month - 6))
        return start_date >= six_months_ago
    except (ValueError, TypeError):
        return False


# ── Credit Tracking ──────────────────────────────────────────────────


class CreditTracker:
    """Track API credit usage within a batch run.

    Simple in-memory counter. Resets between batch invocations.
    For monthly tracking, could be persisted to DB in future.
    """

    def __init__(
        self,
        apollo_limit: int | None = None,
        hunter_search_limit: int | None = None,
        hunter_verify_limit: int | None = None,
    ):
        self.apollo_used = 0
        self.apollo_limit = apollo_limit if apollo_limit is not None else 10000
        self.hunter_search_used = 0
        self.hunter_search_limit = (
            hunter_search_limit if hunter_search_limit is not None else settings.hunter_monthly_search_limit
        )
        self.hunter_verify_used = 0
        self.hunter_verify_limit = (
            hunter_verify_limit if hunter_verify_limit is not None else settings.hunter_monthly_verify_limit
        )

    def can_use_apollo(self) -> bool:
        return self.apollo_used < self.apollo_limit

    def use_apollo(self, count: int = 1) -> None:
        self.apollo_used += count

    def can_use_hunter_search(self) -> bool:
        return self.hunter_search_used < self.hunter_search_limit

    def use_hunter_search(self, count: int = 1) -> None:
        self.hunter_search_used += count

    def can_use_hunter_verify(self) -> bool:
        return self.hunter_verify_used < self.hunter_verify_limit

    def use_hunter_verify(self, count: int = 1) -> None:
        self.hunter_verify_used += count

    def summary(self) -> dict:
        return {
            "apollo_credits_used": self.apollo_used,
            "hunter_searches_used": self.hunter_search_used,
            "hunter_verifications_used": self.hunter_verify_used,
        }


# ── Apollo People Search ────────────────────────────────────────────


async def search_contacts_apollo(domain: str, max_results: int = 10) -> list[dict]:
    """Search Apollo for procurement contacts at a domain.

    Filters by procurement/purchasing/supply chain titles.
    Normalizes emails to lowercase and filters out personal emails.

    Returns: [{name, title, email, linkedin_url, seniority_level, started_current_role_at}]
    """
    from app.connectors.apollo_client import search_contacts

    raw_contacts = await search_contacts(
        company_name="",
        domain=domain,
        title_keywords=PROCUREMENT_TITLE_KEYWORDS,
        limit=max_results,
    )

    results = []
    for c in raw_contacts:
        email = (c.get("email") or "").strip().lower() or None

        # Filter personal emails
        if email and _is_personal_email(email):
            logger.debug("Filtered personal email: {}", mask_email(email))
            email = None

        results.append(
            {
                "name": c.get("full_name") or "Unknown",
                "title": c.get("title") or "",
                "email": email,
                "linkedin_url": c.get("linkedin_url"),
                "seniority_level": classify_contact_seniority(c.get("title") or ""),
                "started_current_role_at": c.get("started_current_role_at"),
            }
        )

    return results


# ── Hunter Email Verification ───────────────────────────────────────


async def verify_email_hunter(email: str, verification_cache: dict | None = None) -> dict:
    """Verify an email via Hunter.io.

    Returns: {email, status, score, verified: bool}
    Quality gate: only "valid" and "accept_all" (score>80) pass.
    Uses cache to avoid re-verifying the same email.
    """
    if not email:
        return {"email": "", "status": "unknown", "score": 0, "verified": False}

    email = email.lower().strip()

    # Check cache
    if verification_cache and email in verification_cache:
        return verification_cache[email]

    from app.connectors.hunter_client import verify_email

    result = await verify_email(email)

    if result is None:
        # Hunter unavailable — mark as unverified but still return
        out = {"email": email, "status": "unknown", "score": 0, "verified": False}
    else:
        status = result.get("status", "unknown")
        score = result.get("score", 0)
        verified = status == "valid" or (status == "accept_all" and score > 80)
        out = {
            "email": email,
            "status": status,
            "score": score,
            "verified": verified,
        }

    # Cache result
    if verification_cache is not None:
        verification_cache[email] = out

    return out


# ── Hunter Domain Pattern ───────────────────────────────────────────


async def get_domain_pattern_hunter(domain: str) -> str | None:
    """Get email pattern for a domain via Hunter domain-search.

    Returns pattern like "{first}.{last}", "{f}{last}", etc.
    """
    from app.connectors.hunter_client import find_domain_emails

    if not domain:
        return None

    # find_domain_emails calls Hunter's domain-search endpoint
    # The pattern is in the response data but our client doesn't return it directly
    # We'll infer the pattern from returned emails
    contacts = await find_domain_emails(domain, limit=5)

    if not contacts:
        return None

    # Try to detect pattern from returned emails
    patterns_seen = []
    for c in contacts:
        email = c.get("email")
        first = (c.get("first_name") or "").lower()
        last = (c.get("last_name") or "").lower()

        if not email or not first or not last:
            continue

        local = email.split("@")[0].lower()

        if local == f"{first}.{last}":
            patterns_seen.append("{first}.{last}")
        elif local == f"{first}{last}":
            patterns_seen.append("{first}{last}")
        elif local == f"{first[0]}{last}":
            patterns_seen.append("{f}{last}")
        elif local == f"{first[0]}.{last}":
            patterns_seen.append("{f}.{last}")
        elif local == f"{first}_{last}":
            patterns_seen.append("{first}_{last}")
        elif local == f"{last}.{first}":
            patterns_seen.append("{last}.{first}")
        elif local == f"{first}":
            patterns_seen.append("{first}")

    if patterns_seen:
        # Most common pattern
        from collections import Counter

        most_common = Counter(patterns_seen).most_common(1)[0][0]
        return most_common

    return None


# ── Prospect Contact Enrichment Orchestrator ─────────────────────────


async def enrich_prospect_contacts(
    prospect_id: int,
    db: Session,
    credit_tracker: CreditTracker | None = None,
) -> dict:
    """Orchestrate contact enrichment for a single prospect.

    Flow: Apollo search -> Hunter verify -> classify seniority -> flag new hires
    -> update contacts_preview + enrichment_data

    MASK emails in contacts_preview. Full emails in enrichment_data (revealed after claim).

    Returns: {total_found, verified, decision_makers, new_hires}
    """
    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        logger.warning("enrich_prospect_contacts: prospect {} not found", prospect_id)
        return {"total_found": 0, "verified": 0, "decision_makers": 0, "new_hires": 0}

    tracker = credit_tracker or CreditTracker()

    # Step 1: Apollo people search
    contacts = []
    if tracker.can_use_apollo():
        contacts = await search_contacts_apollo(prospect.domain, max_results=10)
        tracker.use_apollo(1)
    else:
        logger.warning("Apollo credit limit reached — skipping contact search")

    if not contacts:
        return {"total_found": 0, "verified": 0, "decision_makers": 0, "new_hires": 0}

    # Step 2: Verify emails via Hunter
    verification_cache = {}
    # Merge existing cached verifications from enrichment_data
    existing_data = dict(prospect.enrichment_data or {})
    cached_verifications = existing_data.get("email_verifications", {})
    verification_cache.update(cached_verifications)

    for contact in contacts:
        email = contact.get("email")
        if not email:
            continue

        if email in verification_cache:
            continue  # Already verified, skip API call

        if tracker.can_use_hunter_verify():
            await verify_email_hunter(email, verification_cache)
            tracker.use_hunter_verify(1)
        else:
            verification_cache[email] = {
                "email": email,
                "status": "unknown",
                "score": 0,
                "verified": False,
            }
            logger.warning("Hunter verify limit reached — marking {} as unverified", mask_email(email))

    # Step 3: Get domain pattern
    email_pattern = prospect.email_pattern
    if not email_pattern and tracker.can_use_hunter_search():
        email_pattern = await get_domain_pattern_hunter(prospect.domain)
        if email_pattern:
            prospect.email_pattern = email_pattern
        tracker.use_hunter_search(1)

    # Step 4: Build contacts_preview (masked) and enrichment_data (full)
    preview_contacts = []
    full_contacts = []
    stats = {"total_found": len(contacts), "verified": 0, "decision_makers": 0, "new_hires": 0}

    for contact in contacts:
        email = contact.get("email")
        title = contact.get("title", "")
        seniority = contact.get("seniority_level") or classify_contact_seniority(title)
        is_new = _is_new_hire(contact.get("started_current_role_at"))

        # Get verification status
        verified = False
        if email and email in verification_cache:
            verified = verification_cache[email].get("verified", False)

        if verified:
            stats["verified"] += 1
        if seniority == "decision_maker":
            stats["decision_makers"] += 1
        if is_new:
            stats["new_hires"] += 1

        # Preview: masked email, seniority, verified flag
        preview_contacts.append(
            {
                "name": contact.get("name", "Unknown"),
                "title": title,
                "email_masked": mask_email(email) if email else "",
                "seniority": seniority,
                "verified": verified,
                "is_new_hire": is_new,
            }
        )

        # Full: unmasked email (only stored in enrichment_data, revealed after claim)
        full_contacts.append(
            {
                "name": contact.get("name", "Unknown"),
                "title": title,
                "email": email,
                "linkedin_url": contact.get("linkedin_url"),
                "seniority": seniority,
                "verified": verified,
                "is_new_hire": is_new,
                "started_current_role_at": contact.get("started_current_role_at"),
            }
        )

    # Update prospect
    prospect.contacts_preview = preview_contacts
    existing_data["contacts_full"] = full_contacts
    existing_data["email_verifications"] = verification_cache
    existing_data["email_pattern"] = email_pattern
    existing_data["contact_enrichment_at"] = datetime.now(timezone.utc).isoformat()
    prospect.enrichment_data = existing_data
    prospect.last_enriched_at = datetime.now(timezone.utc)
    db.commit()

    logger.info(
        "Contact enrichment for prospect {} ({}): {} found, {} verified, {} DMs, {} new hires",
        prospect_id,
        prospect.domain,
        stats["total_found"],
        stats["verified"],
        stats["decision_makers"],
        stats["new_hires"],
    )

    return stats


# ── Batch Orchestration ──────────────────────────────────────────────


async def run_contact_enrichment_batch(min_fit_score: int = 60) -> dict:
    """Run contact enrichment across qualifying prospects.

    Rate-limit aware: tracks Apollo/Hunter credit usage.
    Stops if approaching credit limits.

    Returns batch summary.
    """
    from app.database import SessionLocal

    db = SessionLocal()
    tracker = CreditTracker()

    try:
        prospects = (
            db.query(ProspectAccount)
            .filter(
                ProspectAccount.status == "suggested",
                ProspectAccount.fit_score >= min_fit_score,
            )
            .order_by(ProspectAccount.fit_score.desc())
            .all()
        )

        summary = {
            "prospects_processed": 0,
            "total_contacts_found": 0,
            "total_verified": 0,
            "total_decision_makers": 0,
            "skipped_already_enriched": 0,
            "skipped_credit_limit": 0,
            "errors": 0,
        }

        for prospect in prospects:
            # Skip already enriched
            existing = prospect.enrichment_data or {}
            if existing.get("contacts_full"):
                summary["skipped_already_enriched"] += 1
                continue

            # Check credit limits
            if not tracker.can_use_apollo():
                summary["skipped_credit_limit"] += (
                    len(prospects) - summary["prospects_processed"] - summary["skipped_already_enriched"]
                )
                logger.warning("Apollo credits exhausted — stopping batch")
                break

            try:
                stats = await enrich_prospect_contacts(prospect.id, db, credit_tracker=tracker)
                summary["prospects_processed"] += 1
                summary["total_contacts_found"] += stats["total_found"]
                summary["total_verified"] += stats["verified"]
                summary["total_decision_makers"] += stats["decision_makers"]
            except Exception as e:
                logger.error("Contact enrichment error for {}: {}", prospect.id, e)
                summary["errors"] += 1

            # Rate limit: Apollo is 5/min on free tier
            await asyncio.sleep(12)

        summary["credits"] = tracker.summary()

        logger.info("Contact enrichment batch complete: {}", summary)
        return summary

    finally:
        db.close()
