"""
env.py — Alembic Migration Environment for AVAIL AI

Loads DATABASE_URL from app config, imports all SQLAlchemy models
so autogenerate can detect schema changes.

Business Rules:
- Always use transaction-per-migration for safety
- Never run migrations without a backup (see STABLE.md)

Called by: alembic CLI
Depends on: app.models (Base + all tables), app.config (settings)
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import Settings
from app.models import Base  # noqa: F401 — imports all models via Base.metadata

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
