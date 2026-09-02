"""218_prospect_screen_cache — persisted AI-screen verdict + buyer-ready cache (M5+M6).

Two write-through cache columns on ``prospect_accounts`` so the prospecting list's
AI-screen-on lane and the stats panel can filter/sort/aggregate in SQL instead of
loading the whole (only-grows) pool into Python and re-deriving each row's screen
verdict / buyer-ready flag on every request (audit M5/M6):

- ``ai_screen_verdict`` (String(32), nullable) — a flat, indexed mirror of
  ``enrichment_data->ai_screen->verdict`` (one of "pass" / "screened_out" /
  "insufficient_data", or NULL before the account is ever screened). The JSONB blob
  stays the source of truth.
- ``is_buyer_ready`` (Boolean, nullable) — a persisted mirror of the
  ``is_buyer_ready`` flag that ``app.services.prospect_priority.
  build_priority_snapshot()`` computes (that function + the model's
  ``_sync_buyer_ready_score`` listener remain the single source of truth — this
  migration does NOT re-derive the scoring formula in SQL, and the listener keeps
  both columns in lockstep for every write going forward).

Both columns get a plain btree index for the prospecting list's SQL sort/filter and
the stats panel's SQL aggregate (app/routers/htmx/prospecting.py).

Backfills, PG-dialect-guarded (SQLite test DB no-ops — no JSONB path operators there,
and app-level tests exercise the ORM/listener path directly instead):

- ``ai_screen_verdict`` — one UPDATE using the ``#>>`` JSONB path operator, exactly
  mirroring ``enrichment_data#>>'{ai_screen,verdict}'``.
- ``is_buyer_ready`` — a Python loop over the existing rows that replicates
  ``build_priority_snapshot()``'s scoring formula INLINE (deliberately import-free —
  see ``_compute_is_buyer_ready`` below — so this migration stays self-contained and
  immutable even if the service function is later refactored/renamed). The inline
  copy mirrors app/services/prospect_priority.py::build_priority_snapshot as it
  existed on 2026-08-29; a later change to that function only affects rows written
  after the change (via the ORM listener), never this one-time backfill.

Additive + reversible (downgrade drops both columns + their indexes; no data loss
beyond the cache values themselves — both are fully re-derivable from
enrichment_data / the scoring inputs already on the row). Round-tripped
upgrade -> downgrade -> upgrade on a THROWAWAY PostgreSQL 16 instance.
Chains onto 217_drop_dead_schema.

Revision ID: 218_prospect_screen_cache
Revises: 217_drop_dead_schema
"""

import sqlalchemy as sa

from alembic import op

revision = "218_prospect_screen_cache"
down_revision = "217_drop_dead_schema"
branch_labels = None
depends_on = None


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value) -> list:
    return value if isinstance(value, list) else []


def _compute_is_buyer_ready(row: dict) -> bool:
    """Inline replica of build_priority_snapshot()'s is_buyer_ready formula.

    Source of truth: app/services/prospect_priority.py::build_priority_snapshot (as
    of 2026-08-29). Deliberately copied here rather than imported so this migration
    stays self-contained (see module docstring) — do NOT re-derive this in SQL.
    ``row`` is a plain dict of the same columns build_priority_snapshot reads off a
    ProspectAccount instance.
    """
    fit = row.get("fit_score") or 0
    readiness = row.get("readiness_score") or 0
    signals = _as_dict(row.get("readiness_signals"))
    contacts = _as_list(row.get("contacts_preview"))
    similar = _as_list(row.get("similar_customers"))
    historical = _as_dict(row.get("historical_context"))
    enrichment = _as_dict(row.get("enrichment_data"))

    score = fit * 0.45 + readiness * 0.55
    proof_points = 0

    intent = _as_dict(signals.get("intent"))
    intent_strength = intent.get("strength")
    if intent_strength == "strong":
        score += 12
        proof_points += 1
    elif intent_strength == "moderate":
        score += 6
        proof_points += 1

    verified_contacts = sum(1 for c in contacts if isinstance(c, dict) and c.get("verified"))
    verified_dms = sum(
        1 for c in contacts if isinstance(c, dict) and c.get("verified") and c.get("seniority") == "decision_maker"
    )
    if verified_dms:
        score += 9 if verified_dms == 1 else 11
        proof_points += 1
    elif verified_contacts >= 2:
        score += 6
        proof_points += 1
    elif verified_contacts == 1:
        score += 3
        proof_points += 1

    warm_intro = _as_dict(enrichment.get("warm_intro"))
    if warm_intro.get("has_warm_intro"):
        warmth = (warm_intro.get("warmth") or "warm").lower()
        score += 10 if warmth == "hot" else 6
        proof_points += 1

    similar_names: list[str] = []
    for item in similar[:2]:
        name = (item.get("name") or "").strip() if isinstance(item, dict) else str(item).strip()
        if name:
            similar_names.append(name)
    if similar_names:
        score += min(6, len(similar_names) * 3)
        proof_points += 1

    quote_count = historical.get("quote_count", 0)
    if not isinstance(quote_count, (int, float)):
        quote_count = 0
    bought_before = bool(historical.get("bought_before"))
    quoted_before = bool(historical.get("quoted_before")) or quote_count > 0
    if bought_before:
        score += 8
        proof_points += 1
    elif quoted_before:
        score += 4
        proof_points += 1

    hiring = _as_dict(signals.get("hiring"))
    hiring_type = hiring.get("type")
    if hiring_type == "procurement":
        score += 4
        proof_points += 1
    elif hiring_type == "engineering":
        score += 2
        proof_points += 1

    if signals.get("new_procurement_hire") is True:
        score += 3
        proof_points += 1

    if (row.get("import_priority") or "").strip().lower() == "priority":
        score += 3

    buyer_ready_score = max(0, min(100, int(round(score))))
    return buyer_ready_score >= 70 and proof_points >= 1 and fit >= 50 and readiness >= 30


def upgrade() -> None:
    bind = op.get_bind()

    op.add_column("prospect_accounts", sa.Column("ai_screen_verdict", sa.String(length=32), nullable=True))
    op.create_index(
        "ix_prospect_accounts_ai_screen_verdict",
        "prospect_accounts",
        ["ai_screen_verdict"],
        unique=False,
    )
    op.add_column("prospect_accounts", sa.Column("is_buyer_ready", sa.Boolean(), nullable=True))
    op.create_index(
        "ix_prospect_accounts_is_buyer_ready",
        "prospect_accounts",
        ["is_buyer_ready"],
        unique=False,
    )

    if bind.dialect.name != "postgresql":
        return

    # ai_screen_verdict backfill: flat mirror of enrichment_data->ai_screen->verdict.
    # Rows with no 'ai_screen' key (never screened) resolve to NULL, matching the
    # nullable column default.
    op.execute("UPDATE prospect_accounts SET ai_screen_verdict = enrichment_data #>> '{ai_screen,verdict}'")

    # is_buyer_ready backfill: Python-side, formula NOT re-derived in SQL (see
    # _compute_is_buyer_ready above).
    prospect_accounts = sa.table(
        "prospect_accounts",
        sa.column("id", sa.Integer),
        sa.column("fit_score", sa.Integer),
        sa.column("readiness_score", sa.Integer),
        sa.column("readiness_signals", sa.JSON),
        sa.column("contacts_preview", sa.JSON),
        sa.column("similar_customers", sa.JSON),
        sa.column("historical_context", sa.JSON),
        sa.column("enrichment_data", sa.JSON),
        sa.column("import_priority", sa.String),
        # The write target — must be declared on the Core table or the UPDATE's
        # values(is_buyer_ready=...) raises "Unconsumed column names".
        sa.column("is_buyer_ready", sa.Boolean),
    )
    rows = (
        bind.execute(
            sa.select(
                prospect_accounts.c.id,
                prospect_accounts.c.fit_score,
                prospect_accounts.c.readiness_score,
                prospect_accounts.c.readiness_signals,
                prospect_accounts.c.contacts_preview,
                prospect_accounts.c.similar_customers,
                prospect_accounts.c.historical_context,
                prospect_accounts.c.enrichment_data,
                prospect_accounts.c.import_priority,
            )
        )
        .mappings()
        .all()
    )

    if not rows:
        return

    update_stmt = (
        prospect_accounts.update()
        .where(prospect_accounts.c.id == sa.bindparam("row_id"))
        .values(is_buyer_ready=sa.bindparam("is_buyer_ready_val"))
    )
    params = [{"row_id": row["id"], "is_buyer_ready_val": _compute_is_buyer_ready(dict(row))} for row in rows]
    bind.execute(update_stmt, params)


def downgrade() -> None:
    op.drop_index("ix_prospect_accounts_is_buyer_ready", table_name="prospect_accounts", if_exists=True)
    op.drop_column("prospect_accounts", "is_buyer_ready")
    op.drop_index("ix_prospect_accounts_ai_screen_verdict", table_name="prospect_accounts", if_exists=True)
    op.drop_column("prospect_accounts", "ai_screen_verdict")
