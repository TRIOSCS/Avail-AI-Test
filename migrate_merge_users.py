"""Merge duplicate user accounts caused by Graph API email case inconsistency.

Problem:
  - User id=1: mKhoury@trioscs.com  (original, mixed case from initial Graph API response)
  - User id=2: mkhoury@trioscs.com  (created when Graph returned lowercase email)

Solution:
  1. Reassign all records from user id=2 → id=1
  2. Merge tokens (keep whichever is more recent/valid)
  3. Normalize user id=1 email to lowercase
  4. Delete user id=2

Tables affected:
  - requisitions.created_by
  - contacts.user_id
  - vendor_responses.scanned_by_user_id
  - vendor_reviews.user_id

Run: docker compose exec app python -m migrate_merge_users
  or: python migrate_merge_users.py
"""
import os
import sys

# Allow running from project root or inside container
sys.path.insert(0, os.path.dirname(__file__))

from sqlalchemy import create_engine, text
from app.config import settings

def run():
    engine = create_engine(settings.database_url)
    
    with engine.begin() as conn:
        # ── Step 0: Discover duplicates ──────────────────────────────────
        rows = conn.execute(text("SELECT id, email, name, m365_connected, token_expires_at FROM users ORDER BY id")).fetchall()
        
        print(f"\n{'='*60}")
        print("AVAIL — Duplicate User Merge")
        print(f"{'='*60}")
        print(f"\nFound {len(rows)} user(s):\n")
        for r in rows:
            print(f"  id={r[0]}  email={r[1]}  name={r[2]}  m365={r[3]}  token_exp={r[4]}")
        
        if len(rows) < 2:
            print("\nNo duplicates found. Nothing to do.")
            return
        
        # Find the pair: lowercase match, different IDs
        email_map = {}
        keep_id = None
        remove_id = None
        
        for r in rows:
            norm = r[1].strip().lower()
            if norm in email_map:
                # Found a duplicate — keep the lower ID (original account)
                keep_id = email_map[norm][0]
                remove_id = r[0]
                break
            email_map[norm] = r
        
        if not keep_id or not remove_id:
            print("\nNo case-duplicate emails found. Nothing to do.")
            return
        
        keep_row = conn.execute(text("SELECT id, email, name, m365_connected, token_expires_at, refresh_token IS NOT NULL as has_token FROM users WHERE id = :id"), {"id": keep_id}).fetchone()
        remove_row = conn.execute(text("SELECT id, email, name, m365_connected, token_expires_at, refresh_token IS NOT NULL as has_token FROM users WHERE id = :id"), {"id": remove_id}).fetchone()
        
        print(f"\n  KEEP:   id={keep_id}  email={keep_row[1]}  m365={keep_row[3]}  token_exp={keep_row[4]}  has_token={keep_row[5]}")
        print(f"  REMOVE: id={remove_id}  email={remove_row[1]}  m365={remove_row[3]}  token_exp={remove_row[4]}  has_token={remove_row[5]}")
        
        # ── Step 1: Count affected records ───────────────────────────────
        counts = {}
        counts['requisitions'] = conn.execute(text("SELECT COUNT(*) FROM requisitions WHERE created_by = :uid"), {"uid": remove_id}).scalar()
        counts['contacts'] = conn.execute(text("SELECT COUNT(*) FROM contacts WHERE user_id = :uid"), {"uid": remove_id}).scalar()
        counts['vendor_responses'] = conn.execute(text("SELECT COUNT(*) FROM vendor_responses WHERE scanned_by_user_id = :uid"), {"uid": remove_id}).scalar()
        counts['vendor_reviews'] = conn.execute(text("SELECT COUNT(*) FROM vendor_reviews WHERE user_id = :uid"), {"uid": remove_id}).scalar()
        
        total = sum(counts.values())
        print(f"\n  Records to reassign from id={remove_id} → id={keep_id}:")
        for table, count in counts.items():
            print(f"    {table}: {count}")
        print(f"    TOTAL: {total}")
        
        # ── Step 2: Reassign all records ─────────────────────────────────
        if counts['requisitions'] > 0:
            conn.execute(text("UPDATE requisitions SET created_by = :keep WHERE created_by = :remove"),
                        {"keep": keep_id, "remove": remove_id})
            print(f"\n  ✓ Reassigned {counts['requisitions']} requisitions")
        
        if counts['contacts'] > 0:
            conn.execute(text("UPDATE contacts SET user_id = :keep WHERE user_id = :remove"),
                        {"keep": keep_id, "remove": remove_id})
            print(f"  ✓ Reassigned {counts['contacts']} contacts")
        
        if counts['vendor_responses'] > 0:
            conn.execute(text("UPDATE vendor_responses SET scanned_by_user_id = :keep WHERE scanned_by_user_id = :remove"),
                        {"keep": keep_id, "remove": remove_id})
            print(f"  ✓ Reassigned {counts['vendor_responses']} vendor_responses")
        
        if counts['vendor_reviews'] > 0:
            conn.execute(text("UPDATE vendor_reviews SET user_id = :keep WHERE user_id = :remove"),
                        {"keep": keep_id, "remove": remove_id})
            print(f"  ✓ Reassigned {counts['vendor_reviews']} vendor_reviews")
        
        # ── Step 3: Merge tokens ─────────────────────────────────────────
        # Keep the more recent token (from whichever user has the later expiry)
        if remove_row[4] and (not keep_row[4] or remove_row[4] > keep_row[4]):
            conn.execute(text("""
                UPDATE users SET 
                    refresh_token = src.refresh_token,
                    access_token = src.access_token,
                    token_expires_at = src.token_expires_at,
                    m365_connected = src.m365_connected,
                    last_email_scan = COALESCE(src.last_email_scan, users.last_email_scan),
                    last_inbox_scan = COALESCE(src.last_inbox_scan, users.last_inbox_scan),
                    last_contacts_sync = COALESCE(src.last_contacts_sync, users.last_contacts_sync)
                FROM users AS src
                WHERE users.id = :keep AND src.id = :remove
            """), {"keep": keep_id, "remove": remove_id})
            print(f"  ✓ Migrated newer tokens from id={remove_id}")
        else:
            print(f"  ✓ Keeping existing tokens on id={keep_id} (already newer)")
        
        # ── Step 4: Normalize email to lowercase ─────────────────────────
        normalized_email = keep_row[1].strip().lower()
        conn.execute(text("UPDATE users SET email = :email WHERE id = :id"),
                    {"email": normalized_email, "id": keep_id})
        print(f"  ✓ Normalized email: {keep_row[1]} → {normalized_email}")
        
        # ── Step 5: Delete duplicate user ────────────────────────────────
        conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": remove_id})
        print(f"  ✓ Deleted duplicate user id={remove_id}")
        
        # ── Step 6: Verify ───────────────────────────────────────────────
        remaining = conn.execute(text("SELECT id, email, name, m365_connected FROM users ORDER BY id")).fetchall()
        print(f"\n{'='*60}")
        print("RESULT — Remaining users:")
        for r in remaining:
            print(f"  id={r[0]}  email={r[1]}  name={r[2]}  m365={r[3]}")
        print(f"{'='*60}")
        print("\nDone. Duplicate merge complete.\n")


if __name__ == "__main__":
    run()
