"""Video pipeline Alembic environment (separate from MAIA's and Service Desk's).

Migration URL resolution order:
1. ``vid_database_url`` attribute set programmatically by tests/CLI (wins).
2. ``VID_DATABASE_URL`` environment variable.
3. ``sqlalchemy.url`` from alembic_video.ini.

This env never imports ``maia.api`` (no legacy route/startup side effects).
"""
from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from maia.video.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str | None:
    override = getattr(context.config, "attributes", {}).get("vid_database_url")
    if override:
        return str(override)
    return os.environ.get("VID_DATABASE_URL") or config.get_main_option("sqlalchemy.url")


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Explicit URL wins (attributes > VID_DATABASE_URL env > ini default).
    url = _database_url()
    if url:
        config.set_main_option("sqlalchemy.url", url)
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
