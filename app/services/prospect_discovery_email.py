"""Email history mining — discover prospect companies from inbox patterns.

Scans Trio's inbox for domains that emailed the team but are not existing
customers, vendors, or already-discovered prospects. Catches companies
that Explorium might miss.
"""

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Company, VendorCard
from app.models.prospect_account import ProspectAccount
from app.schemas.prospect_account import ProspectAccountCreate
from app.services.prospect_scoring import calculate_fit_score, calculate_readiness_score

# Common freemail domains to exclude
FREEMAIL_DOMAINS = frozenset(
    {
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
        "comcast.net",
        "att.net",
        "verizon.net",
        "sbcglobal.net",
        "cox.net",
        "charter.net",
        "googlemail.com",
        "yahoo.co.uk",
        "hotmail.co.uk",
    }
)

# Internal domains to exclude
INTERNAL_DOMAINS = frozenset(settings.own_domains) if hasattr(settings, "own_domains") else frozenset({"trioscs.com"})


def _normalize_domain(email: str) -> str | None:
    """Extract and normalize domain from email address."""
    if not email or "@" not in email:
        return None
    domain = email.split("@", 1)[1].strip().lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain if domain else None


async def mine_unknown_domains(
    graph_client,
    db: Session,
    days_back: int = 90,
) -> list[dict]:
    """Extract unknown company domains from recent inbox emails.

    Args:
        graph_client: Microsoft Graph API client (for inbox access)
        db: database session
        days_back: how far back to scan

    Returns list of dicts: [{domain, email_count, sample_senders}]
    """
    # Get known domains to exclude
    customer_domains = set(
        row[0].lower()
        for row in db.query(Company.domain)
        .filter(
            Company.domain.isnot(None),
            Company.account_owner_id.isnot(None),
        )
        .all()
        if row[0]
    )

    vendor_domains = set()
    for row in db.query(VendorCard.emails).filter(VendorCard.emails.isnot(None)).limit(5000).all():
        if row[0]:
            for email in row[0]:
                d = _normalize_domain(email)
                if d:
                    vendor_domains.add(d)

    prospect_domains = set(row[0].lower() for row in db.query(ProspectAccount.domain).limit(5000).all() if row[0])

    exclude_domains = customer_domains | vendor_domains | prospect_domains | FREEMAIL_DOMAINS | INTERNAL_DOMAINS

    # Scan inbox via Graph API
    since = datetime.now(timezone.utc) - timedelta(days=days_back)
    domain_counts: dict[str, dict] = {}

    try:
        messages = await graph_client.list_messages(
            folder="inbox",
            since=since,
            top=1000,
            select=["from", "receivedDateTime"],
        )

        for msg in messages:
            sender = msg.get("from", {}).get("emailAddress", {})
            email = (sender.get("address") or "").lower()
            domain = _normalize_domain(email)

            if not domain or domain in exclude_domains:
                continue

            if domain not in domain_counts:
                domain_counts[domain] = {
                    "domain": domain,
                    "email_count": 0,
                    "sample_senders": [],
                }

            domain_counts[domain]["email_count"] += 1
            name = sender.get("name", "")
            if name and len(domain_counts[domain]["sample_senders"]) < 3:
                domain_counts[domain]["sample_senders"].append(
                    {
                        "name": name,
                        "email": email,
                    }
                )

    except Exception as e:
        logger.error("Email mining Graph API error: {}", e)
        return []

    # Only return domains with 2+ emails (not spam)
    results = [info for info in domain_counts.values() if info["email_count"] >= 2]

    # Sort by frequency
    results.sort(key=lambda x: x["email_count"], reverse=True)

    logger.info(
        "Email mining: scanned {} days, found {} unique unknown domains (2+ emails)",
        days_back,
        len(results),
    )

    return results


async def enrich_email_domains(
    domains: list[dict],
    enrich_fn=None,
    apollo_enrich_fn=None,
) -> list[ProspectAccountCreate]:
    """Enrich unknown domains with company data from Explorium or Apollo.

    Args:
        domains: list of {domain, email_count, sample_senders}
        enrich_fn: async function(domain) -> dict|None (Explorium match-business)
        apollo_enrich_fn: async function(domain) -> dict|None (Apollo fallback)

    Returns list of ProspectAccountCreate schemas.
    """
    prospects: list[ProspectAccountCreate] = []

    for d_info in domains:
        domain = d_info["domain"]
        company_data = None

        # Try primary enrichment (Explorium)
        if enrich_fn:
            try:
                company_data = await enrich_fn(domain)
            except Exception as e:
                logger.warning("Explorium enrich failed for {}: {}", domain, e)

        # Fallback to Apollo
        if not company_data and apollo_enrich_fn:
            try:
                company_data = await apollo_enrich_fn(domain)
            except Exception as e:
                logger.warning("Apollo enrich failed for {}: {}", domain, e)

        if not company_data:
            logger.debug("Skip {}: no enrichment data available", domain)
            continue

        # Score
        fit_data = {
            "name": company_data.get("name", domain),
            "industry": company_data.get("industry"),
            "naics_code": company_data.get("naics_code"),
            "employee_count_range": company_data.get("employee_count_range"),
            "region": company_data.get("region"),
        }
        fit_score, fit_reasoning = calculate_fit_score(fit_data)
        readiness_score, _ = calculate_readiness_score(fit_data, {})

        prospect = ProspectAccountCreate(
            name=company_data.get("name") or domain,
            domain=domain,
            website=company_data.get("website") or f"https://{domain}",
            industry=company_data.get("industry"),
            naics_code=company_data.get("naics_code"),
            employee_count_range=company_data.get("employee_count_range"),
            hq_location=company_data.get("hq_location"),
            region=company_data.get("region"),
            description=company_data.get("description"),
            discovery_source="email_history",
            enrichment_data={
                "email_mining": {
                    "email_count": d_info["email_count"],
                    "sample_senders": d_info["sample_senders"],
                },
                "enrichment_source": company_data.get("discovery_source", "unknown"),
            },
        )
        prospects.append(prospect)

    logger.info(
        "Email domain enrichment: {} domains submitted, {} enriched",
        len(domains),
        len(prospects),
    )

    return prospects


async def run_email_mining_batch(
    batch_id: str,
    graph_client,
    db: Session,
    enrich_fn=None,
    apollo_enrich_fn=None,
    days_back: int = 90,
) -> list[ProspectAccountCreate]:
    """Full email mining pipeline: scan -> enrich -> dedup -> score.

    Args:
        batch_id: human-readable batch identifier
        graph_client: Graph API client
        db: database session
        enrich_fn: Explorium enrichment function
        apollo_enrich_fn: Apollo enrichment fallback
        days_back: inbox lookback period
    """
    logger.info("Starting email mining batch: {}", batch_id)

    # Step 1: Mine unknown domains
    domains = await mine_unknown_domains(graph_client, db, days_back)

    if not domains:
        logger.info("Email mining batch {}: no unknown domains found", batch_id)
        return []

    # Step 2: Enrich
    prospects = await enrich_email_domains(
        domains,
        enrich_fn=enrich_fn,
        apollo_enrich_fn=apollo_enrich_fn,
    )

    logger.info(
        "Email mining batch {}: {} domains mined, {} prospects created",
        batch_id,
        len(domains),
        len(prospects),
    )

    return prospects
