"""One-time migration: copy unowned companies into prospect_accounts.

A company is "unowned" if NONE of its customer_sites have an owner_id set.
Companies consolidated by account — each site can have its own account manager.

Usage:
    python scripts/migrate_sf_pool.py --dry-run   # Preview without writing
    python scripts/migrate_sf_pool.py              # Execute migration

Idempotent — skips domains already in prospect_accounts (UNIQUE constraint).
"""

import argparse
import re
import sys
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import and_, exists

# Add project root to path for imports
sys.path.insert(0, "/root/availai")

from app.database import SessionLocal
from app.models import Company
from app.models.crm import CustomerSite
from app.models.discovery_batch import DiscoveryBatch
from app.models.prospect_account import ProspectAccount


def normalize_domain(raw: str | None) -> str | None:
    """Lowercase, strip www.

    prefix, strip trailing slashes/spaces.
    """
    if not raw:
        return None
    d = raw.strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = re.sub(r"^www\.", "", d)
    d = d.rstrip("/").strip()
    return d if d else None


def migrate(dry_run: bool = False) -> dict:
    """Migrate unowned companies into prospect_accounts.

    A company is unowned if no CustomerSite under it has an owner_id.
    This handles the consolidated account model where each site has
    its own account manager.

    Returns summary dict: {migrated, skipped_no_domain, skipped_duplicate, skipped_has_owner, total_candidates}.
    """
    db = SessionLocal()
    try:
        # Subquery: companies that have at least one site with an owner
        has_owned_site = exists().where(
            and_(
                CustomerSite.company_id == Company.id,
                CustomerSite.owner_id.isnot(None),
            )
        )

        # Pool = active companies where NO site has an owner, not dismissed
        pool_companies = (
            db.query(Company)
            .filter(
                Company.is_active.is_(True),
                ~has_owned_site,
                (Company.import_priority != "dismissed") | (Company.import_priority.is_(None)),
            )
            .all()
        )

        total_pool = len(pool_companies)
        logger.info(f"Found {total_pool} pool companies to process")

        # Get existing prospect domains for dedup
        existing_domains = set(row[0] for row in db.query(ProspectAccount.domain).all())

        # Create a discovery batch for this migration
        batch = None
        if not dry_run:
            batch = DiscoveryBatch(
                batch_id=f"sf-pool-migration-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')}",
                source="salesforce_import",
                status="running",
                started_at=datetime.now(timezone.utc),
            )
            db.add(batch)
            db.flush()

        migrated = 0
        skipped_no_domain = 0
        skipped_duplicate = 0

        for co in pool_companies:
            domain = normalize_domain(co.domain)

            if not domain:
                skipped_no_domain += 1
                logger.warning(f"SKIP (no domain): id={co.id} name={co.name!r}")
                continue

            if domain in existing_domains:
                skipped_duplicate += 1
                logger.debug(f"SKIP (duplicate): id={co.id} domain={domain}")
                continue

            if dry_run:
                logger.info(
                    f"DRY-RUN would migrate: id={co.id} name={co.name!r} domain={domain} priority={co.import_priority}"
                )
                migrated += 1
                existing_domains.add(domain)
                continue

            prospect = ProspectAccount(
                name=co.name,
                domain=domain,
                website=co.website,
                industry=co.industry,
                discovery_source="salesforce_import",
                discovery_batch_id=batch.id,
                status="suggested",
                company_id=co.id,
                import_priority=co.import_priority,
                historical_context={"sf_account_id": co.sf_account_id},
                hq_location=", ".join(filter(None, [co.hq_city, co.hq_state, co.hq_country])) or None,
                employee_count_range=co.employee_size,
                fit_score=0,
                readiness_score=0,
            )
            db.add(prospect)
            existing_domains.add(domain)
            migrated += 1
            logger.info(f"MIGRATED: id={co.id} name={co.name!r} domain={domain} priority={co.import_priority}")

        if not dry_run and batch:
            batch.status = "complete"
            batch.prospects_found = total_pool
            batch.prospects_new = migrated
            batch.completed_at = datetime.now(timezone.utc)
            db.commit()
            logger.info("Migration committed to database")
        else:
            logger.info("DRY-RUN complete — no data written")

        summary = {
            "migrated": migrated,
            "skipped_no_domain": skipped_no_domain,
            "skipped_duplicate": skipped_duplicate,
            "total_pool": total_pool,
        }
        logger.info(f"Summary: {summary}")
        return summary

    except Exception:
        db.rollback()
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate SF pool into prospect_accounts")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview migration without writing to DB",
    )
    args = parser.parse_args()
    migrate(dry_run=args.dry_run)
