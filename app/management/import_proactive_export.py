"""One-shot seed of proactive demand/supply history from the Salesforce export.

Usage: python -m app.management.import_proactive_export [--apply]
       [--requirements tests/fixtures/proactive_sf_export/requirements.csv]
       [--availabilities tests/fixtures/proactive_sf_export/availabilities.csv]
       [--actor-email admin@example.com]

DRY-RUN by DEFAULT (no writes — pass --apply to write). Consumes the two CSVs
extracted from the "Requirements (2YR)/Availabilities (7DAY)" Salesforce joined
report (2026-08-06 spec, appendix A):

  requirements.csv → Company (get-or-create by exact account name) + a primary
  CustomerSite + ONE Requisition per SF Req Item (name = the ReqItem# — the
  idempotency key) + its Requirement carrying the VERBATIM material name and
  the original ask date. Rep names map to users.name case-insensitively;
  unmatched reps fall back to the actor and are logged. Status maps:
  requisition Requirement→open, Quoted→quoted, Won→won, Archived→cancelled;
  per-part sourcing_status identical except Archived→archived.

  availabilities.csv → one live Offer per SF Sourcing Item (idempotency key =
  the SRC# stored in Offer.notes), vendor name + qty + outright price +
  original created date, source="salesforce_import".

Material cards are linked by normalize_mpn_key when a card exists — enrichment
only, never a matching prerequisite. Part spelling variants are imported
verbatim (LTSR15-NP vs "LTSR 15-NP" stay separate; the digest's
duplicate-materials section flags them for a human).

ONE-SHOT by design — NO cron, NO scheduler hook: the export is a static
snapshot; re-running with a newer extract is the explicit operator step.

Called by: an operator (manually, after migration 205) and
    tests/test_proactive_validation.py (the three hand-checked lines).
Depends on: app.database.SessionLocal, app.models (Company, CustomerSite,
    Requisition, Requirement, Offer, User, MaterialCard),
    app.utils.normalization.normalize_mpn_key,
    app.services.proactive_matching (scan-watermark rewind so the backdated
    imported offers stay visible to the proactive batch scan).
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, MaterialCard, Offer, Requirement, Requisition, User

from ..utils.normalization import normalize_mpn_key

_REQUISITION_STATUS_MAP = {
    "": "open",
    "Requirement": "open",
    "Quoted": "quoted",
    "Won": "won",
    "Archived": "cancelled",
}
_SOURCING_STATUS_MAP = {
    "": "open",
    "Requirement": "open",
    "Quoted": "quoted",
    "Won": "won",
    "Archived": "archived",
}


def _parse_date(raw: str) -> datetime | None:
    """M/D/YYYY → aware UTC datetime (midnight)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%m/%d/%Y").replace(tzinfo=UTC)
    except ValueError:
        logger.warning("Unparseable date in export: {!r}", raw)
        return None


def _parse_money(raw: str) -> Decimal | None:
    """'$28,000.00' / '0.05' / '' → Decimal or None."""
    raw = (raw or "").strip().replace("$", "").replace(",", "")
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        logger.warning("Unparseable price in export: {!r}", raw)
        return None


def _parse_qty(raw: str) -> int | None:
    raw = (raw or "").strip().replace(",", "")
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError:
        logger.warning("Unparseable qty in export: {!r}", raw)
        return None


def _user_map(db: Session) -> dict[str, User]:
    """Lowercased full name → User, for rep matching."""
    return {(u.name or "").strip().lower(): u for u in db.query(User).filter(User.name.isnot(None)).all()}


def _card_id(db: Session, mpn: str) -> int | None:
    key = normalize_mpn_key(mpn)
    if not key:
        return None
    row = db.query(MaterialCard.id).filter(MaterialCard.normalized_mpn == key).first()
    return row[0] if row else None


def import_proactive_export(
    db: Session,
    *,
    requirements_csv: Path,
    availabilities_csv: Path,
    actor: User,
) -> dict:
    """Import both CSVs idempotently.

    Caller commits.
    """
    users = _user_map(db)
    unmatched_reps: set[str] = set()
    # Oldest created_at among offers ACTUALLY inserted this run (skipped-existing
    # rows don't count) — drives the proactive-scan watermark rewind at the end.
    oldest_offer_created_at: datetime | None = None
    stats: dict[str, int] = {
        "companies_created": 0,
        "requisitions_created": 0,
        "requirements_created": 0,
        "offers_created": 0,
        "rows_skipped_existing": 0,
    }

    def _rep_user(name: str) -> User:
        rep = users.get((name or "").strip().lower())
        if rep:
            return rep
        if (name or "").strip():
            unmatched_reps.add(name.strip())
        return actor

    def _company_and_site(customer_name: str, owner: User) -> tuple[Company, CustomerSite]:
        company = db.query(Company).filter(func.lower(Company.name) == customer_name.lower()).first()
        if not company:
            company = Company(name=customer_name, is_active=True, account_owner_id=owner.id)
            db.add(company)
            db.flush()
            stats["companies_created"] += 1
        elif not company.account_owner_id:
            company.account_owner_id = owner.id
        site = (
            db.query(CustomerSite)
            .filter(CustomerSite.company_id == company.id, CustomerSite.is_active.is_(True))
            .first()
        )
        if not site:
            site = CustomerSite(company_id=company.id, site_name=f"{customer_name} — Primary", is_active=True)
            db.add(site)
            db.flush()
        return company, site

    with requirements_csv.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            mpn = (row["material_name"] or "").strip()
            req_item = (row["req_item_number"] or "").strip()
            if not mpn or not req_item:
                continue
            if db.query(Requisition.id).filter(Requisition.name == req_item).first():
                stats["rows_skipped_existing"] += 1
                continue
            rep = _rep_user(row.get("requisition_owner_full_name", ""))
            customer_name = (row.get("customer_account_name") or "").strip()
            company = site = None
            if customer_name:
                company, site = _company_and_site(customer_name, rep)
            asked_at = _parse_date(row.get("created_date", "")) or datetime.now(UTC)
            sf_status = (row.get("sourcing_status") or "").strip()
            requisition = Requisition(
                name=req_item,
                customer_name=customer_name or None,
                customer_site_id=site.id if site else None,
                company_id=company.id if company else None,
                status=_REQUISITION_STATUS_MAP.get(sf_status, "open"),
                created_by=rep.id,
                created_at=asked_at,
            )
            db.add(requisition)
            db.flush()
            db.add(
                Requirement(
                    requisition_id=requisition.id,
                    primary_mpn=mpn,
                    normalized_mpn=normalize_mpn_key(mpn),
                    material_card_id=_card_id(db, mpn),
                    target_qty=_parse_qty(row.get("qty", "")) or 1,
                    target_price=_parse_money(row.get("target_price_per_unit", "")),
                    sourcing_status=_SOURCING_STATUS_MAP.get(sf_status, "open"),
                    created_at=asked_at,
                )
            )
            stats["requisitions_created"] += 1
            stats["requirements_created"] += 1

    with availabilities_csv.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            mpn = (row["material_name"] or "").strip()
            src_item = (row["sourcing_item_number"] or "").strip()
            vendor = (row.get("vendor_account_name") or "").strip()
            if not mpn or not src_item or not vendor:
                continue
            marker = f"SF sourcing item {src_item}"
            if db.query(Offer.id).filter(Offer.notes == marker).first():
                stats["rows_skipped_existing"] += 1
                continue
            offer_created_at = _parse_date(row.get("created_date", "")) or datetime.now(UTC)
            db.add(
                Offer(
                    vendor_name=vendor,
                    mpn=mpn,
                    normalized_mpn=normalize_mpn_key(mpn),
                    material_card_id=_card_id(db, mpn),
                    qty_available=_parse_qty(row.get("qty", "")),
                    unit_price=_parse_money(row.get("outright_price", "")),
                    status="active",
                    source="salesforce_import",
                    entered_by_id=actor.id,
                    notes=marker,
                    created_at=offer_created_at,
                )
            )
            stats["offers_created"] += 1
            if oldest_offer_created_at is None or offer_created_at < oldest_offer_created_at:
                oldest_offer_created_at = offer_created_at

    _rewind_scan_watermark(db, oldest_offer_created_at)

    if unmatched_reps:
        logger.warning("Reps not matched to users (fell back to {}): {}", actor.email, sorted(unmatched_reps))
    return {**stats, "unmatched_reps": sorted(unmatched_reps)}


def _rewind_scan_watermark(db: Session, oldest_offer_created_at: datetime | None) -> None:
    """Rewind the proactive-scan watermark behind the oldest imported offer.

    Imported offers are backdated to their Salesforce created_date. The proactive
    batch scan only ever looks at ``Offer.created_at > watermark`` (see
    proactive_matching.run_proactive_scan); if the watermark already sits ahead of
    these backdated rows they are invisible to every future scan and never seed a
    match. So, once at least one offer was inserted, move the SHARED SystemConfig
    watermark (same key proactive_matching uses) to just behind the oldest imported
    offer — but only ever BACKWARD, never forward past where the scan already is.

    Runs inside the import transaction (flush only), so a dry-run rollback discards
    it exactly like the imported rows themselves.
    """
    if oldest_offer_created_at is None:
        return
    # Lazy import: keeps this management script from loading the matching service
    # (and its dependency graph) unless an --apply actually inserts offers.
    from ..services.proactive_matching import _get_watermark, _set_watermark

    # Strictly-before target: the scan filter is ``created_at > watermark``, so a
    # watermark equal to the oldest created_at would still exclude that offer.
    target = oldest_offer_created_at - timedelta(seconds=1)
    if _get_watermark(db) > target:
        _set_watermark(db, target)
        logger.info("Rewound proactive scan watermark to {} so imported offers are re-scanned", target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--requirements", default="tests/fixtures/proactive_sf_export/requirements.csv")
    parser.add_argument("--availabilities", default="tests/fixtures/proactive_sf_export/availabilities.csv")
    parser.add_argument("--actor-email", required=True, help="User owning fallback rows (importing admin)")
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry-run rollback)")
    args = parser.parse_args()

    from app.database import SessionLocal

    db = SessionLocal()
    try:
        actor = db.query(User).filter(func.lower(User.email) == args.actor_email.lower()).first()
        if not actor:
            logger.error("No user with email {}", args.actor_email)
            return 1
        stats = import_proactive_export(
            db,
            requirements_csv=Path(args.requirements),
            availabilities_csv=Path(args.availabilities),
            actor=actor,
        )
        if args.apply:
            db.commit()
            logger.info("APPLIED: {}", stats)
        else:
            db.rollback()
            logger.info("DRY-RUN (rolled back): {}", stats)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
