#!/usr/bin/env python3
"""Deep Data Cleanup — AI-Powered Normalization.

One-time cleanup script for the AvailAI database. Runs in 6 phases:
  1. Vendor Card Dedup
  2. Company Dedup
  3. Contact Cleanup (phones + names)
  4. Sighting Normalization
  5. Company/Site Field Standardization
  6. Requirement Normalization

Usage:
    PYTHONPATH=/root/availai python scripts/data_cleanup.py [--dry-run] [--phase N]

Each phase is idempotent. Starts with pg_dump backup.
Writes audit log to cleanup_audit.json.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone

# Must set up path before app imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import SessionLocal, engine
from app.models import (
    ActivityLog,
    BuyerVendorStats,
    Company,
    CustomerSite,
    EnrichmentQueue,
    Offer,
    ProspectContact,
    Requirement,
    RoutingAssignment,
    Sighting,
    StockListHash,
    VendorCard,
    VendorContact,
    VendorMetricsSnapshot,
    VendorReview,
)
from app.utils.normalization import (
    normalize_condition,
    normalize_mpn,
    normalize_packaging,
    normalize_date_code,
)
from app.utils.normalization_helpers import (
    clean_contact_name,
    fix_encoding,
    normalize_country,
    normalize_phone_e164,
    normalize_us_state,
)
from app.vendor_utils import normalize_vendor_name

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cleanup")

# Audit log accumulator
AUDIT: list[dict] = []


def audit(phase: int, action: str, entity: str, details: dict):
    entry = {
        "phase": phase,
        "action": action,
        "entity": entity,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **details,
    }
    AUDIT.append(entry)
    log.info(f"  [{action}] {entity}: {json.dumps(details, default=str)[:200]}")


# ── Backup ──────────────────────────────────────────────────────────


def backup_database():
    """Create pg_dump backup before cleanup."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = f"/root/availai/backups/pre_cleanup_{timestamp}.sql"
    os.makedirs(os.path.dirname(backup_file), exist_ok=True)

    log.info(f"Creating database backup: {backup_file}")
    try:
        result = subprocess.run(
            [
                "pg_dump",
                "-h", "db",
                "-U", "availai",
                "-d", "availai",
                "--no-owner",
                "--no-acl",
                "-f", backup_file,
            ],
            env={**os.environ, "PGPASSWORD": "availai"},
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            log.warning(f"pg_dump warning: {result.stderr[:200]}")
        else:
            size_mb = os.path.getsize(backup_file) / (1024 * 1024)
            log.info(f"Backup complete: {size_mb:.1f} MB")
    except FileNotFoundError:
        log.warning("pg_dump not found — skipping backup (ensure PostgreSQL client is installed)")
    except Exception as e:
        log.warning(f"Backup failed: {e} — proceeding without backup")


# ── Phase 1: Vendor Card Dedup ──────────────────────────────────────


def phase1_vendor_card_dedup(db: Session, dry_run: bool):
    """Re-normalize vendor cards and merge duplicates."""
    log.info("=== Phase 1: Vendor Card Dedup ===")

    cards = db.query(VendorCard).all()
    log.info(f"Total vendor cards: {len(cards)}")

    # Re-normalize all names
    groups: dict[str, list[VendorCard]] = defaultdict(list)
    renorm_count = 0
    for card in cards:
        new_norm = normalize_vendor_name(card.display_name)
        if new_norm != card.normalized_name:
            audit(1, "renormalize", "vendor_card", {
                "id": card.id,
                "old": card.normalized_name,
                "new": new_norm,
                "display": card.display_name,
            })
            if not dry_run:
                card.normalized_name = new_norm
            renorm_count += 1
        groups[new_norm].append(card)

    log.info(f"Re-normalized {renorm_count} vendor cards")

    # Find duplicate groups
    dups = {k: v for k, v in groups.items() if len(v) > 1}
    log.info(f"Duplicate groups found: {len(dups)}")

    merged = 0
    for norm_name, group in dups.items():
        # Sort: most sightings first (winner)
        group.sort(key=lambda c: c.sighting_count or 0, reverse=True)
        winner = group[0]

        for loser in group[1:]:
            log.info(f"  Merging '{loser.display_name}' (id={loser.id}) "
                     f"into '{winner.display_name}' (id={winner.id})")
            audit(1, "merge", "vendor_card", {
                "winner_id": winner.id,
                "loser_id": loser.id,
                "winner_name": winner.display_name,
                "loser_name": loser.display_name,
            })

            if not dry_run:
                _merge_vendor_cards(db, winner, loser)
                merged += 1

    if not dry_run and (renorm_count or merged):
        db.commit()
    log.info(f"Phase 1 complete: {renorm_count} renormalized, {merged} merged")


def _merge_vendor_cards(db: Session, winner: VendorCard, loser: VendorCard):
    """Merge loser vendor card into winner, updating all FK references."""
    # Merge JSON arrays
    winner.emails = list(set((winner.emails or []) + (loser.emails or [])))
    winner.phones = list(set((winner.phones or []) + (loser.phones or [])))
    winner.alternate_names = list(
        set((winner.alternate_names or []) + (loser.alternate_names or []) + [loser.display_name])
    )
    winner.domain_aliases = list(
        set((winner.domain_aliases or []) + (loser.domain_aliases or []))
    )
    if loser.domain and not winner.domain:
        winner.domain = loser.domain

    # Sum stats
    winner.sighting_count = (winner.sighting_count or 0) + (loser.sighting_count or 0)
    winner.total_outreach = (winner.total_outreach or 0) + (loser.total_outreach or 0)
    winner.total_responses = (winner.total_responses or 0) + (loser.total_responses or 0)
    winner.total_wins = (winner.total_wins or 0) + (loser.total_wins or 0)
    winner.total_pos = (winner.total_pos or 0) + (loser.total_pos or 0)

    # Copy enrichment fields if winner is missing them
    for field in [
        "website", "linkedin_url", "legal_name", "employee_size",
        "hq_city", "hq_state", "hq_country", "industry",
    ]:
        if not getattr(winner, field) and getattr(loser, field):
            setattr(winner, field, getattr(loser, field))

    # Update FK tables
    loser_id = loser.id
    winner_id = winner.id

    # Offers
    db.query(Offer).filter_by(vendor_card_id=loser_id).update(
        {"vendor_card_id": winner_id}, synchronize_session=False
    )

    # VendorMetricsSnapshot — check unique on vendor_card_id+snapshot_date
    existing_dates = {
        r[0]
        for r in db.query(VendorMetricsSnapshot.snapshot_date)
        .filter_by(vendor_card_id=winner_id)
        .all()
    }
    for vms in db.query(VendorMetricsSnapshot).filter_by(vendor_card_id=loser_id).all():
        if vms.snapshot_date in existing_dates:
            db.delete(vms)
        else:
            vms.vendor_card_id = winner_id

    # StockListHashes
    db.query(StockListHash).filter_by(vendor_card_id=loser_id).update(
        {"vendor_card_id": winner_id}, synchronize_session=False
    )

    # VendorContacts — check unique on vendor_card_id+email
    existing_emails = {
        r[0]
        for r in db.query(VendorContact.email)
        .filter_by(vendor_card_id=winner_id)
        .all()
    }
    for vc in db.query(VendorContact).filter_by(vendor_card_id=loser_id).all():
        if vc.email and vc.email in existing_emails:
            db.delete(vc)
        else:
            vc.vendor_card_id = winner_id

    # VendorReviews
    db.query(VendorReview).filter_by(vendor_card_id=loser_id).update(
        {"vendor_card_id": winner_id}, synchronize_session=False
    )

    # ProspectContacts
    db.query(ProspectContact).filter_by(vendor_card_id=loser_id).update(
        {"vendor_card_id": winner_id}, synchronize_session=False
    )

    # ActivityLog
    db.query(ActivityLog).filter_by(vendor_card_id=loser_id).update(
        {"vendor_card_id": winner_id}, synchronize_session=False
    )

    # BuyerVendorStats — check unique on user_id+vendor_card_id
    existing_bvs_users = {
        r[0]
        for r in db.query(BuyerVendorStats.user_id)
        .filter_by(vendor_card_id=winner_id)
        .all()
    }
    for bvs in db.query(BuyerVendorStats).filter_by(vendor_card_id=loser_id).all():
        if bvs.user_id in existing_bvs_users:
            db.delete(bvs)
        else:
            bvs.vendor_card_id = winner_id

    # RoutingAssignments
    db.query(RoutingAssignment).filter_by(vendor_card_id=loser_id).update(
        {"vendor_card_id": winner_id}, synchronize_session=False
    )

    # EnrichmentQueue
    db.query(EnrichmentQueue).filter_by(vendor_card_id=loser_id).update(
        {"vendor_card_id": winner_id}, synchronize_session=False
    )

    db.flush()
    db.delete(loser)
    db.flush()


# ── Phase 2: Company Dedup ──────────────────────────────────────────


def phase2_company_dedup(db: Session, dry_run: bool):
    """Find and merge duplicate companies."""
    log.info("=== Phase 2: Company Dedup ===")

    companies = db.query(Company).all()
    log.info(f"Total companies: {len(companies)}")

    groups: dict[str, list[Company]] = defaultdict(list)
    for co in companies:
        norm = normalize_vendor_name(co.name)
        groups[norm].append(co)

    dups = {k: v for k, v in groups.items() if len(v) > 1}
    log.info(f"Duplicate company groups: {len(dups)}")

    merged = 0
    for norm_name, group in dups.items():
        # Winner: most fields filled
        group.sort(
            key=lambda c: sum(
                1 for f in ["domain", "website", "industry", "hq_country", "phone"]
                if getattr(c, f)
            ),
            reverse=True,
        )
        winner = group[0]

        for loser in group[1:]:
            log.info(f"  Merging company '{loser.name}' (id={loser.id}) "
                     f"into '{winner.name}' (id={winner.id})")
            audit(2, "merge", "company", {
                "winner_id": winner.id,
                "loser_id": loser.id,
                "winner_name": winner.name,
                "loser_name": loser.name,
            })

            if not dry_run:
                _merge_companies(db, winner, loser)
                merged += 1

    if not dry_run and merged:
        db.commit()
    log.info(f"Phase 2 complete: {merged} companies merged")


def _merge_companies(db: Session, winner: Company, loser: Company):
    """Merge loser company into winner."""
    # Copy missing fields
    for field in [
        "domain", "website", "industry", "linkedin_url", "legal_name",
        "employee_size", "hq_city", "hq_state", "hq_country",
        "account_type", "phone", "credit_terms", "tax_id",
    ]:
        if not getattr(winner, field) and getattr(loser, field):
            setattr(winner, field, getattr(loser, field))

    # Update FK tables
    db.query(CustomerSite).filter_by(company_id=loser.id).update(
        {"company_id": winner.id}, synchronize_session=False
    )
    db.query(Sighting).filter_by(source_company_id=loser.id).update(
        {"source_company_id": winner.id}, synchronize_session=False
    )
    db.query(ActivityLog).filter_by(company_id=loser.id).update(
        {"company_id": winner.id}, synchronize_session=False
    )
    db.query(EnrichmentQueue).filter_by(company_id=loser.id).update(
        {"company_id": winner.id}, synchronize_session=False
    )

    db.flush()
    db.delete(loser)
    db.flush()


# ── Phase 3: Contact Cleanup ───────────────────────────────────────


def phase3_contact_cleanup(db: Session, dry_run: bool):
    """Normalize phone numbers and clean contact names."""
    log.info("=== Phase 3: Contact Cleanup ===")

    # Sub-phase 3a: Phone normalization
    log.info("--- Phase 3a: Phone Normalization ---")
    phone_updates = 0

    # VendorContacts
    contacts = db.query(VendorContact).filter(VendorContact.phone.isnot(None)).all()
    for c in contacts:
        new_phone = normalize_phone_e164(c.phone)
        if new_phone and new_phone != c.phone:
            audit(3, "normalize_phone", "vendor_contact", {
                "id": c.id, "old": c.phone, "new": new_phone,
            })
            if not dry_run:
                c.phone = new_phone
            phone_updates += 1

    # CustomerSites
    sites = db.query(CustomerSite).filter(CustomerSite.contact_phone.isnot(None)).all()
    for s in sites:
        new_phone = normalize_phone_e164(s.contact_phone)
        if new_phone and new_phone != s.contact_phone:
            audit(3, "normalize_phone", "customer_site", {
                "id": s.id, "old": s.contact_phone, "new": new_phone,
            })
            if not dry_run:
                s.contact_phone = new_phone
            phone_updates += 1

    # Companies
    cos = db.query(Company).filter(Company.phone.isnot(None)).all()
    for co in cos:
        new_phone = normalize_phone_e164(co.phone)
        if new_phone and new_phone != co.phone:
            audit(3, "normalize_phone", "company", {
                "id": co.id, "old": co.phone, "new": new_phone,
            })
            if not dry_run:
                co.phone = new_phone
            phone_updates += 1

    # VendorCard.phones (JSON array)
    cards = db.query(VendorCard).filter(VendorCard.phones.isnot(None)).all()
    for card in cards:
        if not card.phones:
            continue
        new_phones = []
        changed = False
        for p in card.phones:
            np = normalize_phone_e164(p)
            if np and np != p:
                changed = True
                new_phones.append(np)
            else:
                new_phones.append(p)
        if changed:
            audit(3, "normalize_phones", "vendor_card", {
                "id": card.id, "count": len(new_phones),
            })
            if not dry_run:
                card.phones = new_phones
            phone_updates += 1

    log.info(f"Phone normalizations: {phone_updates}")

    # Sub-phase 3b: Contact name cleanup
    log.info("--- Phase 3b: Contact Name Cleanup ---")
    name_updates = 0

    contacts = db.query(VendorContact).filter(
        VendorContact.full_name.isnot(None)
    ).all()
    for c in contacts:
        if not c.full_name:
            continue
        cleaned, is_person = clean_contact_name(c.full_name)
        if cleaned != c.full_name:
            audit(3, "clean_name", "vendor_contact", {
                "id": c.id, "old": c.full_name, "new": cleaned, "is_person": is_person,
            })
            if not dry_run:
                c.full_name = cleaned
            name_updates += 1

    # ProspectContacts
    prospects = db.query(ProspectContact).filter(
        ProspectContact.full_name.isnot(None)
    ).all()
    for p in prospects:
        if not p.full_name:
            continue
        cleaned, is_person = clean_contact_name(p.full_name)
        if cleaned != p.full_name:
            audit(3, "clean_name", "prospect_contact", {
                "id": p.id, "old": p.full_name, "new": cleaned, "is_person": is_person,
            })
            if not dry_run:
                p.full_name = cleaned
            name_updates += 1

    log.info(f"Name cleanups: {name_updates}")

    if not dry_run and (phone_updates or name_updates):
        db.commit()
    log.info(f"Phase 3 complete: {phone_updates} phones, {name_updates} names")


# ── Phase 4: Sighting Normalization ─────────────────────────────────


def phase4_sighting_normalization(db: Session, dry_run: bool):
    """Uppercase MPNs, fix encoding, trim vendor names in sightings."""
    log.info("=== Phase 4: Sighting Normalization ===")

    batch_size = 5000
    offset = 0
    mpn_fixes = 0
    vendor_fixes = 0

    while True:
        sightings = db.query(Sighting).order_by(Sighting.id).offset(offset).limit(batch_size).all()
        if not sightings:
            break

        for s in sightings:
            # MPN normalization
            if s.mpn_matched:
                new_mpn = normalize_mpn(s.mpn_matched)
                if new_mpn and new_mpn != s.mpn_matched:
                    if not dry_run:
                        s.mpn_matched = new_mpn
                    mpn_fixes += 1

            # Vendor name: trim + fix encoding
            if s.vendor_name:
                trimmed = s.vendor_name.strip()
                fixed = fix_encoding(trimmed)
                if fixed != s.vendor_name:
                    if not dry_run:
                        s.vendor_name = fixed
                    vendor_fixes += 1

        if not dry_run:
            db.commit()
        offset += batch_size
        log.info(f"  Processed {offset} sightings...")

    log.info(f"Phase 4 complete: {mpn_fixes} MPN fixes, {vendor_fixes} vendor name fixes")
    audit(4, "summary", "sightings", {
        "mpn_fixes": mpn_fixes, "vendor_fixes": vendor_fixes,
    })


# ── Phase 5: Company/Site Field Standardization ────────────────────


def phase5_field_standardization(db: Session, dry_run: bool):
    """Normalize country and state codes across companies, sites, vendor cards."""
    log.info("=== Phase 5: Field Standardization ===")

    country_fixes = 0
    state_fixes = 0

    # Companies
    for co in db.query(Company).all():
        if co.hq_country:
            new_country = normalize_country(co.hq_country)
            if new_country and new_country != co.hq_country:
                audit(5, "normalize_country", "company", {
                    "id": co.id, "old": co.hq_country, "new": new_country,
                })
                if not dry_run:
                    co.hq_country = new_country
                country_fixes += 1

        if co.hq_state:
            new_state = normalize_us_state(co.hq_state)
            if new_state and new_state != co.hq_state:
                audit(5, "normalize_state", "company", {
                    "id": co.id, "old": co.hq_state, "new": new_state,
                })
                if not dry_run:
                    co.hq_state = new_state
                state_fixes += 1

    # CustomerSites
    for site in db.query(CustomerSite).all():
        country = getattr(site, "country", None)
        if country:
            new_country = normalize_country(country)
            if new_country and new_country != country:
                audit(5, "normalize_country", "customer_site", {
                    "id": site.id, "old": country, "new": new_country,
                })
                if not dry_run:
                    site.country = new_country
                country_fixes += 1

        state = getattr(site, "state", None)
        if state:
            new_state = normalize_us_state(state)
            if new_state and new_state != state:
                audit(5, "normalize_state", "customer_site", {
                    "id": site.id, "old": state, "new": new_state,
                })
                if not dry_run:
                    site.state = new_state
                state_fixes += 1

    # VendorCards
    for card in db.query(VendorCard).all():
        if card.hq_country:
            new_country = normalize_country(card.hq_country)
            if new_country and new_country != card.hq_country:
                audit(5, "normalize_country", "vendor_card", {
                    "id": card.id, "old": card.hq_country, "new": new_country,
                })
                if not dry_run:
                    card.hq_country = new_country
                country_fixes += 1

        if card.hq_state:
            new_state = normalize_us_state(card.hq_state)
            if new_state and new_state != card.hq_state:
                audit(5, "normalize_state", "vendor_card", {
                    "id": card.id, "old": card.hq_state, "new": new_state,
                })
                if not dry_run:
                    card.hq_state = new_state
                state_fixes += 1

    if not dry_run and (country_fixes or state_fixes):
        db.commit()
    log.info(f"Phase 5 complete: {country_fixes} country fixes, {state_fixes} state fixes")


# ── Phase 6: Requirement Normalization ──────────────────────────────


def phase6_requirement_normalization(db: Session, dry_run: bool):
    """Uppercase MPNs, normalize condition/packaging/date_code in requirements."""
    log.info("=== Phase 6: Requirement Normalization ===")

    batch_size = 5000
    offset = 0
    mpn_fixes = 0
    sub_fixes = 0
    field_fixes = 0

    while True:
        reqs = db.query(Requirement).order_by(Requirement.id).offset(offset).limit(batch_size).all()
        if not reqs:
            break

        for req in reqs:
            # Primary MPN
            if req.primary_mpn:
                new_mpn = normalize_mpn(req.primary_mpn)
                if new_mpn and new_mpn != req.primary_mpn:
                    if not dry_run:
                        req.primary_mpn = new_mpn
                    mpn_fixes += 1

            # Substitutes
            if req.substitutes:
                new_subs = []
                changed = False
                for s in req.substitutes:
                    ns = normalize_mpn(s)
                    if ns and ns != s:
                        changed = True
                        new_subs.append(ns)
                    else:
                        new_subs.append(s)
                if changed:
                    if not dry_run:
                        req.substitutes = new_subs
                    sub_fixes += 1

            # Condition
            cond = getattr(req, "condition", None)
            if cond:
                new_cond = normalize_condition(cond)
                if new_cond and new_cond != cond:
                    if not dry_run:
                        req.condition = new_cond
                    field_fixes += 1

            # Packaging
            pkg = getattr(req, "packaging", None)
            if pkg:
                new_pkg = normalize_packaging(pkg)
                if new_pkg and new_pkg != pkg:
                    if not dry_run:
                        req.packaging = new_pkg
                    field_fixes += 1

            # Date code
            dc = getattr(req, "date_codes", None)
            if dc:
                new_dc = normalize_date_code(dc)
                if new_dc and new_dc != dc:
                    if not dry_run:
                        req.date_codes = new_dc
                    field_fixes += 1

        if not dry_run:
            db.commit()
        offset += batch_size
        log.info(f"  Processed {offset} requirements...")

    log.info(f"Phase 6 complete: {mpn_fixes} MPN fixes, {sub_fixes} substitute fixes, "
             f"{field_fixes} field fixes")
    audit(6, "summary", "requirements", {
        "mpn_fixes": mpn_fixes, "sub_fixes": sub_fixes, "field_fixes": field_fixes,
    })


# ── Main ────────────────────────────────────────────────────────────


PHASES = {
    1: ("Vendor Card Dedup", phase1_vendor_card_dedup),
    2: ("Company Dedup", phase2_company_dedup),
    3: ("Contact Cleanup", phase3_contact_cleanup),
    4: ("Sighting Normalization", phase4_sighting_normalization),
    5: ("Field Standardization", phase5_field_standardization),
    6: ("Requirement Normalization", phase6_requirement_normalization),
}


def main():
    parser = argparse.ArgumentParser(description="AvailAI Deep Data Cleanup")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without modifying DB")
    parser.add_argument("--phase", type=int, help="Run only a specific phase (1-6)")
    parser.add_argument("--skip-backup", action="store_true", help="Skip pg_dump backup")
    args = parser.parse_args()

    if args.dry_run:
        log.info("*** DRY RUN MODE — no changes will be made ***")

    # Backup
    if not args.skip_backup and not args.dry_run:
        backup_database()

    db = SessionLocal()
    try:
        if args.phase:
            if args.phase not in PHASES:
                log.error(f"Invalid phase: {args.phase}. Valid: 1-6")
                sys.exit(1)
            name, func = PHASES[args.phase]
            log.info(f"Running phase {args.phase}: {name}")
            func(db, args.dry_run)
        else:
            for phase_num, (name, func) in PHASES.items():
                log.info(f"\n{'='*60}")
                log.info(f"Phase {phase_num}: {name}")
                log.info(f"{'='*60}")
                func(db, args.dry_run)
    finally:
        db.close()

    # Write audit log
    audit_file = "/root/availai/cleanup_audit.json"
    with open(audit_file, "w") as f:
        json.dump(AUDIT, f, indent=2, default=str)
    log.info(f"\nAudit log written to {audit_file} ({len(AUDIT)} entries)")

    if args.dry_run:
        log.info("*** DRY RUN complete — no changes were made ***")


if __name__ == "__main__":
    main()
