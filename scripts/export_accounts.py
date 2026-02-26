#!/usr/bin/env python3
"""Export accounts (companies + sites + contacts) owned by a given user.

Run inside Docker:
    docker compose exec app python scripts/export_accounts.py "Martina Tewes"

Run locally:
    PYTHONPATH=/root/availai python scripts/export_accounts.py "Martina Tewes"

Outputs a CSV to stdout with company, site, and contact details.
"""

import csv
import sys

from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from app.database import SessionLocal
from app.models.auth import User
from app.models.crm import Company, CustomerSite


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/export_accounts.py <user_name>", file=sys.stderr)
        sys.exit(1)

    user_name = sys.argv[1]
    db = SessionLocal()

    try:
        user = db.query(User).filter(User.name.ilike(f"%{user_name}%")).first()
        if not user:
            print(f"No user found matching '{user_name}'", file=sys.stderr)
            sys.exit(1)

        print(f"Found user: {user.name} (id={user.id})", file=sys.stderr)

        # Get company IDs owned at company level OR with sites owned by user
        company_ids = (
            db.query(Company.id)
            .outerjoin(CustomerSite, Company.id == CustomerSite.company_id)
            .filter(
                or_(
                    Company.account_owner_id == user.id,
                    CustomerSite.owner_id == user.id,
                )
            )
            .distinct()
            .all()
        )
        company_ids = [cid for (cid,) in company_ids]

        companies = (
            db.query(Company)
            .options(
                joinedload(Company.sites).joinedload(CustomerSite.site_contacts),
            )
            .filter(Company.id.in_(company_ids))
            .all()
        )

        print(f"Found {len(companies)} companies", file=sys.stderr)

        writer = csv.writer(sys.stdout)
        writer.writerow([
            "company_id", "company_name", "domain", "industry",
            "account_type", "phone", "hq_city", "hq_state", "hq_country",
            "site_id", "site_name", "site_city", "site_state", "site_country",
            "contact_name", "contact_title", "contact_email", "contact_phone",
            "is_primary",
        ])

        rows = 0
        for co in companies:
            if not co.sites:
                writer.writerow([
                    co.id, co.name, co.domain or "", co.industry or "",
                    co.account_type or "", co.phone or "",
                    co.hq_city or "", co.hq_state or "", co.hq_country or "",
                    "", "", "", "", "",
                    "", "", "", "", "",
                ])
                rows += 1
            else:
                for site in co.sites:
                    contacts = site.site_contacts if site.site_contacts else [None]
                    for contact in contacts:
                        writer.writerow([
                            co.id, co.name, co.domain or "", co.industry or "",
                            co.account_type or "", co.phone or "",
                            co.hq_city or "", co.hq_state or "", co.hq_country or "",
                            site.id, site.site_name,
                            site.city or "", site.state or "", site.country or "",
                            contact.full_name if contact else "",
                            contact.title if contact else "",
                            contact.email if contact else "",
                            contact.phone if contact else "",
                            contact.is_primary if contact else "",
                        ])
                        rows += 1

        print(f"Exported {rows} rows", file=sys.stderr)

    finally:
        db.close()


if __name__ == "__main__":
    main()
