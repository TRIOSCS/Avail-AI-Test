"""company_import_service.py — CSV bulk import for companies + contacts.

Parses the uploaded CSV into a preview table (status-flagged, no writes), then creates
rows from the confirmed (already-previewed) payload. Two independent flows sharing one
row-limit cap:

- Companies: name/website/account_type → ``Company`` rows, deduped by
  ``normalize_vendor_name``.
- Contacts: company_name/contact_name/email/phone/role → ``SiteContact`` rows, matched
  to a ``Company`` by normalized name or website domain, attached to (or creating) that
  company's first ACTIVE ``CustomerSite``, deduped by email within the site. Authz:
  a non-manager/admin rep may only import contacts into companies they manage
  (``manageable_company_ids``) — unmatched-authz rows are flagged, not silently skipped,
  so the preview table is honest about what confirm will actually do.

Called by: app.routers.htmx.companies (import_companies_preview/confirm,
    import_contacts_preview/confirm)
Depends on: app.models (Company, CustomerSite, SiteContact),
    app.dependencies (is_manager_or_admin, manageable_company_ids),
    app.vendor_utils (normalize_vendor_name), app.utils.phone (normalize_e164)
"""

import csv
import io
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy.orm import Session

from ..dependencies import is_manager_or_admin, manageable_company_ids
from ..models import Company, User
from ..models.crm import CustomerSite, SiteContact
from ..utils.normalization import parse_website_domain
from ..utils.phone import normalize_e164
from ..vendor_utils import normalize_vendor_name

IMPORT_MAX_ROWS = 1000


def _company_domain(website: str | None) -> str | None:
    """Extract a bare domain (no scheme/www/path) from a company website, or None.

    Delegates to the shared, validated app.utils.normalization.parse_website_domain
    (urlsplit-based; rejects junk like "user@host:8080" instead of naively regexing it
    into a bogus "host:8080" domain) rather than duplicating a narrower ad-hoc regex —
    see that function's docstring for the extraction history.
    """
    if not website:
        return None
    return parse_website_domain(website) or None


def parse_csv_rows(content_bytes: bytes) -> list[dict] | None:
    """Decode + parse an uploaded CSV into a list of raw row dicts.

    Returns ``None`` (rather than raising) on a malformed file — the router renders a
    dedicated "could not parse" partial for that case.
    """
    try:
        text = content_bytes.decode("utf-8", errors="replace")
        return list(csv.DictReader(io.StringIO(text)))
    except Exception:  # noqa: BLE001 — any malformed upload renders the same "could
        # not parse" partial rather than a 500; the router has no more specific
        # recovery to offer regardless of which library call failed.
        logger.warning("CSV import: failed to parse uploaded file", exc_info=True)
        return None


def preview_company_import(db: Session, raw_rows: list[dict]) -> dict:
    """Build the company-import preview table (no writes).

    Raises ``ValueError`` when *raw_rows* exceeds ``IMPORT_MAX_ROWS`` — the router
    maps that to a 400.
    """
    if len(raw_rows) > IMPORT_MAX_ROWS:
        raise ValueError(f"CSV exceeds {IMPORT_MAX_ROWS} row limit")

    existing_norm_names = {
        row[0] for row in db.query(Company.normalized_name).filter(Company.normalized_name.isnot(None)).all()
    }

    rows = []
    for raw in raw_rows:
        name = (raw.get("name") or raw.get("Name") or "").strip()
        website = (raw.get("website") or raw.get("Website") or "").strip()
        account_type = (raw.get("account_type") or raw.get("Account Type") or "").strip()
        norm = normalize_vendor_name(name) if name else None

        if not name:
            status, status_label = "invalid", "Missing name"
        elif norm and norm in existing_norm_names:
            status, status_label = "duplicate", "Already exists"
        else:
            status, status_label = "valid", "OK"

        rows.append(
            {
                "name": name,
                "website": website,
                "account_type": account_type,
                "status": status,
                "status_label": status_label,
            }
        )

    return {
        "rows": rows,
        "valid_count": sum(1 for r in rows if r["status"] == "valid"),
        "dup_count": sum(1 for r in rows if r["status"] == "duplicate"),
        "invalid_count": sum(1 for r in rows if r["status"] == "invalid"),
        "valid_rows": [
            {"name": r["name"], "website": r["website"], "account_type": r["account_type"]}
            for r in rows
            if r["status"] == "valid"
        ],
    }


def confirm_company_import(db: Session, rows: list[dict], user: User) -> dict:
    """Create ``Company`` rows from a confirmed company-import payload.

    Deduplicates by ``normalized_name`` (re-checked against the DB to guard against a
    race since the preview) and sets ``account_owner_id`` to the importing user. Raises
    ``ValueError`` when *rows* exceeds ``IMPORT_MAX_ROWS``.
    """
    if len(rows) > IMPORT_MAX_ROWS:
        raise ValueError(f"rows_json exceeds {IMPORT_MAX_ROWS} row limit")

    existing_norm = {
        row[0] for row in db.query(Company.normalized_name).filter(Company.normalized_name.isnot(None)).all()
    }

    created = 0
    skipped_dup = 0
    skipped_invalid = 0
    now = datetime.now(UTC)

    for row in rows:
        name = str(row.get("name", "")).strip()
        if not name:
            skipped_invalid += 1
            continue
        norm = normalize_vendor_name(name)
        if norm and norm in existing_norm:
            skipped_dup += 1
            continue

        db.add(
            Company(
                name=name,
                website=str(row.get("website", "")).strip() or None,
                account_type=str(row.get("account_type", "")).strip() or None,
                account_owner_id=user.id,
                is_active=True,
                source="import",
                created_at=now,
            )
        )
        if norm:
            existing_norm.add(norm)  # prevent intra-batch duplicates
        created += 1

    if created:
        db.commit()
        logger.info("CSV import: {} companies created by {}", created, user.email)

    parts = [f"Imported {created} compan{'y' if created == 1 else 'ies'}"]
    if skipped_dup:
        parts.append(f"{skipped_dup} duplicate{'s' if skipped_dup != 1 else ''} skipped")
    if skipped_invalid:
        parts.append(f"{skipped_invalid} invalid row{'s' if skipped_invalid != 1 else ''} skipped")

    return {
        "created": created,
        "skipped_dup": skipped_dup,
        "skipped_invalid": skipped_invalid,
        "summary": "; ".join(parts),
    }


def preview_contact_import(db: Session, raw_rows: list[dict], user: User) -> dict:
    """Build the contact-import preview table (no writes).

    Flags duplicate emails (already in ``site_contacts``) and rows whose matched
    company the importing rep doesn't manage (``unauthorized`` — confirm will skip
    these, so the preview says so up front). Raises ``ValueError`` when *raw_rows*
    exceeds ``IMPORT_MAX_ROWS``.
    """
    if len(raw_rows) > IMPORT_MAX_ROWS:
        raise ValueError(f"CSV exceeds {IMPORT_MAX_ROWS} row limit")

    existing_emails = {
        row[0].lower()
        for row in db.query(SiteContact.email).filter(SiteContact.email.isnot(None), SiteContact.email != "").all()
    }

    all_companies = db.query(Company).filter(Company.is_active.is_(True)).all()
    norm_to_company: dict[str, Company] = {}
    domain_to_company: dict[str, Company] = {}
    for co in all_companies:
        if co.normalized_name:
            norm_to_company[co.normalized_name] = co
        domain = _company_domain(co.website)
        if domain:
            domain_to_company[domain] = co

    # Precompute the manageable-company set once (batched) instead of per-row round-trips.
    is_mgr = is_manager_or_admin(user)
    manageable_ids = set() if is_mgr else manageable_company_ids(user, all_companies, db)

    rows = []
    for raw in raw_rows:
        company_name = (raw.get("company_name") or "").strip()
        contact_name = (raw.get("contact_name") or "").strip()
        email = (raw.get("email") or "").strip().lower()
        phone = (raw.get("phone") or "").strip()
        role = (raw.get("role") or "").strip()

        if not company_name or not contact_name:
            status, status_label = "invalid", "Missing required field"
        elif email and email in existing_emails:
            status, status_label = "duplicate", "Email already exists"
        else:
            norm = normalize_vendor_name(company_name)
            matched_co = norm_to_company.get(norm) if norm else None
            if matched_co is None and email and "@" in email:
                matched_co = domain_to_company.get(email.split("@", 1)[1])
            if matched_co is not None and not (is_mgr or matched_co.id in manageable_ids):
                status, status_label = "unauthorized", "Company not yours"
            else:
                status, status_label = "valid", "OK"

        rows.append(
            {
                "company_name": company_name,
                "contact_name": contact_name,
                "email": email,
                "phone": phone,
                "role": role,
                "status": status,
                "status_label": status_label,
            }
        )

    return {
        "rows": rows,
        "valid_count": sum(1 for r in rows if r["status"] == "valid"),
        "dup_count": sum(1 for r in rows if r["status"] == "duplicate"),
        "invalid_count": sum(1 for r in rows if r["status"] == "invalid"),
        "unauthorized_count": sum(1 for r in rows if r["status"] == "unauthorized"),
        "valid_rows": [
            {
                "company_name": r["company_name"],
                "contact_name": r["contact_name"],
                "email": r["email"],
                "phone": r["phone"],
                "role": r["role"],
            }
            for r in rows
            if r["status"] == "valid"
        ],
    }


def confirm_contact_import(db: Session, rows: list[dict], user: User) -> dict:
    """Create ``SiteContact`` rows from a confirmed contacts-import payload.

    Per row: matches company by normalized_name or domain; attaches contact to the
    company's first ACTIVE site (creates an HQ site if none exists); deduplicates by
    email within the site; skips rows whose company isn't found or isn't manageable by
    *user*. Raises ``ValueError`` when *rows* exceeds ``IMPORT_MAX_ROWS``.
    """
    if len(rows) > IMPORT_MAX_ROWS:
        raise ValueError(f"rows_json exceeds {IMPORT_MAX_ROWS} row limit")

    all_companies = db.query(Company).filter(Company.is_active.is_(True)).all()
    norm_to_company: dict[str, Company] = {}
    domain_to_company: dict[str, Company] = {}
    for co in all_companies:
        if co.normalized_name:
            norm_to_company[co.normalized_name] = co
        domain = _company_domain(co.website)
        if domain:
            domain_to_company[domain] = co

    # Precompute the manageable-company set once (batched) instead of per-row round-trips.
    is_mgr = is_manager_or_admin(user)
    manageable_ids = set() if is_mgr else manageable_company_ids(user, all_companies, db)

    now = datetime.now(UTC)
    created = 0
    skipped_no_company = 0
    skipped_dup = 0
    skipped_unauthorized = 0

    # Resolve each row's matched company up front (same normalized-name / domain lookup
    # used below, just hoisted out of the write loop) so the site + dedup lookups can be
    # batched in two queries instead of up to ~2 per row.
    row_companies: list[Company | None] = []
    for row in rows:
        company_name = str(row.get("company_name", "")).strip()
        email = str(row.get("email", "")).strip().lower() or None
        norm = normalize_vendor_name(company_name) if company_name else None
        co = norm_to_company.get(norm) if norm else None
        if co is None and email and "@" in email:
            co = domain_to_company.get(email.split("@", 1)[1])
        row_companies.append(co)

    matched_company_ids = {co.id for co in row_companies if co is not None}

    # Pre-fetch each matched company's first ACTIVE site (ordered by id — same ordering
    # as the per-row ``.order_by(CustomerSite.id).first()`` this replaces) in one query.
    first_site_by_company: dict[int, CustomerSite] = {}
    if matched_company_ids:
        for site in (
            db.query(CustomerSite)
            .filter(CustomerSite.company_id.in_(matched_company_ids), CustomerSite.is_active.is_(True))
            .order_by(CustomerSite.company_id, CustomerSite.id)
            .all()
        ):
            first_site_by_company.setdefault(site.company_id, site)

    # Pre-fetch existing (customer_site_id, email) pairs for those sites in one query.
    # Case is preserved exactly as stored — the per-row dedup this replaces always
    # compared the incoming (already-lowercased) email against the raw stored value, not
    # lower(stored), so we do the same here rather than "fixing" that on the way past.
    existing_site_emails: set[tuple[int, str]] = set()
    site_ids = [s.id for s in first_site_by_company.values()]
    if site_ids:
        existing_site_emails = {
            (cs_id, em)
            for cs_id, em in db.query(SiteContact.customer_site_id, SiteContact.email)
            .filter(SiteContact.customer_site_id.in_(site_ids), SiteContact.email.isnot(None))
            .all()
        }

    for idx, row in enumerate(rows):
        contact_name = str(row.get("contact_name", "")).strip()
        email = str(row.get("email", "")).strip().lower() or None
        phone = str(row.get("phone", "")).strip() or None
        role = str(row.get("role", "")).strip() or None
        company_name = str(row.get("company_name", "")).strip()

        if not company_name or not contact_name:
            skipped_no_company += 1
            continue

        co = row_companies[idx]
        if co is None:
            skipped_no_company += 1
            continue

        # AUTHZ: rep may only attach contacts to companies they manage
        if not (is_mgr or co.id in manageable_ids):
            skipped_unauthorized += 1
            continue

        # Find or create the first ACTIVE site for this company — reuse the cached site
        # (pre-fetched above, or created for an earlier row of the same company in this
        # same batch) instead of re-querying.
        site = first_site_by_company.get(co.id)
        if site is None:
            site = CustomerSite(company_id=co.id, site_name="HQ", is_active=True, created_at=now)
            db.add(site)
            db.flush()  # get site.id
            first_site_by_company[co.id] = site

        # Deduplicate by email within the site — check the pre-fetched set (updated
        # in-loop as contacts are created) instead of querying per row.
        if email and (site.id, email) in existing_site_emails:
            skipped_dup += 1
            continue

        contact = SiteContact(
            customer_site_id=site.id,
            full_name=contact_name,
            email=email,
            phone=normalize_e164(phone) if phone else None,
            contact_role=role,
            is_active=True,
            created_at=now,
        )
        db.add(contact)
        created += 1
        if email:
            existing_site_emails.add((site.id, email))

    if created:
        db.commit()
        logger.info("Contact CSV import: {} contacts created by {}", created, user.email)

    parts = [f"Imported {created} contact{'s' if created != 1 else ''}"]
    if skipped_no_company:
        parts.append(f"{skipped_no_company} skipped (company not found)")
    if skipped_dup:
        parts.append(f"{skipped_dup} duplicate{'s' if skipped_dup != 1 else ''} skipped")
    if skipped_unauthorized:
        parts.append(f"{skipped_unauthorized} skipped — not your account{'s' if skipped_unauthorized != 1 else ''}")

    return {
        "created": created,
        "skipped_no_company": skipped_no_company,
        "skipped_dup": skipped_dup,
        "skipped_unauthorized": skipped_unauthorized,
        "summary": "; ".join(parts),
    }
