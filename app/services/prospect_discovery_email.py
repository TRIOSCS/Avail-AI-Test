"""Email history mining — discover prospect companies from inbox patterns.

Scans Trio's inbox for domains that emailed the team but are not existing customers,
vendors, or already-discovered prospects. Catches companies that Explorium might miss.
"""

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Company, VendorCard
from app.models.prospect_account import ProspectAccount
from app.schemas.prospect_account import ProspectAccountCreate

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
    customer_domains = {
        row[0].lower()
        for row in db.query(Company.domain)
        .filter(
            Company.domain.isnot(None),
            Company.account_owner_id.isnot(None),
        )
        .all()
        if row[0]
    }

    vendor_domains = set()
    for row in db.query(VendorCard.emails).filter(VendorCard.emails.isnot(None)).limit(5000).all():
        for email in row[0] or []:
            d = _normalize_domain(email)
            if d:
                vendor_domains.add(d)

    prospect_domains = {row[0].lower() for row in db.query(ProspectAccount.domain).limit(5000).all() if row[0]}

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

            info = domain_counts.setdefault(
                domain,
                {"domain": domain, "email_count": 0, "sample_senders": []},
            )
            info["email_count"] += 1
            name = sender.get("name", "")
            if name and len(info["sample_senders"]) < 3:
                info["sample_senders"].append({"name": name, "email": email})

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


def _map_explorium_to_prospect(c: dict) -> dict:
    """Map an Explorium CRM firmographic dict to the prospect-account shape.

    Explorium's connector returns the company shape (legal_name, employee_size,
    hq_city/state/country, naics, ...); prospects use name/employee_count_range/
    hq_location/region/naics_code. Reuses the prospect-discovery location + region
    helpers (they read the hq_* keys directly).
    """
    from app.services.prospect_discovery_explorium import _build_location, _detect_region

    return {
        "name": c.get("legal_name"),
        "industry": c.get("industry"),
        "naics_code": c.get("naics"),
        "employee_count_range": c.get("employee_size"),
        "revenue_range": c.get("revenue_range"),
        "website": c.get("website"),
        "hq_location": _build_location(c),
        "region": _detect_region(c),
        "description": c.get("description"),
        "discovery_source": "explorium",
    }


async def _explorium_domain_enrich(domain: str) -> dict | None:
    """Eager enrich_fn for email mining: one Explorium domain match → prospect shape.

    Self-gating: returns None (→ the domain stays a bare prospect) when Explorium is
    disabled, the credential is missing, or the explorium circuit is open. A
    ProviderQuotaError trips the circuit and returns None. Explorium-only by design —
    no Clay/Lusha — so cost per batch is bounded.
    """
    from app.connectors import explorium
    from app.services import enrichment_credit_guard as cg
    from app.services.credential_service import get_credential_cached

    if not settings.explorium_enrichment_enabled or cg.circuit_open("explorium"):
        return None
    api_key = get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY")
    if not api_key:
        return None
    try:
        company = await explorium.enrich_company(domain, "", api_key)
    except cg.ProviderQuotaError:
        cg.trip_circuit("explorium", settings.explorium_cooldown_minutes)
        return None
    if not company:
        return None
    return _map_explorium_to_prospect(company)


async def enrich_email_domains(
    domains: list[dict],
    enrich_fn=None,
    enrich_cap: int = 25,
) -> list[ProspectAccountCreate]:
    """Turn mined domains into prospect accounts (hybrid: signal-always + capped
    enrich).

    Args:
        domains: list of {domain, email_count, sample_senders}, sorted by email volume.
        enrich_fn: async (domain) -> dict|None firmographics in the prospect shape.
        enrich_cap: how many domains (the highest-volume first) to eagerly enrich.

    Every domain becomes a ProspectAccountCreate from the email signal alone; the first
    *enrich_cap* domains additionally get enrich_fn merged over the base when it returns
    data. Domains past the cap, and enrichment misses, are created unenriched and stay
    enrichable on demand later. No domain is dropped.
    """
    prospects: list[ProspectAccountCreate] = []
    enriched_count = 0

    for index, d_info in enumerate(domains):
        domain = d_info["domain"]

        company_data: dict = {}
        if enrich_fn and index < enrich_cap:
            try:
                company_data = await enrich_fn(domain) or {}
            except Exception as e:
                logger.warning("Email-mining enrich failed for {}: {}", domain, e)
                company_data = {}
            if company_data:
                enriched_count += 1

        # Fit/readiness scoring is computed at persist time (_persist_discovery_results)
        # from the persisted prospect fields, so no scoring is done here.
        prospects.append(
            ProspectAccountCreate(
                name=company_data.get("name") or domain,
                domain=domain,
                website=company_data.get("website") or f"https://{domain}",
                industry=company_data.get("industry"),
                naics_code=company_data.get("naics_code"),
                employee_count_range=company_data.get("employee_count_range"),
                revenue_range=company_data.get("revenue_range"),
                hq_location=company_data.get("hq_location"),
                region=company_data.get("region"),
                description=company_data.get("description"),
                discovery_source="email_history",
                enrichment_data={
                    "email_mining": {
                        "email_count": d_info["email_count"],
                        "sample_senders": d_info["sample_senders"],
                    },
                    "enrichment_source": company_data.get("discovery_source") if company_data else None,
                },
            )
        )

    logger.info(
        "Email domain enrichment: {} domains → {} prospects ({} eagerly enriched, cap {})",
        len(domains),
        len(prospects),
        enriched_count,
        enrich_cap,
    )

    return prospects


async def run_email_mining_batch(
    batch_id: str,
    graph_client,
    db: Session,
    enrich_fn=None,
    enrich_cap: int | None = None,
    days_back: int = 90,
) -> list[ProspectAccountCreate]:
    """Full email mining pipeline: scan -> build prospects (signal + capped enrich).

    Args:
        batch_id: human-readable batch identifier
        graph_client: Graph API client
        db: database session
        enrich_fn: async (domain) -> dict|None firmographics in the prospect shape
        enrich_cap: max domains to eagerly enrich; defaults to settings.email_mining_enrich_cap
        days_back: inbox lookback period
    """
    logger.info("Starting email mining batch: {}", batch_id)
    if enrich_cap is None:
        enrich_cap = settings.email_mining_enrich_cap

    # Step 1: Mine unknown domains
    domains = await mine_unknown_domains(graph_client, db, days_back)

    if not domains:
        logger.info("Email mining batch {}: no unknown domains found", batch_id)
        return []

    # Step 2: Build prospects (signal-always; top enrich_cap eagerly Explorium-enriched)
    prospects = await enrich_email_domains(domains, enrich_fn=enrich_fn, enrich_cap=enrich_cap)

    logger.info(
        "Email mining batch {}: {} domains mined, {} prospects created",
        batch_id,
        len(domains),
        len(prospects),
    )

    return prospects
