"""T1 migration tests: alembic upgrade on a clean database + constraint
verification against real PostgreSQL (plan T1).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from alembic import command
from alembic.config import Config

from maia.servicedesk.db import make_engine
from tests.servicedesk.conftest import TEST_DB_URL

REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_TABLES = {
    "sd_users",
    "sd_memberships",
    "sd_integrations",
    "sd_tickets",
    "sd_ticket_comments",
    "sd_external_identities",
    "sd_drafts",
    "sd_proposals",
    "sd_jobs",
    "sd_inbox_events",
    "sd_outbox",
    "sd_audit_events",
    # T3 knowledge base
    "sd_documents",
    "sd_chunks",
}


def _alembic_upgrade(target: str) -> None:
    cfg = Config(str(REPO_ROOT / "alembic_servicedesk.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic_servicedesk"))
    cfg.attributes["sd_database_url"] = TEST_DB_URL
    command.upgrade(cfg, target)


@pytest.fixture()
def clean_engine():
    engine = make_engine(TEST_DB_URL)
    # Start from truly empty: drop everything (incl. previous test tables).
    Base = None  # placeholder to avoid unused import lint noise
    from maia.servicedesk.models import Base as Metadata

    Metadata.metadata.drop_all(engine)
    conn = engine.connect()
    conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
    conn.commit()
    conn.close()
    yield engine
    engine.dispose()


def test_upgrade_head_creates_all_sd_tables(clean_engine):
    _alembic_upgrade("head")
    tables = set(inspect(clean_engine).get_table_names())
    missing = EXPECTED_TABLES - tables
    assert not missing, f"missing tables after upgrade: {sorted(missing)}"
    assert "alembic_version" in tables


def test_upgrade_is_idempotent(clean_engine):
    _alembic_upgrade("head")
    _alembic_upgrade("head")  # second run must not fail
    tables = set(inspect(clean_engine).get_table_names())
    assert EXPECTED_TABLES <= tables


def test_downgrade_drops_sd_tables(clean_engine):
    from sqlalchemy import text

    _alembic_upgrade("head")
    with clean_engine.connect() as conn:
        stamped = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert stamped == "0001_service_desk", f"unexpected stamped revision: {stamped}"

    cfg = Config(str(REPO_ROOT / "alembic_servicedesk.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic_servicedesk"))
    cfg.attributes["sd_database_url"] = TEST_DB_URL
    command.downgrade(cfg, "base")
    tables = set(inspect(clean_engine).get_table_names())
    assert not (EXPECTED_TABLES & tables), f"tables survived downgrade: {sorted(EXPECTED_TABLES & tables)}"



def test_duplicate_integration_external_key_rejected(clean_engine, db_session):
    _alembic_upgrade("head")
    from maia.servicedesk.models import SDIntegration, SDTicket

    integration = SDIntegration(
        tenant_id="t1",
        base_url="https://x.example.atlassian.net",
        project_key="ITSD",
        secret_ref="env:TOKEN",
    )
    db_session.add(integration)
    db_session.flush()
    db_session.add(
        SDTicket(
            tenant_id="t1",
            integration_id=integration.id,
            external_key="ITSD-1",
            summary="first",
        )
    )
    db_session.commit()
    db_session.add(
        SDTicket(
            tenant_id="t1",
            integration_id=integration.id,
            external_key="ITSD-1",
            summary="dup",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_membership_role_check_constraint(clean_engine, db_session):
    _alembic_upgrade("head")
    from maia.servicedesk.models import SDMembership, SDUser
    from maia.auth import get_password_hash

    user = SDUser(email="x@x.test", password_hash=get_password_hash("pw"))
    db_session.add(user)
    db_session.flush()
    db_session.add(
        SDMembership(user_id=user.id, tenant_id="t1", role="root")  # invalid role
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
