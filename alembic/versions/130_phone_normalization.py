"""Phone normalization foundation — normalized_phone (E.164) columns + indexes.

Adds indexed E.164 normalized_phone columns to single-phone tables and a
normalized_phones JSON list (with Postgres GIN index) on VendorCard.phones:

- companies.normalized_phone (String(20), indexed)
- customer_sites.normalized_phone (String(20), indexed)
- customer_sites.normalized_phone_2 (String(20), indexed)
- site_contacts.normalized_phone (String(20), indexed)
- vendor_contacts.normalized_phone (String(20), indexed)
- vendor_cards.normalized_phones (JSON, list of E.164) + GIN index (Postgres-only)

Backfill: populates each normalized_* from the raw value via normalize_e164().
Postgres GIN index on vendor_cards.normalized_phones is guarded behind
dialect.name == "postgresql" (mirrors the pg_trgm guard pattern from migration 120).

Revision ID: 130_phone_normalization
Revises: 129_drop_bid_tables
Create Date: 2026-06-23
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "130_phone_normalization"
down_revision = "129_drop_bid_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # --- companies ---
    op.add_column("companies", sa.Column("normalized_phone", sa.String(20), nullable=True))
    op.create_index("ix_companies_normalized_phone", "companies", ["normalized_phone"])

    # --- customer_sites ---
    op.add_column("customer_sites", sa.Column("normalized_phone", sa.String(20), nullable=True))
    op.add_column("customer_sites", sa.Column("normalized_phone_2", sa.String(20), nullable=True))
    op.create_index("ix_customer_sites_normalized_phone", "customer_sites", ["normalized_phone"])
    op.create_index("ix_customer_sites_normalized_phone_2", "customer_sites", ["normalized_phone_2"])

    # --- site_contacts ---
    op.add_column("site_contacts", sa.Column("normalized_phone", sa.String(20), nullable=True))
    op.create_index("ix_site_contacts_normalized_phone", "site_contacts", ["normalized_phone"])

    # --- vendor_contacts ---
    op.add_column("vendor_contacts", sa.Column("normalized_phone", sa.String(20), nullable=True))
    op.create_index("ix_vendor_contacts_normalized_phone", "vendor_contacts", ["normalized_phone"])

    # --- vendor_cards: normalized_phones JSON list ---
    op.add_column("vendor_cards", sa.Column("normalized_phones", sa.JSON(), nullable=True))

    # GIN index on vendor_cards.normalized_phones — Postgres-only (SQLite has no JSONB/GIN)
    if bind.dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX ix_vendor_cards_normalized_phones_gin "
            "ON vendor_cards USING gin (normalized_phones jsonb_path_ops)"
        )

    # --- Backfill ---
    from app.utils.phone import normalize_e164

    # companies
    companies = sa.table(
        "companies",
        sa.column("id", sa.Integer),
        sa.column("phone", sa.String),
        sa.column("normalized_phone", sa.String),
    )
    for row in bind.execute(sa.select(companies.c.id, companies.c.phone)).fetchall():
        norm = normalize_e164(row.phone)
        if norm:
            bind.execute(sa.update(companies).where(companies.c.id == row.id).values(normalized_phone=norm))

    # customer_sites
    sites = sa.table(
        "customer_sites",
        sa.column("id", sa.Integer),
        sa.column("contact_phone", sa.String),
        sa.column("contact_phone_2", sa.String),
        sa.column("normalized_phone", sa.String),
        sa.column("normalized_phone_2", sa.String),
    )
    for row in bind.execute(sa.select(sites.c.id, sites.c.contact_phone, sites.c.contact_phone_2)).fetchall():
        norm1 = normalize_e164(row.contact_phone)
        norm2 = normalize_e164(row.contact_phone_2)
        updates: dict = {}
        if norm1:
            updates["normalized_phone"] = norm1
        if norm2:
            updates["normalized_phone_2"] = norm2
        if updates:
            bind.execute(sa.update(sites).where(sites.c.id == row.id).values(**updates))

    # site_contacts
    sc = sa.table(
        "site_contacts",
        sa.column("id", sa.Integer),
        sa.column("phone", sa.String),
        sa.column("normalized_phone", sa.String),
    )
    for row in bind.execute(sa.select(sc.c.id, sc.c.phone)).fetchall():
        norm = normalize_e164(row.phone)
        if norm:
            bind.execute(sa.update(sc).where(sc.c.id == row.id).values(normalized_phone=norm))

    # vendor_contacts
    vc = sa.table(
        "vendor_contacts",
        sa.column("id", sa.Integer),
        sa.column("phone", sa.String),
        sa.column("normalized_phone", sa.String),
    )
    for row in bind.execute(sa.select(vc.c.id, vc.c.phone)).fetchall():
        norm = normalize_e164(row.phone)
        if norm:
            bind.execute(sa.update(vc).where(vc.c.id == row.id).values(normalized_phone=norm))

    # vendor_cards — backfill normalized_phones JSON list from phones JSON
    import json

    vcards = sa.table(
        "vendor_cards",
        sa.column("id", sa.Integer),
        sa.column("phones", sa.JSON),
        sa.column("normalized_phones", sa.JSON),
    )
    for row in bind.execute(sa.select(vcards.c.id, vcards.c.phones)).fetchall():
        phones_raw = row.phones
        if isinstance(phones_raw, str):
            try:
                phones_raw = json.loads(phones_raw)
            except (ValueError, TypeError):
                phones_raw = []
        if not phones_raw:
            continue
        normalized = [normalize_e164(p) for p in phones_raw if p]
        normalized = [n for n in normalized if n is not None]
        if normalized:
            bind.execute(
                sa.update(vcards).where(vcards.c.id == row.id).values(normalized_phones=json.dumps(normalized))
            )


def downgrade() -> None:
    bind = op.get_bind()

    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_vendor_cards_normalized_phones_gin")

    op.drop_column("vendor_cards", "normalized_phones")

    op.drop_index("ix_vendor_contacts_normalized_phone", table_name="vendor_contacts")
    op.drop_column("vendor_contacts", "normalized_phone")

    op.drop_index("ix_site_contacts_normalized_phone", table_name="site_contacts")
    op.drop_column("site_contacts", "normalized_phone")

    op.drop_index("ix_customer_sites_normalized_phone_2", table_name="customer_sites")
    op.drop_index("ix_customer_sites_normalized_phone", table_name="customer_sites")
    op.drop_column("customer_sites", "normalized_phone_2")
    op.drop_column("customer_sites", "normalized_phone")

    op.drop_index("ix_companies_normalized_phone", table_name="companies")
    op.drop_column("companies", "normalized_phone")
