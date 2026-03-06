"""Export customer contact emails to CSV for Mailchimp.

Queries site_contacts and customer_sites tables, joined to companies.
Filters to account_type='Customer', excludes brokers/resellers/wholesalers,
deduplicates on email.

Run inside Docker: docker compose exec -T app python -c "$(cat export_customer_emails.py)"
"""

import csv
import os

from sqlalchemy import create_engine, text

DB_URL = os.environ.get("DATABASE_URL", "postgresql://availai:availai@db:5432/availai")

# Exclude brokers, resellers, wholesalers, distributors, authorized distys
# Also exclude companies that have a matching vendor_card with high sighting counts (broker behavior)
BROKER_FILTER = """
    AND LOWER(COALESCE(c.industry, '')) NOT LIKE '%%broker%%'
    AND LOWER(COALESCE(c.industry, '')) NOT LIKE '%%reseller%%'
    AND LOWER(COALESCE(c.industry, '')) NOT LIKE '%%wholesale%%'
    AND LOWER(COALESCE(c.industry, '')) NOT LIKE '%%distributor%%'
    AND LOWER(COALESCE(c.industry, '')) NOT LIKE '%%authorized%%disty%%'
    AND LOWER(c.name) NOT LIKE '%%broker%%'
    AND LOWER(c.name) NOT LIKE '%%reseller%%'
    AND c.id NOT IN (
        SELECT c2.id FROM companies c2
        JOIN vendor_cards vc ON vc.normalized_name = LOWER(c2.name)
        WHERE vc.sighting_count > 20
          AND LOWER(c2.name) != 'sonicare solutions'
    )
"""

QUERY = text(f"""
WITH customer_emails AS (
    -- Source 1: site_contacts (has full_name as single field)
    SELECT
        sc.full_name,
        TRIM(LOWER(sc.email)) AS email,
        c.name AS company_name,
        c.industry AS company_industry,
        u.name AS account_manager,
        c.id AS company_id
    FROM site_contacts sc
    JOIN customer_sites cs ON cs.id = sc.customer_site_id
    JOIN companies c ON c.id = cs.company_id
    LEFT JOIN users u ON u.id = c.account_owner_id
    WHERE c.account_type = 'Customer'
      AND sc.email IS NOT NULL
      AND TRIM(sc.email) != ''
      {BROKER_FILTER}

    UNION

    -- Source 2: customer_sites inline contact
    SELECT
        cs.contact_name AS full_name,
        TRIM(LOWER(cs.contact_email)) AS email,
        c.name AS company_name,
        c.industry AS company_industry,
        u.name AS account_manager,
        c.id AS company_id
    FROM customer_sites cs
    JOIN companies c ON c.id = cs.company_id
    LEFT JOIN users u ON u.id = c.account_owner_id
    WHERE c.account_type = 'Customer'
      AND cs.contact_email IS NOT NULL
      AND TRIM(cs.contact_email) != ''
      {BROKER_FILTER}
)
SELECT DISTINCT ON (email)
    ce.full_name,
    ce.email,
    ce.company_name,
    ce.company_industry,
    ce.account_manager,
    CASE WHEN vc.id IS NOT NULL THEN 'ALSO VENDOR' ELSE '' END AS vendor_flag,
    COALESCE(vc.sighting_count, 0) AS vendor_sightings,
    CASE WHEN ve.email IS NOT NULL THEN 'YES' ELSE '' END AS in_vendor_contacts
FROM customer_emails ce
LEFT JOIN vendor_cards vc ON vc.normalized_name = LOWER(ce.company_name)
LEFT JOIN (
    SELECT DISTINCT LOWER(TRIM(email)) AS email
    FROM vendor_contacts
    WHERE email IS NOT NULL AND TRIM(email) != ''
) ve ON ve.email = ce.email
ORDER BY email, full_name
""")

OUTPUT_FILE = "customer_email_export.csv"


def split_name(full_name: str | None) -> tuple[str, str]:
    """Best-effort split of full_name into (first, last)."""
    if not full_name or not full_name.strip():
        return ("", "")
    parts = full_name.strip().split(None, 1)
    return (parts[0], parts[1] if len(parts) > 1 else "")


def main():
    engine = create_engine(DB_URL)
    with engine.connect() as conn:
        rows = conn.execute(QUERY).fetchall()

    with open(OUTPUT_FILE, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "first_name",
                "last_name",
                "email",
                "company_name",
                "industry",
                "account_manager",
                "vendor_flag",
                "vendor_sightings",
                "in_vendor_contacts",
            ]
        )
        for row in rows:
            first, last = split_name(row.full_name)
            writer.writerow(
                [
                    first,
                    last,
                    row.email,
                    row.company_name,
                    row.company_industry or "",
                    row.account_manager or "",
                    row.vendor_flag,
                    row.vendor_sightings,
                    row.in_vendor_contacts,
                ]
            )

    print(f"Exported {len(rows)} unique customer emails to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
