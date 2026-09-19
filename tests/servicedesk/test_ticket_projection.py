"""T2 projection tests (plan T2 RED list).

Covers: stale remote snapshot cannot revert a newer projection, the plan's
literal `upsert_snapshot` CAS test, cross-integration isolation for the same
external key, and tenant consistency.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from maia.servicedesk.models import SDIntegration
from maia.servicedesk.tickets import TicketService
from tests.servicedesk.conftest import TENANT_A, TENANT_B


def snapshot(key: str, summary: str, updated: str, reporter: str = "acct-1") -> dict:
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "description": {"type": "doc", "content": [
                {"type": "paragraph", "content": [
                    {"type": "text", "text": "description body"}
                ]}
            ]},
            "status": {"name": "To Do"},
            "priority": {"name": "Medium"},
            "reporter": {"accountId": reporter},
            "updated": updated,
        },
    }


class JiraSnapshots:
    """Plan-style fixture naming: jira_snapshots.newer / .older.

    The plan's literal assertion (``get_version == newer_version == 2``)
    models a ticket that was already ingested once: initial -> newer (v2)
    -> older must be ignored and leave v2 in place.
    """

    initial = snapshot("ITSD-1", "VPN cannot connect (initial)", "2026-09-19T11:00:00.000+0000")
    newer = snapshot("ITSD-1", "VPN cannot connect (updated)", "2026-09-19T13:00:00.000+0000")
    older = snapshot("ITSD-1", "VPN cannot connect (old)", "2026-09-19T12:00:00.000+0000")
    newer_version = 2


def make_integration(session: Session, tenant: str, key: str = "ITSD") -> SDIntegration:
    integration = SDIntegration(
        tenant_id=tenant,
        base_url="https://sandbox.example.atlassian.net",
        project_key=key,
        secret_ref="env:JIRA_API_TOKEN",
    )
    session.add(integration)
    session.commit()
    return integration


def test_remote_old_snapshot_cannot_revert_ticket(db_session):
    integration = make_integration(db_session, TENANT_A)
    service = TicketService(db_session)
    snapshots = JiraSnapshots()

    ticket_id = service.upsert_snapshot(integration.id, snapshots.initial)
    assert service.get_version(ticket_id) == 1
    service.upsert_snapshot(integration.id, snapshots.newer)
    assert service.get_version(ticket_id) == snapshots.newer_version

    # A late/out-of-order webhook echo must not revert the projection.
    service.upsert_snapshot(integration.id, snapshots.older)

    assert service.get_version(ticket_id) == snapshots.newer_version
    ticket = service.get_ticket(ticket_id)
    assert "updated" in ticket.summary


def test_newer_snapshot_bumps_version_and_applies_fields(db_session):
    integration = make_integration(db_session, TENANT_A)
    service = TicketService(db_session)

    first = service.upsert_snapshot(
        integration.id, snapshot("ITSD-2", "first", "2026-09-19T12:00:00.000+0000")
    )
    assert service.get_version(first) == 1

    later = snapshot("ITSD-2", "second", "2026-09-19T12:30:00.000+0000")
    assert service.upsert_snapshot(integration.id, later) == first
    assert service.get_version(first) == 2
    assert service.get_ticket(first).summary == "second"


def test_same_external_key_in_two_integrations_never_mixes(db_session):
    integration_a = make_integration(db_session, TENANT_A)
    integration_b = make_integration(db_session, TENANT_B)

    service = TicketService(db_session)
    ticket_a = service.upsert_snapshot(
        integration_a.id, snapshot("ITSD-1", "tenant A ticket", "2026-09-19T12:00:00.000+0000")
    )
    ticket_b = service.upsert_snapshot(
        integration_b.id, snapshot("ITSD-1", "tenant B ticket", "2026-09-19T12:00:00.000+0000")
    )

    assert ticket_a != ticket_b
    assert service.get_ticket(ticket_a).tenant_id == TENANT_A
    assert service.get_ticket(ticket_b).tenant_id == TENANT_B


def test_projection_keeps_integration_tenant(db_session):
    integration = make_integration(db_session, TENANT_B)
    service = TicketService(db_session)
    ticket_id = service.upsert_snapshot(
        integration.id, snapshot("ITSD-7", "tenant B", "2026-09-19T12:00:00.000+0000")
    )
    ticket = service.get_ticket(ticket_id)
    assert ticket.tenant_id == TENANT_B
    assert ticket.requester_external_id == "acct-1"
    assert ticket.description == "description body"
    assert ticket.remote_status == "To Do"


def test_unknown_integration_rejected(db_session):
    from uuid import uuid4

    import pytest

    service = TicketService(db_session)
    with pytest.raises(ValueError, match="unknown integration"):
        service.upsert_snapshot(
            uuid4(), snapshot("ITSD-9", "x", "2026-09-19T12:00:00.000+0000")
        )


def test_snapshot_missing_key_rejected(db_session):
    import pytest

    integration = make_integration(db_session, TENANT_A)
    service = TicketService(db_session)
    with pytest.raises(ValueError, match="key and summary"):
        service.upsert_snapshot(integration.id, {"fields": {"summary": "x"}})