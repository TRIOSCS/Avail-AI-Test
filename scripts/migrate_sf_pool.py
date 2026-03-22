"""Migrate unowned Salesforce companies into prospect_accounts.

Copies unowned, non-dismissed companies with valid domains from the companies
table into prospect_accounts with salesforce_import as the discovery_source.

Called by: manual invocation, tests/test_models/test_prospect_account.py
Depends on: app.models, app.database
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Company, CustomerSite
from app.models.discovery_batch import DiscoveryBatch
from app.models.prospect_account import ProspectAccount


def normalize_domain(raw: str | None) -> str | None:
    """Normalize a domain string: lowercase, strip protocol/www/whitespace."""
    if not raw or not raw.strip():
        return None
    d = raw.strip().lower()
    # Remove protocol
    for prefix in ("https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix) :]
    # Remove trailing slash
    d = d.rstrip("/")
    # Remove www.
    if d.startswith("www."):
        d = d[4:]
    return d or None


def migrate(dry_run: bool = True) -> dict:
    """Migrate unowned SF pool companies to prospect_accounts.

    Args:
        dry_run: If True, log what would be done but don't write.

    Returns:
        Summary dict with counts.
    """
    db: Session = SessionLocal()
    try:
        # Find companies that have an owner via CustomerSite
        owned_company_ids = (
            db.query(CustomerSite.company_id).filter(CustomerSite.owner_id.isnot(None)).distinct().subquery()
        )

        # Pool = unowned, active, not dismissed
        pool = (
            db.query(Company)
            .filter(
                Company.is_active.is_(True),
                Company.id.notin_(owned_company_ids),
                or_(Company.import_priority != "dismissed", Company.import_priority.is_(None)),
            )
            .all()
        )

        # Create a discovery batch for this migration
        batch = None
        if not dry_run and pool:
            import uuid

            batch = DiscoveryBatch(
                batch_id=f"sf_migration_{uuid.uuid4().hex[:12]}",
                source="salesforce_import",
                status="completed",
                started_at=datetime.now(timezone.utc),
                completed_at=datetime.now(timezone.utc),
            )
            db.add(batch)
            db.flush()

        total_pool = len(pool)
        migrated = 0
        skipped_no_domain = 0
        skipped_duplicate = 0

        for co in pool:
            domain = normalize_domain(co.domain)
            if not domain:
                skipped_no_domain += 1
                continue

            # Check for existing prospect with same domain
            existing = db.query(ProspectAccount).filter(ProspectAccount.domain == domain).first()
            if existing:
                skipped_duplicate += 1
                continue

            if not dry_run:
                historical = {}
                if co.sf_account_id:
                    historical["sf_account_id"] = co.sf_account_id

                pa = ProspectAccount(
                    name=co.name,
                    domain=domain,
                    industry=co.industry,
                    company_id=co.id,
                    discovery_source="salesforce_import",
                    discovery_batch_id=batch.id if batch else None,
                    import_priority=co.import_priority,
                    historical_context=historical if historical else None,
                )
                db.add(pa)

            migrated += 1

        if not dry_run:
            db.commit()

        result = {
            "total_pool": total_pool,
            "migrated": migrated,
            "skipped_no_domain": skipped_no_domain,
            "skipped_duplicate": skipped_duplicate,
        }
        logger.info("SF pool migration: {}", result)
        return result
    except Exception:
        db.rollback()
        raise


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually write to DB")
    args = parser.parse_args()

    result = migrate(dry_run=not args.apply)
    print(result)
