"""Shared Service Desk fixtures: real PostgreSQL, real auth dependency.

Isolation: each test gets a FRESH schema (drop_all/create_all) so constraint
and authorization tests never see stale rows. The API client exercises the
real auth dependency — policies are never disabled (plan T1).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from maia.auth import get_password_hash
from maia.servicedesk.api import create_app
from maia.servicedesk.db import make_engine, make_session_factory
from maia.servicedesk.models import (
    Base,
    SDIntegration,
    SDMembership,
    SDTicket,
    SDUser,
)
from maia.servicedesk.settings import Settings

TEST_DB_URL = "postgresql+psycopg://servicedesk:servicedesk@localhost:5433/servicedesk"
TENANT_A = "tenant-a"
TENANT_B = "tenant-b"
GROUP_NETWORK = "network"
GROUP_APP = "app"


def _settings() -> Settings:
    return Settings(mode="offline", database_url=TEST_DB_URL, _env_file=None)


@pytest.fixture()
def db_engine():
    engine = make_engine(TEST_DB_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def db_session(db_engine):
    factory = make_session_factory(db_engine)
    session = factory()
    yield session
    session.rollback()
    session.close()


@pytest.fixture()
def seeded(db_session):
    """tenant-a: alice(requester), agent1(agent@network), sup1(supervisor@network);
    tenant-b: bob(requester). Password for everyone: testpw123."""
    password_hash = get_password_hash("testpw123")

    def mkuser(email, name, tenant, role, group=None):
        user = SDUser(email=email, full_name=name, password_hash=password_hash)
        db_session.add(user)
        db_session.flush()
        db_session.add(
            SDMembership(user_id=user.id, tenant_id=tenant, role=role, group_id=group)
        )
        return user

    alice = mkuser("alice@t-a.test", "Alice", TENANT_A, "requester")
    agent1 = mkuser("agent1@t-a.test", "Agent One", TENANT_A, "agent", GROUP_NETWORK)
    sup1 = mkuser("sup1@t-a.test", "Sup One", TENANT_A, "supervisor", GROUP_NETWORK)
    bob = mkuser("bob@t-b.test", "Bob", TENANT_B, "requester")

    integration = SDIntegration(
        tenant_id=TENANT_A,
        base_url="https://sandbox.example.atlassian.net",
        project_key="ITSD",
        secret_ref="env:JIRA_API_TOKEN",
    )
    db_session.add(integration)
    db_session.flush()

    alice_ticket = SDTicket(
        tenant_id=TENANT_A,
        integration_id=integration.id,
        external_key="ITSD-1",
        summary="VPN cannot connect",
        description="Cisco VPN fails with error 809",
        requester_external_id="ext-alice",
        requester_user_id=alice.id,
        support_group=GROUP_NETWORK,
        version=1,
    )
    db_session.add(alice_ticket)
    db_session.commit()

    return {
        "users": {"alice": alice, "agent1": agent1, "sup1": sup1, "bob": bob},
        "integration": integration,
        "alice_ticket": alice_ticket,
    }


def _login(client: TestClient, email: str, tenant: str) -> dict:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "testpw123", "tenant_id": tenant},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture()
def client(db_engine, seeded):
    app = create_app(_settings())
    with TestClient(app) as c:
        c.headers_alice = _login(c, "alice@t-a.test", TENANT_A)
        c.headers_agent = _login(c, "agent1@t-a.test", TENANT_A)
        c.headers_sup = _login(c, "sup1@t-a.test", TENANT_A)
        c.headers_bob = _login(c, "bob@t-b.test", TENANT_B)
        c.seed = seeded
        yield c
