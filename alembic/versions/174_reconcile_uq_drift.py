"""Reconcile the remaining #464 schema drift: 21 unique constraints, 3 dead-table drops,
1 column comment.

What it does: (1) creates the 21 unique constraints the models declare but the
migration-built baseline never created (every target was duplicate-checked clean
on the live PG on 2026-07-02 before this migration was written); (2) drops the
three model-less legacy tables the old chain creates — ``buy_plans`` (V1;
superseded by ``buy_plans_v3`` via migration 076), ``notification_engagement``
and ``self_heal_log`` — all guarded with IF EXISTS because the live staging DB
already lacks them (manually dropped long ago); no inbound FKs reference any of
the three (verified via pg_constraint); (3) sets the
``material_cards.enrichment_status`` column comment so model and DB agree.
What calls it: ``alembic upgrade head`` (deploy + migration-full-cycle CI).
Depends on: 173_approvals_workflow_fold; scripts/check_schema_matches_models.py
drops the matching ``_GRANDFATHERED_*`` entries in the same PR, so the drift
gate enforces all of this for real from now on.

Revision ID: 174_reconcile_uq_drift
Revises: 173_approvals_workflow_fold
"""

import sqlalchemy as sa

from alembic import op

revision = "174_reconcile_uq_drift"
down_revision = "173_approvals_workflow_fold"
branch_labels = None
depends_on = None

# (constraint name, table, columns) — names and column ORDER match the model
# declarations exactly (9 are explicitly named in __table_args__; the 12 unnamed
# ``unique=True`` singles get conventional uq_* names, which alembic's autogen
# matches by column signature).
_CONSTRAINTS: list[tuple[str, str, list[str]]] = [
    # Explicitly named in the models — names must match verbatim.
    ("uq_css_commodity_spec_key", "commodity_spec_schemas", ["commodity", "spec_key"]),
    ("uq_vss_req_vendor", "vendor_sighting_summary", ["requirement_id", "vendor_name"]),
    ("uq_tags_name_type", "tags", ["name", "tag_type"]),
    ("uq_material_tags_card_tag", "material_tags", ["material_card_id", "tag_id"]),
    ("uq_entity_tags_type_id_tag", "entity_tags", ["entity_type", "entity_id", "tag_id"]),
    ("uq_threshold_entity_tag", "tag_threshold_config", ["entity_type", "tag_type"]),
    (
        "uq_sourcing_lead_requirement_vendor_part",
        "sourcing_leads",
        ["requirement_id", "vendor_name_normalized", "part_number_matched"],
    ),
    (
        "uq_cph_company_card_source",
        "customer_part_history",
        ["company_id", "material_card_id", "source"],
    ),
    ("uq_msf_card_spec", "material_spec_facets", ["material_card_id", "spec_key"]),
    # Unnamed ``unique=True`` single columns in the models.
    ("uq_api_sources_name", "api_sources", ["name"]),
    ("uq_discovery_batches_batch_id", "discovery_batches", ["batch_id"]),
    ("uq_users_email", "users", ["email"]),
    ("uq_users_azure_id", "users", ["azure_id"]),
    ("uq_email_sig_extracts_sender", "email_signature_extracts", ["sender_email"]),
    ("uq_enrichment_runs_run_id", "enrichment_runs", ["run_id"]),
    ("uq_knowledge_config_key", "knowledge_config", ["key"]),
    ("uq_graph_subs_subscription_id", "graph_subscriptions", ["subscription_id"]),
    ("uq_verification_group_members_user", "verification_group_members", ["user_id"]),
    ("uq_prospect_accounts_domain", "prospect_accounts", ["domain"]),
    ("uq_quotes_quote_number", "quotes", ["quote_number"]),
    ("uq_trouble_tickets_number", "trouble_tickets", ["ticket_number"]),
]

_ENRICHMENT_STATUS_COMMENT = (
    "unenriched|verified|web_sourced|oem_sourced|ai_inferred|not_found|not_catalogued (see MaterialEnrichmentStatus)"
)


# Model-less legacy tables the old chain creates (001 / 040 / 062). Already absent
# on live staging; dropped here so migration-built DBs match the models too.
_DEAD_TABLES = ("self_heal_log", "notification_engagement", "buy_plans")

# Exact recreation DDL for downgrade, captured via pg_dump --schema-only from a
# migration-built throwaway PG at head 173 (OWNER/psql-meta lines removed).
_DEAD_TABLE_DDL = """
CREATE TABLE buy_plans (
    id SERIAL PRIMARY KEY,
    requisition_id integer REFERENCES requisitions(id),
    quote_id integer REFERENCES quotes(id),
    status character varying(30) NOT NULL,
    line_items json,
    manager_notes text,
    salesperson_notes text,
    rejection_reason text,
    sales_order_number character varying(100),
    submitted_by_id integer REFERENCES users(id),
    approved_by_id integer REFERENCES users(id),
    completed_by_id integer REFERENCES users(id),
    cancelled_by_id integer REFERENCES users(id),
    submitted_at timestamp without time zone,
    approved_at timestamp without time zone,
    rejected_at timestamp without time zone,
    completed_at timestamp without time zone,
    cancelled_at timestamp without time zone,
    cancellation_reason text,
    approval_token character varying(100),
    token_expires_at timestamp without time zone,
    is_stock_sale boolean,
    created_at timestamp without time zone
);
CREATE INDEX ix_buyplans_token ON buy_plans USING btree (approval_token);
CREATE TABLE notification_engagement (
    id SERIAL PRIMARY KEY,
    user_id integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_type character varying(50) NOT NULL,
    entity_id character varying(100) NOT NULL,
    delivery_method character varying(20) DEFAULT 'dm'::character varying NOT NULL,
    action character varying(20) NOT NULL,
    response_time_s double precision,
    ai_priority character varying(20),
    ai_confidence double precision,
    suppression_reason text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE INDEX ix_notif_engage_created ON notification_engagement USING btree (created_at);
CREATE INDEX ix_notif_engage_user_action ON notification_engagement USING btree (user_id, action);
CREATE INDEX ix_notif_engage_user_event ON notification_engagement USING btree (user_id, event_type);
CREATE TABLE self_heal_log (
    id SERIAL PRIMARY KEY,
    ticket_id integer NOT NULL REFERENCES trouble_tickets(id) ON DELETE CASCADE,
    category character varying(20),
    risk_tier character varying(10),
    files_modified json,
    fix_succeeded boolean,
    iterations_used integer,
    cost_usd double precision,
    user_verified boolean,
    created_at timestamp without time zone
);
CREATE INDEX ix_self_heal_log_created_at ON self_heal_log USING btree (created_at);
CREATE INDEX ix_self_heal_log_ticket_id ON self_heal_log USING btree (ticket_id);
"""


def upgrade() -> None:
    for name, table, cols in _CONSTRAINTS:
        op.create_unique_constraint(name, table, cols)
    for table in _DEAD_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.alter_column(
        "material_cards",
        "enrichment_status",
        existing_type=sa.String(20),
        existing_nullable=False,
        existing_server_default="unenriched",
        comment=_ENRICHMENT_STATUS_COMMENT,
    )


def downgrade() -> None:
    op.alter_column(
        "material_cards",
        "enrichment_status",
        existing_type=sa.String(20),
        existing_nullable=False,
        existing_server_default="unenriched",
        comment=None,
    )
    op.execute(_DEAD_TABLE_DDL)
    for name, table, _cols in reversed(_CONSTRAINTS):
        op.drop_constraint(name, table, type_="unique")
