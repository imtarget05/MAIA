"""Alembic environment for the Service Desk schema (separate from MAIA's).

Migration URL resolution order:
1. ``sd_database_url`` attribute set programmatically by tests/CLI (wins).
2. ``SD_DATABASE_URL`` environment variable.
3. ``sqlalchemy.url`` from alembic_servicedesk.ini.

This env never imports ``maia.api`` (no legacy route/startup side effects).
"""
from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from maia.servicedesk.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str | None:
    override = getattr(context.config, "attributes", {}).get("sd_database_url")
    if override:
        return override
    return os.environ.get("SD_DATABASE_URL")


def run_migrations_offline() -> None:
    url = _database_url()
    if url:
        context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    else:
        context.configure(url=config.get_main_option("sqlalchemy.url"))
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    url = _database_url()
    if url:
        section["sqlalchemy.url"] = url
    connectable = engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
