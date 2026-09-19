"""Video pipeline database layer (T1).

Enforcement of PostgreSQL in integrated mode lives in
``maia.video.settings.Settings._enforce_mode_gates`` (single source of truth);
this layer only wires engines/sessions. Offline mode (tests/dev) runs on
SQLite via ``resolve_database_url`` — an explicit, documented default, not a
silent degrade.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from maia.video.models import Base
from maia.video.settings import Settings


def make_engine(database_url: str) -> Engine:
    """Create an engine from a non-empty SQLAlchemy URL.

    Raises ValueError for empty/unsupported URLs (no silent fallbacks).
    """
    if not database_url.startswith(
        (
            "postgresql://",
            "postgresql+psycopg://",
            "postgresql+psycopg2://",
            "sqlite://",
        )
    ):
        raise ValueError(
            "Video pipeline database_url must be a postgresql:// or sqlite:// "
            f"SQLAlchemy URL, got: {database_url or '<empty>'!r}"
        )
    kwargs: dict = {"future": True}
    if database_url.startswith("sqlite://"):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_pre_ping"] = True
    return create_engine(database_url, **kwargs)


def resolve_database_url(settings: Settings) -> str:
    """Effective DB URL: configured URL, or (offline only) a SQLite file
    under ``VID_STORAGE_DIR``. Integrated mode always has a Postgres URL
    (Settings validator refuses empty)."""
    if settings.database_url:
        return settings.database_url
    if settings.mode == "integrated":
        raise ValueError("integrated mode requires VID_DATABASE_URL (PostgreSQL)")
    path = Path(settings.storage_dir)
    path.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path / 'video.db'}"


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def create_all(engine: Engine) -> None:
    """Create all video tables (offline/dev/tests convenience; production
    uses the Alembic migration)."""
    Base.metadata.create_all(engine)


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
