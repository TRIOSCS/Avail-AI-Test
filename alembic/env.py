"""env.py — Alembic Migration Environment for AVAIL AI.

Loads DATABASE_URL from app config, imports all SQLAlchemy models
so autogenerate can detect schema changes.

Business Rules:
- Always use transaction-per-migration for safety
- Never run migrations without a backup (see STABLE.md)

Called by: alembic CLI
Depends on: app.models (Base + all tables), app.config (settings)
"""

import os
from logging.config import fileConfig

import sqlalchemy as sa
from loguru import logger
from sqlalchemy import engine_from_config, pool

from alembic import context, op
from app.config import Settings
from app.models import Base


# ── Idempotent op wrappers ─────────────────────────────────────────────
# Migration 001 is the explicit-DDL baseline (today's full schema). The
# subsequent chain (002+) was written incrementally on top of an empty
# starting state, so every schema op in 002+ targets an object that 001
# has already created. Re-running the chain on a fresh DB therefore hits
# DuplicateColumn / DuplicateObject errors. We wrap the relevant op
# functions to no-op when the target already matches the desired state.
# Idempotent forward-migration is also a defensive win for replays in any
# environment.
#
# The full set of wrapped ops (10):
#   create_table, drop_table, add_column, alter_column,
#   create_foreign_key, create_unique_constraint, create_check_constraint,
#   drop_constraint, create_index, drop_index
#
# Each wrapper also short-circuits when the parent table is missing, so
# chain replays survive earlier table drops (e.g. a3f9c1d82e47 dropping
# error_reports before a later migration tries to alter columns on it).
#
# Every wrapper that short-circuits emits a WARN-level log line so
# operators can distinguish "already-in-target-state, safe skip" from
# "user typo, real bug" — without it, silent skips would mask real
# developer errors.
def _skip(reason: str, **details) -> None:
    """Log a wrapper short-circuit so silent skips are visible in logs."""
    extra = " ".join(f"{k}={v!r}" for k, v in details.items())
    logger.warning(f"[alembic-idempotent] SKIP {reason} {extra}")


def _table_exists(bind, table: str, schema: str | None = None) -> bool:
    insp = sa.inspect(bind)
    return table in insp.get_table_names(schema=schema)


def _column_exists(bind, table: str, column: str, schema: str | None = None) -> bool:
    if not _table_exists(bind, table, schema):
        return False
    insp = sa.inspect(bind)
    return column in {c["name"] for c in insp.get_columns(table, schema=schema)}


def _index_exists(bind, table: str, index: str, schema: str | None = None) -> bool:
    if not _table_exists(bind, table, schema):
        return False
    insp = sa.inspect(bind)
    return index in {ix["name"] for ix in insp.get_indexes(table, schema=schema)}


def _fk_exists(bind, table: str, fk_name: str, schema: str | None = None) -> bool:
    if not _table_exists(bind, table, schema):
        return False
    insp = sa.inspect(bind)
    return fk_name in {fk.get("name") for fk in insp.get_foreign_keys(table, schema=schema) if fk.get("name")}


def _unique_constraint_exists(bind, table: str, name: str, schema: str | None = None) -> bool:
    if not _table_exists(bind, table, schema):
        return False
    insp = sa.inspect(bind)
    return name in {uc.get("name") for uc in insp.get_unique_constraints(table, schema=schema) if uc.get("name")}


def _check_constraint_exists(bind, table: str, name: str, schema: str | None = None) -> bool:
    if not _table_exists(bind, table, schema):
        return False
    insp = sa.inspect(bind)
    return name in {cc.get("name") for cc in insp.get_check_constraints(table, schema=schema) if cc.get("name")}


def _pk_constraint_name(bind, table: str, schema: str | None = None) -> str | None:
    if not _table_exists(bind, table, schema):
        return None
    insp = sa.inspect(bind)
    pk = insp.get_pk_constraint(table, schema=schema)
    return pk.get("name") if pk else None


_orig_add_column = op.add_column
_orig_alter_column = op.alter_column
_orig_create_foreign_key = op.create_foreign_key
_orig_create_unique_constraint = op.create_unique_constraint
_orig_create_check_constraint = op.create_check_constraint
_orig_drop_constraint = op.drop_constraint
_orig_create_table = op.create_table
_orig_drop_table = op.drop_table
_orig_create_index = op.create_index
_orig_drop_index = op.drop_index


def _idempotent_create_index(index_name, table_name, *args, **kwargs):
    """Skip if the table is gone (no table-level IF EXISTS in CREATE INDEX) OR the index
    is already there.

    The index-existence check is the load-bearing guard against the original CI
    failure: ``relation "ix_vr_scanned_by" already exists``. While the bulk
    transform also added ``if_not_exists=True`` to migration sites, this wrapper
    is the safety net for any site that was missed or any future migration that
    forgets the kwarg.
    """
    bind = op.get_bind()
    schema = kwargs.get("schema")
    if not _table_exists(bind, table_name, schema):
        _skip("create_index (table missing)", index=index_name, table=table_name)
        return None
    if index_name and _index_exists(bind, table_name, index_name, schema):
        _skip("create_index (index already exists)", index=index_name, table=table_name)
        return None
    return _orig_create_index(index_name, table_name, *args, **kwargs)


def _idempotent_drop_index(index_name, table_name=None, **kwargs):
    """Skip the drop if the table is missing.

    With ``ALEMBIC_ALLOW_CASCADE=1`` (chain-replay mode), also default
    ``if_exists=True`` so a downgrade that double-drops via duplicated
    chain logic (e.g. 049's idempotent downgrade clearing what 002's
    downgrade also tries to clear) doesn't blow up.

    Without the env var, the caller's explicit kwargs are passed through
    unchanged — so a real "upgrade never ran, this downgrade is bogus"
    error still surfaces in production single-step downgrades.
    """
    if table_name and not _table_exists(op.get_bind(), table_name, kwargs.get("schema")):
        _skip("drop_index (table missing)", index=index_name, table=table_name)
        return None
    if os.environ.get("ALEMBIC_ALLOW_CASCADE") == "1":
        kwargs.setdefault("if_exists", True)
    return _orig_drop_index(index_name, table_name=table_name, **kwargs)


def _idempotent_create_table(table_name, *columns, **kwargs):
    """Skip entirely (return None before touching the DB) if the table already exists.

    The wrapper short-circuits via `_table_exists` reflection — it does NOT pass
    `IF NOT EXISTS` through to alembic. The reason for the early return rather
    than an `IF NOT EXISTS` clause: alembic emits implicit `CREATE INDEX`
    statements for `sa.Column(..., index=True)` columns alongside the
    `CREATE TABLE`, and those implicit indexes have no `IF NOT EXISTS` clause.
    Returning early before alembic ever runs avoids the `DuplicateObject` error
    that the index emission would otherwise trigger on chain replay.

    Any caller-supplied `if_not_exists` kwarg is dropped (the reflection check
    has already done that job).
    """
    if _table_exists(op.get_bind(), table_name, kwargs.get("schema")):
        _skip("create_table (already exists)", table=table_name)
        return None
    kwargs.pop("if_not_exists", None)
    return _orig_create_table(table_name, *columns, **kwargs)


def _idempotent_drop_table(table_name, cascade: bool = False, **kwargs):
    """Drop table. CASCADE when caller opts in OR ``ALEMBIC_ALLOW_CASCADE=1``.

    Default behavior delegates to alembic's original DROP TABLE — no CASCADE
    — so populated-DB single-step downgrades fail loudly on
    DependentObjectsStillExist instead of silently destroying dependent FK
    rows. The env var ``ALEMBIC_ALLOW_CASCADE=1`` is the escape hatch for
    chain replays (e.g. CI's `alembic downgrade base`) where unwinding to
    base genuinely needs to drop dependents. Production envs leave the var
    unset so single-step downgrades surface dependency errors.

    Either way, every CASCADE is logged so operators can grep for unexpected
    cascades.
    """
    bind = op.get_bind()
    schema = kwargs.get("schema")
    if not _table_exists(bind, table_name, schema):
        _skip("drop_table (table missing)", table=table_name)
        return None
    use_cascade = cascade or os.environ.get("ALEMBIC_ALLOW_CASCADE") == "1"
    if use_cascade:
        quoted = f'"{schema}"."{table_name}"' if schema else f'"{table_name}"'
        source = "explicit cascade=True" if cascade else "ALEMBIC_ALLOW_CASCADE=1"
        logger.warning(f"[alembic-idempotent] CASCADE drop_table table={table_name!r} ({source})")
        op.execute(f"DROP TABLE {quoted} CASCADE")
        return None
    return _orig_drop_table(table_name, **kwargs)


def _idempotent_add_column(table_name, column, **kwargs):
    """Skip when target table is absent OR the column is already present.

    Accepts `**kwargs` to forward any future alembic kwargs (e.g. `if_exists`)
    that callers might pass.
    """
    bind = op.get_bind()
    schema = kwargs.get("schema")
    if not _table_exists(bind, table_name, schema):
        _skip("add_column (table missing)", table=table_name, column=column.name)
        return None
    if _column_exists(bind, table_name, column.name, schema):
        _skip("add_column (column already exists)", table=table_name, column=column.name)
        return None
    return _orig_add_column(table_name, column, **kwargs)


def _idempotent_alter_column(table_name, column_name, **kwargs):
    """Skip if the table is missing OR the column already matches the requested target.

    Special-cases the rename idiom `op.alter_column(t, old_name,
    new_column_name=new_name)`: if the source column is missing but the target column is
    already present, treat the rename as already-applied (skip silently). If neither old
    nor new exists, skip too.

    For type/nullability changes, inspect the current column descriptor and short-
    circuit only when the existing state already matches the requested target —
    otherwise PostgreSQL would silently accept a no-op ALTER COLUMN TYPE and mask real
    schema drift between 001 baseline and chain target.
    """
    bind = op.get_bind()
    schema = kwargs.get("schema")
    new_name = kwargs.get("new_column_name")
    if not _table_exists(bind, table_name, schema):
        _skip("alter_column (table missing)", table=table_name, column=column_name)
        return None
    src_exists = _column_exists(bind, table_name, column_name, schema)
    if not src_exists:
        if new_name and _column_exists(bind, table_name, new_name, schema):
            _skip(
                "alter_column (rename already applied)",
                table=table_name,
                column=column_name,
                new=new_name,
            )
            return None
        _skip("alter_column (column missing)", table=table_name, column=column_name)
        return None

    # Source column exists — compare current descriptor against requested target.
    insp = sa.inspect(bind)
    current_cols = {c["name"]: c for c in insp.get_columns(table_name, schema=schema)}
    current = current_cols[column_name]
    requested_type = kwargs.get("type_")
    requested_nullable = kwargs.get("nullable")
    # SQLAlchemy types compare poorly across dialect-vs-generic and
    # instance-vs-class; repr() comparison is pragmatic.
    type_mismatch = requested_type is not None and repr(current.get("type")) != repr(requested_type)
    nullable_mismatch = requested_nullable is not None and bool(current.get("nullable")) != bool(requested_nullable)

    # If neither type nor nullable is being changed (e.g. server_default-only
    # alter, or rename), fall through and call the original — the rename and
    # other-attribute paths still need to execute.
    has_change_kw = (requested_type is not None) or (requested_nullable is not None)
    if has_change_kw and not type_mismatch and not nullable_mismatch:
        _skip(
            "alter_column (already in target state)",
            table=table_name,
            column=column_name,
        )
        return None

    if type_mismatch or nullable_mismatch:
        logger.info(
            "[alembic-idempotent] alter_column applying "
            f"table={table_name!r} column={column_name!r} "
            f"before_type={current.get('type')!r} after_type={requested_type!r} "
            f"before_nullable={current.get('nullable')!r} after_nullable={requested_nullable!r}"
        )
    return _orig_alter_column(table_name, column_name, **kwargs)


def _idempotent_create_foreign_key(constraint_name, source_table, referent_table, *args, **kwargs):
    bind = op.get_bind()
    schema = kwargs.get("source_schema") or kwargs.get("schema")
    if not _table_exists(bind, source_table, schema):
        # Loud warning: a missing source table almost always means a misnamed
        # migration, not legitimate idempotency. Without this, a referentially-
        # broken DB ships silently.
        logger.warning(
            f"[alembic-idempotent] create_foreign_key SKIPPED "
            f"constraint={constraint_name!r} reason='source table missing' "
            f"source_table={source_table!r} referent_table={referent_table!r}"
        )
        _skip("create_foreign_key (source missing)", name=constraint_name, src=source_table)
        return None
    if not _table_exists(bind, referent_table, kwargs.get("referent_schema") or schema):
        logger.warning(
            f"[alembic-idempotent] create_foreign_key SKIPPED "
            f"constraint={constraint_name!r} reason='referent table missing' "
            f"source_table={source_table!r} referent_table={referent_table!r}"
        )
        _skip("create_foreign_key (referent missing)", name=constraint_name, ref=referent_table)
        return None
    if constraint_name and _fk_exists(bind, source_table, constraint_name, schema):
        _skip("create_foreign_key (already exists)", name=constraint_name, src=source_table)
        return None
    return _orig_create_foreign_key(constraint_name, source_table, referent_table, *args, **kwargs)


def _idempotent_create_unique_constraint(constraint_name, table_name, *args, **kwargs):
    bind = op.get_bind()
    schema = kwargs.get("schema")
    if not _table_exists(bind, table_name, schema):
        _skip("create_unique_constraint (table missing)", name=constraint_name, table=table_name)
        return None
    if constraint_name and _unique_constraint_exists(bind, table_name, constraint_name, schema):
        _skip("create_unique_constraint (already exists)", name=constraint_name, table=table_name)
        return None
    return _orig_create_unique_constraint(constraint_name, table_name, *args, **kwargs)


def _idempotent_create_check_constraint(constraint_name, table_name, *args, **kwargs):
    bind = op.get_bind()
    schema = kwargs.get("schema")
    if not _table_exists(bind, table_name, schema):
        _skip("create_check_constraint (table missing)", name=constraint_name, table=table_name)
        return None
    if constraint_name and _check_constraint_exists(bind, table_name, constraint_name, schema):
        _skip("create_check_constraint (already exists)", name=constraint_name, table=table_name)
        return None
    return _orig_create_check_constraint(constraint_name, table_name, *args, **kwargs)


def _idempotent_drop_constraint(constraint_name, table_name, type_=None, **kwargs):
    """Skip when table is missing or the named constraint isn't present.

    Handles `type_="primary"` via inspector.get_pk_constraint() in addition to
    foreignkey / unique / check. `**kwargs` forwarded to the original to allow
    for future alembic kwargs (e.g. `if_exists`).
    """
    bind = op.get_bind()
    schema = kwargs.get("schema")
    if not _table_exists(bind, table_name, schema):
        _skip("drop_constraint (table missing)", name=constraint_name, table=table_name)
        return None
    insp = sa.inspect(bind)
    existing: set[str | None] = set()
    if type_ in (None, "foreignkey"):
        existing |= {fk.get("name") for fk in insp.get_foreign_keys(table_name, schema=schema)}
    if type_ in (None, "unique"):
        existing |= {uc.get("name") for uc in insp.get_unique_constraints(table_name, schema=schema)}
    if type_ in (None, "check"):
        existing |= {cc.get("name") for cc in insp.get_check_constraints(table_name, schema=schema)}
    if type_ in (None, "primary"):
        pk = _pk_constraint_name(bind, table_name, schema)
        if pk:
            existing.add(pk)
    existing.discard(None)
    if constraint_name not in existing:
        _skip("drop_constraint (not present)", name=constraint_name, table=table_name)
        return None
    return _orig_drop_constraint(constraint_name, table_name, type_=type_, **kwargs)


op.add_column = _idempotent_add_column
op.alter_column = _idempotent_alter_column
op.create_foreign_key = _idempotent_create_foreign_key
op.create_unique_constraint = _idempotent_create_unique_constraint
op.create_check_constraint = _idempotent_create_check_constraint
op.drop_constraint = _idempotent_drop_constraint
op.create_table = _idempotent_create_table
op.drop_table = _idempotent_drop_table
op.create_index = _idempotent_create_index
op.drop_index = _idempotent_drop_index

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
        # Safe to run on already-stamped DBs because of `IF NOT EXISTS`; the
        # existing column width is preserved (CREATE TABLE IF NOT EXISTS is a
        # no-op when the table is present, so we never narrow an existing column).
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
