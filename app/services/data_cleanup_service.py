"""data_cleanup_service.py — Production data remediation and quarantine service.

Purpose: Identifies and quarantines polluted placeholder/test/security-payload
         records from production views without permanently deleting data.
         Supports incremental cleanup with dry-run mode.

Called by: admin endpoints, scheduler (future)
Depends on: models (Requisition, Offer, VendorCard, etc.), database session
"""

import re
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

# Patterns that indicate test/junk data
_TEST_PATTERNS = [
    re.compile(r"^test[\s_-]", re.IGNORECASE),
    re.compile(r"^xxx|^zzz|^aaa", re.IGNORECASE),
    re.compile(r"<script", re.IGNORECASE),
    re.compile(r"javascript:", re.IGNORECASE),
    re.compile(r"onerror\s*=", re.IGNORECASE),
    re.compile(r"^placeholder", re.IGNORECASE),
    re.compile(r"^dummy", re.IGNORECASE),
    re.compile(r"^fake", re.IGNORECASE),
    re.compile(r"^sample[\s_-]", re.IGNORECASE),
]

# Sentinel dates that indicate bad data
_SENTINEL_DATES = [
    datetime(1970, 1, 1, tzinfo=timezone.utc),
    datetime(1, 1, 1, tzinfo=timezone.utc),
    datetime(9999, 12, 31, tzinfo=timezone.utc),
]


def _is_test_data(text: str | None) -> bool:
    """Check if a text field matches known test/junk/XSS patterns."""
    if not text:
        return False
    return any(p.search(text) for p in _TEST_PATTERNS)


def scan_junk_data(db: Session, *, dry_run: bool = True) -> dict:
    """Scan for test/junk/XSS data across core tables.

    Returns a summary of flagged records. In dry_run mode, no changes are made. When
    dry_run=False, flagged records are quarantined (status set to 'quarantined' or notes
    prefixed with [QUARANTINED]).
    """
    from app.models import Offer, Requisition, VendorCard

    results = {
        "requisitions": [],
        "offers": [],
        "vendor_cards": [],
        "total_flagged": 0,
        "dry_run": dry_run,
    }

    # Scan requisitions
    reqs = db.query(Requisition).filter(Requisition.status != "archived").limit(5000).all()
    for r in reqs:
        if _is_test_data(r.name) or _is_test_data(r.customer_name):
            results["requisitions"].append(
                {
                    "id": r.id,
                    "name": r.name,
                    "reason": "matches test/junk pattern",
                }
            )
            if not dry_run:
                r.status = "archived"
                r.name = f"[QUARANTINED] {r.name}"
            results["total_flagged"] += 1

    # Scan offers for XSS payloads
    offers = db.query(Offer).filter(Offer.status == "active").limit(10000).all()
    for o in offers:
        flagged_fields = []
        for field in ("vendor_name", "mpn", "notes", "manufacturer"):
            val = getattr(o, field, None)
            if _is_test_data(val):
                flagged_fields.append(field)
        if flagged_fields:
            results["offers"].append(
                {
                    "id": o.id,
                    "mpn": o.mpn,
                    "vendor_name": o.vendor_name,
                    "flagged_fields": flagged_fields,
                    "reason": "contains test/junk/XSS content",
                }
            )
            if not dry_run:
                o.status = "rejected"
                o.notes = f"[QUARANTINED: {', '.join(flagged_fields)}] {o.notes or ''}"
            results["total_flagged"] += 1

    # Scan vendor cards
    cards = db.query(VendorCard).limit(5000).all()
    for c in cards:
        if _is_test_data(c.display_name):
            results["vendor_cards"].append(
                {
                    "id": c.id,
                    "display_name": c.display_name,
                    "reason": "matches test/junk pattern",
                }
            )
            if not dry_run:
                c.is_blacklisted = True
                c.display_name = f"[QUARANTINED] {c.display_name}"
            results["total_flagged"] += 1

    if not dry_run:
        db.commit()
        logger.info("Data cleanup: quarantined {} records", results["total_flagged"])
    else:
        logger.info("Data cleanup dry run: found {} flaggable records", results["total_flagged"])

    return results


def fix_sentinel_dates(db: Session, *, dry_run: bool = True) -> dict:
    """Find and null out sentinel/invalid dates (1970-01-01, 0001-01-01, etc).

    These typically come from bad data imports or default values.
    """
    from app.models import Offer, Requisition

    fixed = {"offers": 0, "requisitions": 0, "dry_run": dry_run}

    for Model, date_fields in [
        (Requisition, ["deadline", "created_at", "updated_at"]),
        (Offer, ["valid_until", "created_at", "updated_at"]),
    ]:
        for field_name in date_fields:
            col = getattr(Model, field_name, None)
            if col is None:
                continue
            for sentinel in _SENTINEL_DATES:
                rows = db.query(Model).filter(col == sentinel).limit(1000).all()
                for row in rows:
                    if not dry_run:
                        # Don't null out created_at, just fix sentinel values
                        if field_name == "created_at":
                            setattr(row, field_name, datetime.now(timezone.utc))
                        else:
                            setattr(row, field_name, None)
                    table_key = Model.__tablename__
                    if table_key not in fixed:
                        fixed[table_key] = 0
                    fixed[table_key] += 1

    if not dry_run:
        db.commit()

    return fixed
