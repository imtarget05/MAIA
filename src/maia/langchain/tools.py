"""LangChain tool wrappers for MAIA's enterprise actions.

These are thin ``BaseTool`` wrappers around the *existing*
``agent.tools.TOOL_REGISTRY`` functions — so every tenant-authorization
check (P1-5, fail-closed), the mock HR DB, and the HRIS-with-mock-fallback
path are reused unchanged.  Making the tools LangChain-native is the
*LangChain* layer's job: it lets a node bind them onto the LLM (autonomous
tool-calling) OR dispatch them explicitly (the existing agent pattern).
"""
from __future__ import annotations

import json

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from ..agent.tools import (
    check_leave_balance as _balance,
)
from ..agent.tools import (
    create_it_ticket as _ticket,
)
from ..agent.tools import (
    create_leave_request as _leave,
)
from ..agent.tools import (
    get_employee_requests as _requests,
)

# ---------------------------------------------------------------------------
# Input schemas (typed tool params -> PydanticInputParser / JSON schema)
# ---------------------------------------------------------------------------


class BalanceInput(BaseModel):
    employee_id: str = Field(..., description="MAIA employee id, e.g. 'emp_001'")
    tenant_id: str = Field(default="default", description="Tenant for isolation")


class LeaveRequestInput(BaseModel):
    employee_id: str = Field(..., description="MAIA employee id")
    days: int = Field(..., gt=0, le=30, description="Number of leave days (1-30)")
    start_date: str | None = Field(default=None, description="Start date, e.g. '10/09' or '2026-09-10'")
    tenant_id: str = Field(default="default")


class ItTicketInput(BaseModel):
    employee_id: str = Field(..., description="MAIA employee id")
    ticket_type: str = Field(default="general",
                             description="vpn_request | lost_device | laptop_broken | security | general")
    description: str = Field(default="", description="Free-text description of the issue")
    tenant_id: str = Field(default="default")


class RequestsInput(BaseModel):
    employee_id: str = Field(..., description="MAIA employee id")
    tenant_id: str = Field(default="default")


# ---------------------------------------------------------------------------
# Tool implementations (delegate to existing TOOL_REGISTRY funcs)
# ---------------------------------------------------------------------------
# Each wrapper accepts **kwargs (the fields of its args_schema) so it is
# robust to how a particular langchain-core version forwards structured-tool
# input (positional model vs. unpacked kwargs).  We re-build the Pydantic
# model inside to get validation + defaults for free.


def _balance_run(**kwargs) -> str:
    inp = BalanceInput(**kwargs)
    res = _balance(inp.employee_id, tenant_id=inp.tenant_id)
    return json.dumps(res, ensure_ascii=False)


def _leave_run(**kwargs) -> str:
    inp = LeaveRequestInput(**kwargs)
    res = _leave(inp.employee_id, days=inp.days,
                 start_date=inp.start_date, tenant_id=inp.tenant_id)
    return json.dumps(res, ensure_ascii=False)


def _ticket_run(**kwargs) -> str:
    inp = ItTicketInput(**kwargs)
    res = _ticket(inp.employee_id, ticket_type=inp.ticket_type,
                  description=inp.description, tenant_id=inp.tenant_id)
    return json.dumps(res, ensure_ascii=False)


def _requests_run(**kwargs) -> str:
    inp = RequestsInput(**kwargs)
    res = _requests(inp.employee_id, tenant_id=inp.tenant_id)
    return json.dumps(res, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Registry: name -> LangChain BaseTool (parallels agent.tools.TOOL_REGISTRY)
# ---------------------------------------------------------------------------

TOOL_REGISTRY_LC: dict[str, BaseTool] = {
    "check_leave_balance": StructuredTool(
        name="check_leave_balance",
        description="Check an employee's remaining annual leave balance (days). "
                    "Use this BEFORE proposing a leave request so the balance "
                    "and eligibility can be cited. Returns JSON with balance + unit.",
        args_schema=BalanceInput,
        func=_balance_run,
    ),
    "create_leave_request": StructuredTool(
        name="create_leave_request",
        description="PROPOSE a leave request (days, start_date). This does NOT "
                    "create it yet — creation happens only after explicit human "
                    "approval via confirm_action(). Returns a pending proposal.",
        args_schema=LeaveRequestInput,
        func=_leave_run,
    ),
    "create_it_ticket": StructuredTool(
        name="create_it_ticket",
        description="PROPOSE an IT/security ticket (ticket_type, description). "
                    "Does NOT create it yet — approval required (confirm_action).",
        args_schema=ItTicketInput,
        func=_ticket_run,
    ),
    "get_employee_requests": StructuredTool(
        name="get_employee_requests",
        description="List the employee's recent leave requests / ticket history.",
        args_schema=RequestsInput,
        func=_requests_run,
    ),
}


# Backwards-compat: expose a flat list for ``llm.bind_tools([...])``
LANGCHAIN_TOOLS: list[BaseTool] = list(TOOL_REGISTRY_LC.values())

__all__ = ["LANGCHAIN_TOOLS", "TOOL_REGISTRY_LC", "tool_schemas"]


def tool_schemas() -> list[dict]:
    """JSON schemas for all MAIA tools (handy for LLM function-calling specs)."""
    return [
        {"name": t.name, "description": t.description,
         "parameters": t.args_schema.model_json_schema() if t.args_schema else {}}
        for t in LANGCHAIN_TOOLS
    ]
