"""v1.1.1 → v1.1.2: Upgrade VendorCard for domain-based email matching.

Adds:
  - vendor_cards.domain          (primary matching key, e.g. "winsource.com")
  - vendor_cards.alternate_names (JSON list of known name variants)
  - vendor_cards.contacts        (JSON list of rich contact objects)

Backfills domains from existing websites and emails.
"""
import os, sys, re, logging
from sqlalchemy import create_engine, text

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("migrate_v111")

DB_URL = os.getenv("DATABASE_URL", "postgresql://avail:avail@db:5432/availai")


def _extract_domain(url_or_email: str) -> str:
    """Extract base domain from URL or email."""
    s = url_or_email.strip().lower()
    # From email
    if "@" in s:
        return s.split("@")[-1]
    # From URL
    s = re.sub(r'^https?://', '', s)
    s = s.split("/")[0]  # remove path
    s = s.split(":")[0]  # remove port
    if s.startswith("www."):
        s = s[4:]
    return s


def run():
    engine = create_engine(DB_URL)
    with engine.begin() as conn:
        # Add columns if missing
        cols = {r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name='vendor_cards'"
        ))}

        if "domain" not in cols:
            conn.execute(text("ALTER TABLE vendor_cards ADD COLUMN domain VARCHAR(255)"))
            log.info("Added vendor_cards.domain")
        if "alternate_names" not in cols:
            conn.execute(text("ALTER TABLE vendor_cards ADD COLUMN alternate_names JSON DEFAULT '[]'"))
            log.info("Added vendor_cards.alternate_names")
        if "contacts" not in cols:
            conn.execute(text("ALTER TABLE vendor_cards ADD COLUMN contacts JSON DEFAULT '[]'"))
            log.info("Added vendor_cards.contacts")

        # Create index on domain
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_vendor_cards_domain
            ON vendor_cards (domain)
        """))

        # Backfill domains from existing websites
        rows = conn.execute(text(
            "SELECT id, website, emails FROM vendor_cards WHERE domain IS NULL"
        )).fetchall()

        updated = 0
        for row in rows:
            domain = None
            # Try website first
            if row[1]:
                domain = _extract_domain(row[1])
            # Fallback: derive from first email
            if not domain and row[2]:
                emails = row[2] if isinstance(row[2], list) else []
                for e in emails:
                    if isinstance(e, str) and "@" in e:
                        d = e.split("@")[-1].lower()
                        # Skip generic domains
                        if d not in {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com",
                                     "aol.com", "icloud.com", "protonmail.com", "mail.com"}:
                            domain = d
                            break
            if domain:
                conn.execute(text(
                    "UPDATE vendor_cards SET domain = :d WHERE id = :id"
                ), {"d": domain, "id": row[0]})
                updated += 1

        log.info(f"Backfilled domains for {updated}/{len(rows)} vendor cards")

        # Backfill contacts from flat emails list
        rows2 = conn.execute(text(
            "SELECT id, emails, phones, source FROM vendor_cards WHERE emails IS NOT NULL"
        )).fetchall()

        contacts_updated = 0
        for row in rows2:
            emails = row[1] if isinstance(row[1], list) else []
            source = row[3] or "unknown"
            if not emails:
                continue
            contacts = []
            for e in emails:
                if isinstance(e, str) and "@" in e:
                    contacts.append({
                        "email": e.lower().strip(),
                        "name": None,
                        "source": source,
                        "verified": source in ("email_mining", "outlook_contacts", "manual"),
                    })
            if contacts:
                import json
                conn.execute(text(
                    "UPDATE vendor_cards SET contacts = :c::json WHERE id = :id"
                ), {"c": json.dumps(contacts), "id": row[0]})
                contacts_updated += 1

        log.info(f"Backfilled contacts for {contacts_updated} vendor cards")


if __name__ == "__main__":
    run()
