"""Website scraper — extract vendor contact emails from their websites."""

import asyncio
import logging
import re
from datetime import datetime, timezone

import httpx
from sqlalchemy import func

from ..http_client import http_redirect
from sqlalchemy.orm import Session

from ..models import VendorCard, VendorContact
from ..vendor_utils import merge_emails_into_card

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

# Emails matching these prefixes get lower confidence
GENERIC_PREFIXES = {
    "noreply", "no-reply", "no_reply", "donotreply", "support", "info",
    "admin", "webmaster", "postmaster", "mailer-daemon", "help", "abuse",
    "newsletter", "marketing", "notifications", "alerts", "bounces",
}

# Pages to try scraping
CONTACT_PATHS = ["", "/contact", "/contact-us", "/about", "/about-us"]

RATE_LIMIT_DELAY = 0.5  # seconds between requests
MAX_VENDORS_DEFAULT = 500
TIMEOUT = 10


def _classify_email(email: str, page_path: str) -> int:
    """Return confidence score (40-70) for an extracted email."""
    local = email.split("@")[0].lower()

    # Generic emails get lower confidence
    for prefix in GENERIC_PREFIXES:
        if local == prefix or local.startswith(prefix + ".") or local.startswith(prefix + "+"):
            return 40

    # Emails from /contact pages get higher confidence
    if "/contact" in page_path:
        return 70

    # Emails from /about pages get moderate confidence
    if "/about" in page_path:
        return 60

    # Homepage emails get base confidence
    return 55


async def _fetch_page(client: httpx.AsyncClient, url: str) -> str | None:
    """Fetch a single page, return text content or None."""
    try:
        r = await client.get(url, follow_redirects=True)
        if r.status_code == 200 and "text" in r.headers.get("content-type", ""):
            return r.text[:500_000]  # Cap at 500KB
    except Exception:
        pass
    return None


async def _scrape_vendor(client: httpx.AsyncClient, website: str) -> list[dict]:
    """Scrape a vendor website for email addresses. Returns list of {email, confidence}."""
    # Normalize the website URL
    if not website.startswith(("http://", "https://")):
        website = "https://" + website
    website = website.rstrip("/")

    results = []
    seen_emails = set()

    # Fetch all contact pages in parallel
    urls_and_paths = [(website + path, path) for path in CONTACT_PATHS]
    pages = await asyncio.gather(
        *[_fetch_page(client, url) for url, _ in urls_and_paths],
        return_exceptions=True,
    )

    for (_, path), html in zip(urls_and_paths, pages):
        if isinstance(html, Exception) or not html:
            continue

        emails = EMAIL_RE.findall(html)
        for email in emails:
            email_lower = email.strip().lower()
            if email_lower.endswith((".png", ".jpg", ".gif", ".svg", ".css", ".js")):
                continue
            if email_lower in seen_emails:
                continue
            seen_emails.add(email_lower)
            confidence = _classify_email(email_lower, path)
            results.append({"email": email_lower, "confidence": confidence})

    return results


async def scrape_vendor_websites(
    db: Session,
    max_vendors: int = MAX_VENDORS_DEFAULT,
) -> dict:
    """Scrape vendor websites for contact emails.

    Targets vendors with a website URL but fewer than 2 email contacts.
    """
    # Find vendors with websites but few contacts
    contact_counts = (
        db.query(VendorContact.vendor_card_id, func.count(VendorContact.id))
        .filter(VendorContact.email.isnot(None))
        .group_by(VendorContact.vendor_card_id)
        .subquery()
    )

    vendors = (
        db.query(VendorCard)
        .outerjoin(contact_counts, VendorCard.id == contact_counts.c.vendor_card_id)
        .filter(
            VendorCard.website.isnot(None),
            VendorCard.website != "",
            VendorCard.is_blacklisted == False,  # noqa: E712
        )
        .filter(
            (contact_counts.c[1] == None) | (contact_counts.c[1] < 2)  # noqa: E711
        )
        .limit(max_vendors)
        .all()
    )

    if not vendors:
        return {"vendors_scraped": 0, "emails_found": 0}

    log.info("Website scraper: processing %d vendors", len(vendors))

    vendors_scraped = 0
    emails_found = 0

    # Process vendors in batches of 10 concurrently
    sem = asyncio.Semaphore(10)

    async def _scrape_one(card):
        async with sem:
            if not card.website:
                return None
            try:
                scrape_results = await _scrape_vendor(http_redirect, card.website)
                await asyncio.sleep(RATE_LIMIT_DELAY)  # Rate limit between vendors
                return (card, scrape_results)
            except Exception as e:
                log.debug("Scrape failed for %s: %s", card.website, e)
                return None

    scrape_results_list = await asyncio.gather(
        *[_scrape_one(c) for c in vendors], return_exceptions=True
    )

    for result in scrape_results_list:
        if isinstance(result, Exception) or result is None:
            continue

        card, results = result
        vendors_scraped += 1

        if not results:
            continue

        new_emails = [r["email"] for r in results]
        merge_emails_into_card(card, new_emails)

        for r in results:
            email = r["email"]
            existing = (
                db.query(VendorContact)
                .filter_by(vendor_card_id=card.id, email=email)
                .first()
            )
            if existing:
                continue

            vc = VendorContact(
                vendor_card_id=card.id,
                email=email,
                source="website_scrape",
                confidence=r["confidence"],
                contact_type="company",
            )
            db.add(vc)
            emails_found += 1

        if vendors_scraped % 50 == 0:
            try:
                db.commit()
            except Exception as e:
                log.warning("Website scraper periodic commit failed: %s", e)
                db.rollback()

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        log.error("Website scraper final commit failed: %s", e)

    log.info("Website scraper complete: %d vendors scraped, %d emails found", vendors_scraped, emails_found)
    return {
        "vendors_scraped": vendors_scraped,
        "emails_found": emails_found,
    }
