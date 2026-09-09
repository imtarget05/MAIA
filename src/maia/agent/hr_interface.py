"""Shared abstract interface for HR/IT tool implementations.

Defines the common contract that both ``hris.py`` (real HRIS + mock fallback)
and ``tools.py`` (mock DB) implement. The ``TOOL_REGISTRY`` in tools.py
references implementations through this interface.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class HRInterface(Protocol):
    """Common interface for HR/IT tool operations.

    Both :mod:`maia.agent.hris` and :mod:`maia.agent.tools` implement
    these methods. Every method accepts an optional ``tenant_id`` for
    multi-tenant authorization.
    """

    def check_leave_balance(self, employee_id: str, tenant_id: str | None = None) -> dict:
        """Check remaining leave balance for an employee."""
        ...

    def create_leave_request(self, employee_id: str, days: int,
                               start_date: str | None, tenant_id: str | None = None) -> dict:
        """Create a leave request."""
        ...

    def create_it_ticket(self, employee_id: str, ticket_type: str,
                           description: str, tenant_id: str | None = None) -> dict:
        """Create an IT/security ticket."""
        ...

    def get_employee_requests(self, employee_id: str, tenant_id: str | None = None) -> list[dict]:
        """Get leave requests for an employee."""
        ...


__all__ = ["HRInterface"]
