"""data cleanup: contact_phone_2, dedup, phone normalize, site_name extract

Revision ID: 055
Revises: 054
Create Date: 2026-03-07
"""

import re

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision = "055"
down_revision = "054"
branch_labels = None
depends_on = None


PHONE_RE = re.compile(r"[\(+]?\d[\d\s\-\(\)\.]{8,}\d")


def _dedup_site_contacts(conn):
    """Merge duplicate SiteContacts sharing (customer_site_id, lower(email))."""
    dupes = conn.execute(
        text("""
        SELECT customer_site_id, lower(email) as em, array_agg(id ORDER BY id) as ids
        FROM site_contacts
        WHERE email IS NOT NULL
        GROUP BY customer_site_id, lower(email)
        HAVING count(*) > 1
    """)
    ).fetchall()
    for row in dupes:
        ids = row.ids
        contacts = conn.execute(text("SELECT * FROM site_contacts WHERE id = ANY(:ids)"), {"ids": ids}).fetchall()
        best = max(contacts, key=lambda c: sum(1 for v in c if v is not None))
        delete_ids = [c.id for c in contacts if c.id != best.id]
        for other in contacts:
            if other.id == best.id:
                continue
            for col in ["full_name", "title", "phone", "notes", "linkedin_url"]:
                best_val = getattr(best, col, None)
                other_val = getattr(other, col, None)
                if best_val is None and other_val is not None:
                    conn.execute(
                        text(f"UPDATE site_contacts SET {col} = :val WHERE id = :id"), {"val": other_val, "id": best.id}
                    )
        conn.execute(text("DELETE FROM site_contacts WHERE id = ANY(:ids)"), {"ids": delete_ids})


def _normalize_phones(conn):
    """Normalize all phone fields to E.164 format."""
    from app.utils.phone_utils import format_phone_e164

    tables_cols = [
        ("companies", "phone"),
        ("customer_sites", "contact_phone"),
        ("customer_sites", "contact_phone_2"),
        ("site_contacts", "phone"),
        ("vendor_contacts", "phone"),
        ("vendor_contacts", "phone_mobile"),
    ]
    for table, col in tables_cols:
        rows = conn.execute(
            text(f"""
            SELECT id, {col} FROM {table}
            WHERE {col} IS NOT NULL AND {col} != ''
              AND {col} NOT LIKE '+%%'
        """)
        ).fetchall()
        for row in rows:
            raw = getattr(row, col)
            normalized = format_phone_e164(raw)
            if normalized and normalized != raw:
                conn.execute(text(f"UPDATE {table} SET {col} = :val WHERE id = :id"), {"val": normalized, "id": row.id})


def _extract_phones_from_site_name(conn):
    """Extract phone numbers embedded in customer_sites.site_name."""
    from app.utils.phone_utils import format_phone_e164

    rows = conn.execute(
        text("""
        SELECT id, site_name, contact_phone, contact_phone_2
        FROM customer_sites WHERE site_name IS NOT NULL
    """)
    ).fetchall()
    for row in rows:
        match = PHONE_RE.search(row.site_name)
        if not match:
            continue
        raw_phone = match.group(0).strip()
        e164 = format_phone_e164(raw_phone)
        if not e164:
            continue
        clean_name = row.site_name[: match.start()] + row.site_name[match.end() :]
        clean_name = re.sub(r"\s+", " ", clean_name).strip(" -\u2013\u2014,")
        target_col = "contact_phone" if not row.contact_phone else "contact_phone_2"
        conn.execute(
            text(f"""
            UPDATE customer_sites
            SET site_name = :name, {target_col} = :phone
            WHERE id = :id
        """),
            {"name": clean_name, "phone": e164, "id": row.id},
        )


def upgrade():
    op.add_column("customer_sites", sa.Column("contact_phone_2", sa.String(100), nullable=True))
    conn = op.get_bind()
    _dedup_site_contacts(conn)
    _normalize_phones(conn)
    _extract_phones_from_site_name(conn)


def downgrade():
    op.drop_column("customer_sites", "contact_phone_2")
