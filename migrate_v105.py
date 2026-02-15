#!/usr/bin/env python3
"""Migrate v1.0.4 → v1.0.5: Create new tables, migrate vendor_contacts → vendor_cards."""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.database import engine, SessionLocal
from app.models import Base
from app.vendor_utils import normalize_vendor_name


def migrate():
    print("=== AVAIL v1.0.5 Migration ===")

    # 1. Create new tables (vendor_cards, vendor_reviews, material_cards, material_vendor_history)
    print("[1/3] Creating new tables…")
    Base.metadata.create_all(bind=engine)
    print("  ✓ Tables created")

    # 2. Migrate data from vendor_contacts → vendor_cards (using raw SQL since model was removed)
    db = SessionLocal()
    try:
        # Check if vendor_contacts table exists
        try:
            rows = db.execute(text("SELECT id, vendor_name, email, phone, website, all_emails, source, raw_response FROM vendor_contacts")).fetchall()
        except Exception:
            print("[2/3] No vendor_contacts table found — skipping migration")
            rows = []

        if rows:
            print(f"[2/3] Migrating {len(rows)} vendor contacts → vendor cards…")
            migrated = 0
            skipped = 0

            for row in rows:
                vendor_name = row[1]
                email = row[2]
                phone = row[3]
                website = row[4]
                all_emails_raw = row[5]
                source = row[6]
                raw_response = row[7]

                norm = normalize_vendor_name(vendor_name)
                if not norm:
                    skipped += 1
                    continue

                # Check if card already exists
                existing = db.execute(
                    text("SELECT id, emails FROM vendor_cards WHERE normalized_name = :n"),
                    {"n": norm}
                ).fetchone()

                # Parse all_emails (could be JSON string or list)
                all_emails = []
                if all_emails_raw:
                    if isinstance(all_emails_raw, str):
                        try:
                            all_emails = json.loads(all_emails_raw)
                        except (json.JSONDecodeError, TypeError):
                            all_emails = []
                    elif isinstance(all_emails_raw, list):
                        all_emails = all_emails_raw

                if existing:
                    # Merge emails
                    old_emails = json.loads(existing[1]) if isinstance(existing[1], str) else (existing[1] or [])
                    new_emails = all_emails or ([email] if email else [])
                    merged = list(dict.fromkeys(old_emails + [e.strip().lower() for e in new_emails if e and "@" in str(e)]))
                    if len(merged) > len(old_emails):
                        db.execute(
                            text("UPDATE vendor_cards SET emails = :e WHERE id = :id"),
                            {"e": json.dumps(merged), "id": existing[0]}
                        )
                    else:
                        skipped += 1
                    continue

                # Build email list
                emails = []
                if all_emails:
                    emails = [e.strip().lower() for e in all_emails if e and "@" in str(e)]
                elif email:
                    emails = [email.strip().lower()]
                emails = list(dict.fromkeys(emails))

                # Build phone list
                phones = [phone.strip()] if phone else []

                db.execute(text(
                    "INSERT INTO vendor_cards (normalized_name, display_name, website, emails, phones, source, raw_response, sighting_count, is_blacklisted) "
                    "VALUES (:norm, :display, :web, :emails, :phones, :source, :raw, 0, 0)"
                ), {
                    "norm": norm, "display": vendor_name, "web": website,
                    "emails": json.dumps(emails), "phones": json.dumps(phones),
                    "source": source, "raw": (raw_response or "")[:1000],
                })
                migrated += 1

            db.commit()
            print(f"  ✓ Migrated: {migrated}  Skipped: {skipped}")
        else:
            print("[2/3] No contacts to migrate")

        # 3. Summary
        total = db.execute(text("SELECT COUNT(*) FROM vendor_cards")).scalar()
        print(f"[3/3] Done! Total vendor cards: {total}")

    finally:
        db.close()

    print("=== Migration complete ===")


if __name__ == "__main__":
    migrate()
