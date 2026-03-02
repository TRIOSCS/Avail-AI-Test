"""Add tagging tables for AI classification and entity propagation.

Creates 4 tables:
- tags: brand + commodity taxonomy
- material_tags: links tags to material cards with confidence/source
- entity_tags: propagated tags on vendors/customers with visibility gates
- tag_threshold_config: per-entity-type visibility thresholds

Seeds 46 commodity taxonomy tags and 4 default threshold config rows.

Revision ID: 042_add_tagging_tables
Revises: 041_add_notifications
"""

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "042_add_tagging_tables"
down_revision = "041_add_notifications"
branch_labels = None
depends_on = None

# 46 commodity taxonomy tags (organized by category)
COMMODITY_TAGS = [
    # Semiconductors
    "Microcontrollers (MCU)",
    "Microprocessors (MPU)",
    "Memory ICs",
    "FPGAs & PLDs",
    "Analog ICs",
    "Power Management ICs",
    "Interface ICs",
    "RF & Wireless ICs",
    "Sensors",
    "Optoelectronics",
    "Discrete Semiconductors",
    "ASICs",
    "DSPs",
    "Logic ICs",
    # Passives
    "Capacitors",
    "Resistors",
    "Inductors",
    "Transformers",
    "Crystals & Oscillators",
    "Filters",
    # Electromechanical
    "Connectors",
    "Relays",
    "Switches",
    "Circuit Protection",
    "Terminal Blocks",
    # PC / Server
    "Server CPUs",
    "Server Memory (DIMMs)",
    "Hard Drives / SSDs",
    "Network Cards",
    "Power Supplies",
    "Fans & Thermal",
    "Server Boards",
    # Other
    "Cables & Wire",
    "PCBs & Substrates",
    "Displays",
    "Batteries",
    "Enclosures & Hardware",
    "Test & Measurement",
    "Miscellaneous",
]

THRESHOLD_SEEDS = [
    ("vendor", "brand", 2, 0.05),
    ("vendor", "commodity", 3, 0.05),
    ("customer", "brand", 3, 0.05),
    ("customer", "commodity", 3, 0.05),
]


def upgrade() -> None:
    # ── tags ──────────────────────────────────────────────────────────
    op.create_table(
        "tags",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("tag_type", sa.String(20), nullable=False),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("tags.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name", "tag_type", name="uq_tags_name_type"),
    )
    op.create_index("ix_tags_tag_type", "tags", ["tag_type"])

    # ── material_tags ─────────────────────────────────────────────────
    op.create_table(
        "material_tags",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("material_card_id", sa.Integer(), sa.ForeignKey("material_cards.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tag_id", sa.Integer(), sa.ForeignKey("tags.id", ondelete="CASCADE"), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("classified_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("material_card_id", "tag_id", name="uq_material_tags_card_tag"),
    )
    op.create_index("ix_material_tags_tag_id", "material_tags", ["tag_id"])
    op.create_index("ix_material_tags_source", "material_tags", ["source"])

    # ── entity_tags ───────────────────────────────────────────────────
    op.create_table(
        "entity_tags",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entity_type", sa.String(20), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("tag_id", sa.Integer(), sa.ForeignKey("tags.id", ondelete="CASCADE"), nullable=False),
        sa.Column("interaction_count", sa.Float(), nullable=False, server_default="0"),
        sa.Column("total_entity_interactions", sa.Float(), nullable=False, server_default="0"),
        sa.Column("is_visible", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("entity_type", "entity_id", "tag_id", name="uq_entity_tags_type_id_tag"),
    )
    op.create_index("ix_entity_tags_type_tag_visible", "entity_tags", ["entity_type", "tag_id", "is_visible"])
    op.create_index("ix_entity_tags_type_id", "entity_tags", ["entity_type", "entity_id"])

    # ── tag_threshold_config ──────────────────────────────────────────
    op.create_table(
        "tag_threshold_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entity_type", sa.String(20), nullable=False),
        sa.Column("tag_type", sa.String(20), nullable=False),
        sa.Column("min_count", sa.Integer(), nullable=False),
        sa.Column("min_percentage", sa.Float(), nullable=False),
        sa.UniqueConstraint("entity_type", "tag_type", name="uq_threshold_entity_tag"),
    )

    # ── Seed commodity taxonomy ───────────────────────────────────────
    tags_table = sa.table(
        "tags",
        sa.column("name", sa.String),
        sa.column("tag_type", sa.String),
        sa.column("created_at", sa.DateTime),
    )
    op.bulk_insert(
        tags_table,
        [
            {"name": name, "tag_type": "commodity", "created_at": datetime.now(timezone.utc)}
            for name in COMMODITY_TAGS
        ],
    )

    # ── Seed threshold config ─────────────────────────────────────────
    config_table = sa.table(
        "tag_threshold_config",
        sa.column("entity_type", sa.String),
        sa.column("tag_type", sa.String),
        sa.column("min_count", sa.Integer),
        sa.column("min_percentage", sa.Float),
    )
    op.bulk_insert(
        config_table,
        [
            {"entity_type": et, "tag_type": tt, "min_count": mc, "min_percentage": mp}
            for et, tt, mc, mp in THRESHOLD_SEEDS
        ],
    )


def downgrade() -> None:
    op.drop_table("tag_threshold_config")
    op.drop_index("ix_entity_tags_type_id")
    op.drop_index("ix_entity_tags_type_tag_visible")
    op.drop_table("entity_tags")
    op.drop_index("ix_material_tags_source")
    op.drop_index("ix_material_tags_tag_id")
    op.drop_table("material_tags")
    op.drop_index("ix_tags_tag_type")
    op.drop_table("tags")
