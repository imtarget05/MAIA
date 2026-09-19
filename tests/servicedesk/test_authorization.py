"""T1 authorization RED→GREEN tests (plan T1).

Covers: cross-tenant 404, group scoping, admin without group bypass,
client-supplied tenant cannot upgrade, forged role claims gain nothing,
revoked membership loses access, wrong-tenant login rejected, and
proposal-authority role rules.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from maia.auth import get_password_hash
from maia.servicedesk.models import SDIntegration, SDMembership, SDTicket, SDUser
from maia.servicedesk.policies import NotAuthorized, OutOfScope, authorize_proposal
from tests.servicedesk.conftest import TENANT_A, TENANT_B


def _make_tenant_b_ticket(db_session, seeded, bob):
    integration_b = SDIntegration(
        tenant_id=TENANT_B,
        base_url="https://other.example.atlassian.net",
        project_key="ITSD",
        secret_ref="env:JIRA_API_TOKEN_B",
    )
    db_session.add(integration_b)
    db_session.flush()
    ticket = SDTicket(
        tenant_id=TENANT_B,
        integration_id=integration_b.id,
        external_key="ITSD-1",  # same external key as the tenant-a ticket
        summary="Bob's access request",
        requester_external_id="ext-bob",
        requester_user_id=bob.id,
        support_group="network-b",
        version=1,
    )
    db_session.add(ticket)
    db_session.commit()
    return ticket


def _make_group_ticket(db_session, seeded, group: str, key: str) -> SDTicket:
    ticket = SDTicket(
        tenant_id=TENANT_A,
        integration_id=seeded["integration"].id,
        external_key=key,
        summary=f"Ticket for {group}",
        support_group=group,
        version=1,
    )
    db_session.add(ticket)
    db_session.commit()
    return ticket


def test_requester_cannot_read_other_ticket(client: TestClient, db_session, seeded):
    bobs_ticket = _make_tenant_b_ticket(db_session, seeded, seeded["users"]["bob"])
    response = client.get(
        f"/api/v1/tickets/{bobs_ticket.id}", headers=client.headers_alice
    )
    assert response.status_code == 404


def test_requester_sees_own_ticket(client: TestClient):
    ticket_id = client.seed["alice_ticket"].id
    response = client.get(f"/api/v1/tickets/{ticket_id}", headers=client.headers_alice)
    assert response.status_code == 200
    assert response.json()["external_key"] == "ITSD-1"


def test_agent_scope_limited_to_assigned_groups(client: TestClient, db_session, seeded):
    app_ticket = _make_group_ticket(db_session, seeded, "app", "ITSD-9")
    alice_ticket_id = client.seed["alice_ticket"].id

    ok = client.get(f"/api/v1/tickets/{alice_ticket_id}", headers=client.headers_agent)
    assert ok.status_code == 200

    blocked = client.get(
        f"/api/v1/tickets/{app_ticket.id}", headers=client.headers_agent
    )
    assert blocked.status_code == 404


def test_supervisor_reads_within_group(client: TestClient):
    ticket_id = client.seed["alice_ticket"].id
    response = client.get(f"/api/v1/tickets/{ticket_id}", headers=client.headers_sup)
    assert response.status_code == 200


# --- part 2: upgrade paths, forgery, revocation, authority rules ---


def test_admin_has_no_group_bypass(client: TestClient, db_session, seeded):
    admin = SDUser(
        email="admin@t-a.test",
        full_name="Admin",
        password_hash=get_password_hash("testpw123"),
    )
    db_session.add(admin)
    db_session.flush()
    db_session.add(
        SDMembership(user_id=admin.id, tenant_id=TENANT_A, role="admin", group_id=None)
    )
    db_session.commit()

    login = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@t-a.test", "password": "testpw123", "tenant_id": TENANT_A},
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    ticket_id = client.seed["alice_ticket"].id
    assert client.get(f"/api/v1/tickets/{ticket_id}", headers=headers).status_code == 404


def test_client_supplied_tenant_cannot_upgrade_access(
    client: TestClient, db_session, seeded
):
    ticket_id = client.seed["alice_ticket"].id
    response = client.get(
        f"/api/v1/tickets/{ticket_id}",
        headers={**client.headers_bob, "X-Tenant-Id": TENANT_A},
    )
    assert response.status_code == 404


def test_forged_role_claim_grants_nothing(client: TestClient, db_session, seeded):
    """Forged claims never grant authority: a token minted with
    'role': 'admin' (1) is rejected outright for a tenant where the user has
    no membership (401), and (2) resolves to the DB role, not the claim, in
    the user's real tenant."""
    from maia.auth import create_access_token

    bob_id = str(seeded["users"]["bob"].id)

    # (1) forged tenant + role: no membership in tenant-a -> unauthenticated
    forged = create_access_token(
        data={"sub": bob_id, "tid": TENANT_A, "role": "admin"}
    )
    ticket_id = client.seed["alice_ticket"].id
    response = client.get(
        f"/api/v1/tickets/{ticket_id}", headers={"Authorization": f"Bearer {forged}"}
    )
    assert response.status_code in (401, 404)
    assert response.status_code != 200

    # (2) real tenant + forged role claim: /me must report the DB role
    forged_home = create_access_token(
        data={"sub": bob_id, "tid": TENANT_B, "role": "admin"}
    )
    me = client.get("/api/v1/me", headers={"Authorization": f"Bearer {forged_home}"})
    assert me.status_code == 200
    assert me.json()["role"] == "requester"


def test_revoked_membership_loses_access_immediately(client: TestClient, db_session):
    response = client.get("/api/v1/me", headers=client.headers_agent)
    assert response.status_code == 200
    assert response.json()["role"] == "agent"

    db_session.query(SDMembership).filter(
        SDMembership.tenant_id == TENANT_A, SDMembership.role == "agent"
    ).delete()
    db_session.commit()

    assert client.get("/api/v1/me", headers=client.headers_agent).status_code == 401


def test_wrong_tenant_login_rejected(client: TestClient):
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "alice@t-a.test", "password": "testpw123", "tenant_id": TENANT_B},
    )
    assert response.status_code == 401


def test_me_reports_server_side_membership(client: TestClient):
    me = client.get("/api/v1/me", headers=client.headers_agent).json()
    assert me["role"] == "agent"
    assert me["group_ids"] == ["network"]
    assert me["tenant_id"] == TENANT_A


def test_authorize_proposal_role_rules(db_session, seeded):
    from maia.servicedesk.schemas import Principal

    ticket = seeded["alice_ticket"]
    agent = Principal(
        user_id=seeded["users"]["agent1"].id,
        tenant_id=TENANT_A,
        role="agent",
        group_ids=frozenset({"network"}),
    )
    requester = Principal(
        user_id=seeded["users"]["alice"].id,
        tenant_id=TENANT_A,
        role="requester",
        group_ids=frozenset(),
    )
    outsider = Principal(
        user_id=seeded["users"]["bob"].id,
        tenant_id=TENANT_B,
        role="agent",
        group_ids=frozenset({"network-b"}),
    )

    authorize_proposal(agent, ticket, "add_comment")  # allowed

    with pytest.raises(NotAuthorized):
        authorize_proposal(agent, ticket, "set_priority")

    with pytest.raises(NotAuthorized):
        authorize_proposal(requester, ticket, "add_comment")

    with pytest.raises(OutOfScope):
        authorize_proposal(outsider, ticket, "add_comment")


import pytest  # noqa: E402  (kept at bottom to keep the test bodies above tight)
