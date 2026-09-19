"""Service Desk database layer — PostgreSQL only, no /tmp fallback (S4).

Separate ``Base`` from legacy MAIA models: the Service Desk owns its own
schema (``sd_`` tables, alembic_servicedesk migrations) and must not inherit
or mutate legacy MAIA tables.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from maia.servicedesk.models import Base
from maia.servicedesk.settings import Settings


def make_engine(database_url: str) -> Engine:
    """Create a Postgres engine. Non-Postgres URLs are rejected outright so a
    misconfiguration can never silently degrade to SQLite (S4 constraint)."""
    if not database_url.startswith(("postgresql://", "postgresql+psycopg://")):
        raise ValueError(
            "Service Desk requires a PostgreSQL database_url "
            "(postgresql:// or postgresql+psycopg://), got "
            f"{database_url or '<empty>'!r}"
        )
    return create_engine(database_url, pool_pre_ping=True, future=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on any error."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def settings_from_env() -> Settings:
    """Load Settings from .env.servicedesk / SD_* environment."""
    return Settings()
