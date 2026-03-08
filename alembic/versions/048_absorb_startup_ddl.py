"""Absorb startup.py DDL into Alembic.

Moves all column additions, index creations, FK changes, CHECK constraints,
and extensions from startup.py into a proper migration. All operations are
idempotent (IF NOT EXISTS / inspection checks) so this is safe to run on
production DBs where startup.py already applied these changes.

Revision ID: 048
Revises: 047
"""

import sqlalchemy as sa

from alembic import op

revision = "048"
down_revision = "047"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _col_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    return column in [c["name"] for c in insp.get_columns(table)]


def _idx_exists(table: str, index: str) -> bool:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    return index in [i["name"] for i in insp.get_indexes(table)]


def _constraint_exists(name: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text("SELECT 1 FROM pg_constraint WHERE conname = :name"),
        {"name": name},
    )
    return result.fetchone() is not None


def _add_col(table: str, col: sa.Column) -> None:
    if not _col_exists(table, col.name):
        op.add_column(table, col)


def _add_idx(name: str, table: str, columns: list, **kw) -> None:
    if not _idx_exists(table, name):
        op.create_index(name, table, columns, **kw)


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    conn = op.get_bind()

    # ── Extensions ────────────────────────────────────────────────────
    conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_stat_statements"))
    conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))

    # ── Column additions (19) ─────────────────────────────────────────
    # vendor_cards
    _add_col("vendor_cards", sa.Column("vendor_score", sa.Float()))
    _add_col("vendor_cards", sa.Column("advancement_score", sa.Float()))
    _add_col("vendor_cards", sa.Column("is_new_vendor", sa.Boolean(), server_default="true"))
    _add_col("vendor_cards", sa.Column("vendor_score_computed_at", sa.DateTime()))

    # vendor_contacts
    _add_col("vendor_contacts", sa.Column("first_name", sa.String(100)))
    _add_col("vendor_contacts", sa.Column("last_name", sa.String(100)))
    _add_col("vendor_contacts", sa.Column("phone_mobile", sa.String(100)))
    _add_col("vendor_contacts", sa.Column("relationship_score", sa.Float()))
    _add_col("vendor_contacts", sa.Column("activity_trend", sa.String(20)))
    _add_col("vendor_contacts", sa.Column("score_computed_at", sa.DateTime()))

    # activity_log
    _add_col("activity_log", sa.Column("quote_id", sa.Integer()))
    _add_col("activity_log", sa.Column("auto_logged", sa.Boolean(), server_default="false"))
    _add_col("activity_log", sa.Column("occurred_at", sa.DateTime()))
    _add_col(
        "activity_log",
        sa.Column(
            "customer_site_id",
            sa.Integer(),
            sa.ForeignKey("customer_sites.id"),
        ),
    )
    _add_col(
        "activity_log",
        sa.Column(
            "site_contact_id",
            sa.Integer(),
            sa.ForeignKey("site_contacts.id"),
        ),
    )

    # site_contacts
    _add_col("site_contacts", sa.Column("is_active", sa.Boolean(), server_default="true"))

    # api_sources
    _add_col("api_sources", sa.Column("is_active", sa.Boolean(), server_default="false"))

    # material_cards
    _add_col("material_cards", sa.Column("deleted_at", sa.DateTime()))

    # requirements — generated column (raw SQL needed)
    if not _col_exists("requirements", "substitutes_text"):
        conn.execute(
            sa.text(
                "ALTER TABLE requirements "
                "ADD COLUMN substitutes_text TEXT "
                "GENERATED ALWAYS AS (substitutes::text) STORED"
            )
        )

    # ── FK constraint changes (2) ─────────────────────────────────────
    # offers.vendor_card_id → ON DELETE SET NULL
    conn.execute(
        sa.text("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM information_schema.table_constraints
            WHERE constraint_name='offers_vendor_card_id_fkey' AND table_name='offers')
          THEN
            ALTER TABLE offers DROP CONSTRAINT offers_vendor_card_id_fkey;
            ALTER TABLE offers ADD CONSTRAINT offers_vendor_card_id_fkey
              FOREIGN KEY (vendor_card_id) REFERENCES vendor_cards(id) ON DELETE SET NULL;
          END IF;
        END $$
    """)
    )

    # offers.vendor_response_id → ON DELETE SET NULL
    conn.execute(
        sa.text("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM information_schema.table_constraints
            WHERE constraint_name='offers_vendor_response_id_fkey' AND table_name='offers')
          THEN
            ALTER TABLE offers DROP CONSTRAINT offers_vendor_response_id_fkey;
            ALTER TABLE offers ADD CONSTRAINT offers_vendor_response_id_fkey
              FOREIGN KEY (vendor_response_id) REFERENCES vendor_responses(id) ON DELETE SET NULL;
          END IF;
        END $$
    """)
    )

    # ── Index cleanup (1 drop) ────────────────────────────────────────
    conn.execute(sa.text("DROP INDEX IF EXISTS ix_sightings_vendor_lower"))

    # ── Index creations (16) ──────────────────────────────────────────
    # Trigram indexes (GIN)
    _add_idx(
        "ix_requisitions_name_trgm",
        "requisitions",
        ["name"],
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )
    _add_idx(
        "ix_requisitions_customer_name_trgm",
        "requisitions",
        ["customer_name"],
        postgresql_using="gin",
        postgresql_ops={"customer_name": "gin_trgm_ops"},
    )
    _add_idx(
        "ix_requirements_mpn_trgm",
        "requirements",
        ["primary_mpn"],
        postgresql_using="gin",
        postgresql_ops={"primary_mpn": "gin_trgm_ops"},
    )
    _add_idx(
        "ix_requirements_subs_trgm",
        "requirements",
        ["substitutes_text"],
        postgresql_using="gin",
        postgresql_ops={"substitutes_text": "gin_trgm_ops"},
    )

    # Partial / functional indexes
    _add_idx(
        "ix_vendor_cards_blacklisted",
        "vendor_cards",
        ["is_blacklisted"],
        postgresql_where=sa.text("is_blacklisted = TRUE"),
    )

    # Expression index — raw SQL
    if not _idx_exists("requirements", "ix_requirements_primary_mpn"):
        conn.execute(sa.text("CREATE INDEX ix_requirements_primary_mpn ON requirements (LOWER(primary_mpn))"))

    _add_idx(
        "ix_offers_vendor_card", "offers", ["vendor_card_id"], postgresql_where=sa.text("vendor_card_id IS NOT NULL")
    )
    _add_idx(
        "ix_contacts_vendor_name", "contacts", ["vendor_name"], postgresql_where=sa.text("vendor_name IS NOT NULL")
    )
    _add_idx(
        "ix_vendor_responses_vendor_name",
        "vendor_responses",
        ["vendor_name"],
        postgresql_where=sa.text("vendor_name IS NOT NULL"),
    )
    _add_idx("ix_offers_vendor_name", "offers", ["vendor_name"])
    _add_idx("ix_vendor_cards_score_computed_at", "vendor_cards", ["vendor_score_computed_at"])

    # FK indexes (prevent slow JOINs / cascade deletes)
    _add_idx("ix_activity_log_customer_site_id", "activity_log", ["customer_site_id"])
    _add_idx("ix_activity_log_site_contact_id", "activity_log", ["site_contact_id"])
    _add_idx("ix_requisitions_updated_by_id", "requisitions", ["updated_by_id"])
    _add_idx("ix_offers_approved_by_id", "offers", ["approved_by_id"])
    _add_idx("ix_offers_updated_by_id", "offers", ["updated_by_id"])

    # ── CHECK constraints (18) ────────────────────────────────────────
    checks = [
        # requirements
        ("requirements", "chk_req_target_qty", "target_qty IS NULL OR target_qty >= 1"),
        ("requirements", "chk_req_target_price", "target_price IS NULL OR target_price >= 0"),
        ("requirements", "chk_req_condition", "condition IS NULL OR condition IN ('new','refurb','used')"),
        (
            "requirements",
            "chk_req_packaging",
            "packaging IS NULL OR packaging IN ('reel','tube','tray','bulk','cut_tape')",
        ),
        # sightings
        ("sightings", "chk_sight_qty", "qty_available IS NULL OR qty_available > 0"),
        ("sightings", "chk_sight_price", "unit_price IS NULL OR unit_price > 0"),
        ("sightings", "chk_sight_moq", "moq IS NULL OR moq > 0"),
        ("sightings", "chk_sight_confidence", "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)"),
        ("sightings", "chk_sight_score", "score IS NULL OR score >= 0"),
        ("sightings", "chk_sight_lead_time", "lead_time_days IS NULL OR lead_time_days >= 0"),
        ("sightings", "chk_sight_condition", "condition IS NULL OR condition IN ('new','refurb','used','other')"),
        (
            "sightings",
            "chk_sight_packaging",
            "packaging IS NULL OR packaging IN "
            "('reel','tube','tray','bulk','cut_tape','bag','box','each','strip','other')",
        ),
        # offers
        ("offers", "chk_offer_qty", "qty_available IS NULL OR qty_available > 0"),
        ("offers", "chk_offer_price", "unit_price IS NULL OR unit_price > 0"),
        ("offers", "chk_offer_moq", "moq IS NULL OR moq > 0"),
        ("offers", "chk_offer_condition", "condition IS NULL OR condition IN ('new','refurb','used','other')"),
        (
            "offers",
            "chk_offer_packaging",
            "packaging IS NULL OR packaging IN "
            "('reel','tube','tray','bulk','cut_tape','bag','box','each','strip','other')",
        ),
        ("offers", "chk_offer_status", "status IN ('active','expired','won','lost','pending_review','rejected')"),
    ]
    for table, name, check in checks:
        if not _constraint_exists(name):
            conn.execute(sa.text(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({check}) NOT VALID"))

    # ── Data backfills (6) ────────────────────────────────────────────
    # Backfill vendor_score from engagement_score
    conn.execute(
        sa.text("""
        UPDATE vendor_cards
        SET vendor_score = engagement_score,
            advancement_score = engagement_score,
            is_new_vendor = CASE WHEN engagement_score IS NULL THEN TRUE ELSE FALSE END
        WHERE vendor_score IS NULL AND engagement_score IS NOT NULL
    """)
    )

    # Backfill first_name/last_name from full_name
    conn.execute(
        sa.text("""
        UPDATE vendor_contacts
        SET first_name = SPLIT_PART(full_name, ' ', 1),
            last_name = CASE
                WHEN POSITION(' ' IN full_name) > 0
                THEN SUBSTRING(full_name FROM POSITION(' ' IN full_name) + 1)
                ELSE NULL
            END
        WHERE full_name IS NOT NULL AND first_name IS NULL
    """)
    )

    # Backfill occurred_at from created_at
    conn.execute(sa.text("UPDATE activity_log SET occurred_at = created_at WHERE occurred_at IS NULL"))

    # Backfill customer_sites.last_activity_at from parent company
    conn.execute(
        sa.text("""
        UPDATE customer_sites cs
        SET last_activity_at = c.last_activity_at
        FROM companies c
        WHERE cs.company_id = c.id
          AND cs.last_activity_at IS NULL
          AND c.last_activity_at IS NOT NULL
    """)
    )

    # Backfill site_contacts.is_active
    conn.execute(sa.text("UPDATE site_contacts SET is_active = TRUE WHERE is_active IS NULL"))

    # Backfill api_sources.is_active from status
    conn.execute(sa.text("UPDATE api_sources SET is_active = TRUE WHERE status = 'live' AND is_active = FALSE"))


# ---------------------------------------------------------------------------
# Downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    conn = op.get_bind()

    # ── Drop CHECK constraints ────────────────────────────────────────
    for name in [
        "chk_req_target_qty",
        "chk_req_target_price",
        "chk_req_condition",
        "chk_req_packaging",
        "chk_sight_qty",
        "chk_sight_price",
        "chk_sight_moq",
        "chk_sight_confidence",
        "chk_sight_score",
        "chk_sight_lead_time",
        "chk_sight_condition",
        "chk_sight_packaging",
        "chk_offer_qty",
        "chk_offer_price",
        "chk_offer_moq",
        "chk_offer_condition",
        "chk_offer_packaging",
        "chk_offer_status",
    ]:
        conn.execute(
            sa.text(
                f"ALTER TABLE requirements DROP CONSTRAINT IF EXISTS {name}; "
                f"ALTER TABLE sightings DROP CONSTRAINT IF EXISTS {name}; "
                f"ALTER TABLE offers DROP CONSTRAINT IF EXISTS {name}"
            )
        )

    # ── Drop indexes ──────────────────────────────────────────────────
    for idx in [
        "ix_requisitions_name_trgm",
        "ix_requisitions_customer_name_trgm",
        "ix_requirements_mpn_trgm",
        "ix_requirements_subs_trgm",
        "ix_vendor_cards_blacklisted",
        "ix_requirements_primary_mpn",
        "ix_offers_vendor_card",
        "ix_contacts_vendor_name",
        "ix_vendor_responses_vendor_name",
        "ix_offers_vendor_name",
        "ix_vendor_cards_score_computed_at",
        "ix_activity_log_customer_site_id",
        "ix_activity_log_site_contact_id",
        "ix_requisitions_updated_by_id",
        "ix_offers_approved_by_id",
        "ix_offers_updated_by_id",
    ]:
        conn.execute(sa.text(f"DROP INDEX IF EXISTS {idx}"))

    # ── Drop columns ──────────────────────────────────────────────────
    cols_to_drop = [
        ("vendor_cards", "vendor_score"),
        ("vendor_cards", "advancement_score"),
        ("vendor_cards", "is_new_vendor"),
        ("vendor_cards", "vendor_score_computed_at"),
        ("vendor_contacts", "first_name"),
        ("vendor_contacts", "last_name"),
        ("vendor_contacts", "phone_mobile"),
        ("vendor_contacts", "relationship_score"),
        ("vendor_contacts", "activity_trend"),
        ("vendor_contacts", "score_computed_at"),
        ("activity_log", "quote_id"),
        ("activity_log", "auto_logged"),
        ("activity_log", "occurred_at"),
        ("activity_log", "customer_site_id"),
        ("activity_log", "site_contact_id"),
        ("site_contacts", "is_active"),
        ("api_sources", "is_active"),
        ("material_cards", "deleted_at"),
        ("requirements", "substitutes_text"),
    ]
    for table, col in cols_to_drop:
        if _col_exists(table, col):
            op.drop_column(table, col)
