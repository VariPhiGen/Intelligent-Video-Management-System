"""Alembic async migration environment for asyncpg."""
from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Import all models so Alembic can detect schema changes.
from backend.db import Base
import backend.models  # noqa: F401 — registers the core tables on Base.metadata

# Optional extensions declare tables of their own. They are discovered, never
# named — see backend/extensions. Without this, autogenerate would see their
# tables in the database but not in the metadata, and emit a DROP for each.
from backend import extensions  # noqa: E402
extensions.load_models()

config = context.config

# Override the URL from environment (supports DATABASE_URL env var in Docker)
db_url = os.environ.get("DATABASE_URL")
if db_url:
    config.set_main_option("sqlalchemy.url", db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Tables this chain must not touch:
#   * any extension's own tables — they belong to that extension's chain, and
#     proposing changes to them here would put them in the wrong migration;
#   * any alembic version table but our own. The compliance extension keeps its
#     state in alembic_version_compliance, and autogenerate saw that as an
#     unknown table and proposed DROPPING it — which would have erased that
#     extension's migration history. Found by running an autogenerate probe on
#     2026-08-28; the prefix match names no extension.
_EXCLUDED_TABLES = extensions.owned_tables()


def _include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table":
        return not (name in _EXCLUDED_TABLES or name.startswith("alembic_version"))
    parent = getattr(obj, "table", None)
    if parent is not None:
        return not (parent.name in _EXCLUDED_TABLES
                    or parent.name.startswith("alembic_version"))
    return True


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        include_object=_include_object,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
