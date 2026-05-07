"""env.py — Alembic Migration Environment for AVAIL AI.

Loads DATABASE_URL from app config, imports all SQLAlchemy models
so autogenerate can detect schema changes.

Business Rules:
- Always use transaction-per-migration for safety
- Never run migrations without a backup (see STABLE.md)

Called by: alembic CLI
Depends on: app.models (Base + all tables), app.config (settings)
"""

from logging.config import fileConfig

import sqlalchemy as sa
from sqlalchemy import engine_from_config, pool

from alembic import context, op
from app.config import Settings
from app.models import Base  # noqa: F401 — imports all models via Base.metadata


# ── Idempotent op wrappers ─────────────────────────────────────────────
# Migration 001 is the explicit-DDL baseline (today's full schema). The
# subsequent chain (002+) was written incrementally on top of an empty
# starting state, so every op.add_column / op.alter_column / op.create_*
# in 002+ targets an object that 001 has already created. Re-running the
# chain on a fresh DB therefore hits DuplicateColumn / DuplicateObject
# errors. We wrap the relevant op functions to no-op when the target
# already matches the desired state. Idempotent forward-migration is also
# a defensive win for replays in any environment.
def _column_exists(bind, table: str, column: str, schema: str | None = None) -> bool:
    insp = sa.inspect(bind)
    return column in {c["name"] for c in insp.get_columns(table, schema=schema)}


def _fk_exists(bind, table: str, fk_name: str, schema: str | None = None) -> bool:
    insp = sa.inspect(bind)
    return fk_name in {fk.get("name") for fk in insp.get_foreign_keys(table, schema=schema)}


def _unique_constraint_exists(bind, table: str, name: str, schema: str | None = None) -> bool:
    insp = sa.inspect(bind)
    return name in {uc.get("name") for uc in insp.get_unique_constraints(table, schema=schema)}


def _check_constraint_exists(bind, table: str, name: str, schema: str | None = None) -> bool:
    insp = sa.inspect(bind)
    return name in {cc.get("name") for cc in insp.get_check_constraints(table, schema=schema)}


def _table_exists(bind, table: str, schema: str | None = None) -> bool:
    insp = sa.inspect(bind)
    return table in insp.get_table_names(schema=schema)


_orig_add_column = op.add_column
_orig_alter_column = op.alter_column
_orig_create_foreign_key = op.create_foreign_key
_orig_create_unique_constraint = op.create_unique_constraint
_orig_create_check_constraint = op.create_check_constraint
_orig_drop_constraint = op.drop_constraint
_orig_create_table = op.create_table
_orig_drop_table = op.drop_table


def _idempotent_create_table(table_name, *columns, **kwargs):
    """Skip if table exists.

    Necessary because op.create_table emits implicit
    CREATE INDEX for every ``sa.Column(..., index=True)`` even when the outer
    CREATE TABLE IF NOT EXISTS no-ops, and those implicit indexes don't carry
    IF NOT EXISTS, producing DuplicateTable on chain replay.
    """
    if _table_exists(op.get_bind(), table_name, kwargs.get("schema")):
        return None
    kwargs.pop("if_not_exists", None)
    return _orig_create_table(table_name, *columns, **kwargs)


def _idempotent_drop_table(table_name, **kwargs):
    if not _table_exists(op.get_bind(), table_name, kwargs.get("schema")):
        return None
    kwargs.pop("if_exists", None)
    return _orig_drop_table(table_name, **kwargs)


def _idempotent_add_column(table_name, column, *, schema=None):
    if _column_exists(op.get_bind(), table_name, column.name, schema):
        return
    return _orig_add_column(table_name, column, schema=schema)


def _idempotent_alter_column(table_name, column_name, **kwargs):
    if not _column_exists(op.get_bind(), table_name, column_name, kwargs.get("schema")):
        return
    return _orig_alter_column(table_name, column_name, **kwargs)


def _idempotent_create_foreign_key(constraint_name, source_table, referent_table, *args, **kwargs):
    if constraint_name and _fk_exists(op.get_bind(), source_table, constraint_name, kwargs.get("source_schema")):
        return
    return _orig_create_foreign_key(constraint_name, source_table, referent_table, *args, **kwargs)


def _idempotent_create_unique_constraint(constraint_name, table_name, *args, **kwargs):
    if constraint_name and _unique_constraint_exists(op.get_bind(), table_name, constraint_name, kwargs.get("schema")):
        return
    return _orig_create_unique_constraint(constraint_name, table_name, *args, **kwargs)


def _idempotent_create_check_constraint(constraint_name, table_name, *args, **kwargs):
    if constraint_name and _check_constraint_exists(op.get_bind(), table_name, constraint_name, kwargs.get("schema")):
        return
    return _orig_create_check_constraint(constraint_name, table_name, *args, **kwargs)


def _idempotent_drop_constraint(constraint_name, table_name, type_=None, *, schema=None):
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = set()
    if type_ in (None, "foreignkey"):
        existing |= {fk.get("name") for fk in insp.get_foreign_keys(table_name, schema=schema)}
    if type_ in (None, "unique"):
        existing |= {uc.get("name") for uc in insp.get_unique_constraints(table_name, schema=schema)}
    if type_ in (None, "check"):
        existing |= {cc.get("name") for cc in insp.get_check_constraints(table_name, schema=schema)}
    if constraint_name not in existing:
        return
    return _orig_drop_constraint(constraint_name, table_name, type_=type_, schema=schema)


op.add_column = _idempotent_add_column
op.alter_column = _idempotent_alter_column
op.create_foreign_key = _idempotent_create_foreign_key
op.create_unique_constraint = _idempotent_create_unique_constraint
op.create_check_constraint = _idempotent_create_check_constraint
op.drop_constraint = _idempotent_drop_constraint
op.create_table = _idempotent_create_table
op.drop_table = _idempotent_drop_table

# Alembic Config object
config = context.config

# Set sqlalchemy.url from our app settings (not hardcoded in alembic.ini)
settings = Settings()
config.set_main_option("sqlalchemy.url", settings.database_url)

# Python logging from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Target metadata for autogenerate
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — generates SQL without connecting."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode — connects to DB and applies."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        # Pre-create the alembic_version table with a wider column. Some revision
        # IDs are >32 chars (e.g. 009_prospect_accounts_discovery_batches, 39 chars);
        # the default VARCHAR(32) is too narrow for them, and on a fresh DB the
        # widening migration (016) doesn't run before 009 hits the limit.
        # Creating the table up-front with VARCHAR(128) avoids the bootstrap race.
        connection.execute(
            sa.text(
                "CREATE TABLE IF NOT EXISTS alembic_version ("
                "version_num VARCHAR(128) NOT NULL, "
                "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)"
                ")"
            )
        )
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
