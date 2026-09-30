"""MCP tool-calling for the agent: plan → allowlist → execute → audit.

The design keeps what an enterprise reviewer asks about in one file:

* **The LLM proposes, the registry disposes.** A tool call runs only when the
  tool is (a) advertised by a connected server, (b) on the allowlist, and
  (c) within the per-turn budget. Model output never reaches an integration
  directly.
* **Two planning paths.** ``plan_from_llm`` would consume a model's tool-call
  array; ``plan_offline`` is the deterministic keyword router used when no LLM is
  wired (CI, demos, and as the fixture for the LLM path's own test). The offline
  router is what makes the whole chain testable without a gateway.
* **Budgets and idempotency.** At most ``MCP_MAX_TOOL_CALLS`` calls per turn, and
  every execution is audited (tool, hashed args, duration, outcome) so a
  side-effecting integration is always traceable.
* **Degradation, not crash.** A server that is down, a transport error or an
  ``isError`` result all come back as ``ok=False`` rows in the report; the turn
  still completes and can explain what failed.
* **Prompts are a separate, explicit surface.** A prompt is an *instruction
  injected into the turn*, not a side-effecting call, so it is not routed the
  way tools are: there is no keyword router, no inference, and no riding along
  on ``MCP_ENABLED``. The model lists the catalogue and asks for one **by
  name**, and the same three guards (advertised, allowlisted, within budget)
  plus the same taxonomy gate apply. The reasoning is in
  :func:`intent_matches_prompt`.
"""
from __future__ import annotations

import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..config import settings
from ..mcp.client import AuditSink, MCPClient, ToolInfo
from ..mcp.transport import make_transport

__all__ = [
    "MCP_ELIGIBLE_INTENTS",
    "PROMPT_SERVER_NAME",
    "MCPDispatchService",
    "PromptFetchReport",
    "ToolCall",
    "ToolCallReport",
    "intent_matches_prompt",
    "intent_matches_tool",
    "plan_offline",
]

# Deterministic offline routing: (patterns, server, tool).
#
# Patterns are ASCII regexes matched against the *diacritic-folded* question, so
# "cảm xúc" and "cam xuc" both hit. They are deliberately **specific**, because a
# loose matcher is how an HR question gets answered by a marketing tool:
#
#   * bare "email" is NOT a match — "Liên hệ [PII-EMAIL] máy lẻ 202" is a contact
#     lookup, not a request to send a report;
#   * bare "chi phi" is NOT a match — "bảo hiểm chi trả 100% chi phí khám" is a
#     benefits policy, not a CPI question;
#   * bare "noi dung" / "lich" are NOT matches — they appear in ordinary HR text.
#
# Short tokens use ``\b`` word boundaries so "teams" cannot match inside another
# word, while phrases are matched as phrases.
_OFFLINE_ROUTES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (
        (r"\breviews?\b", r"\bsentiment\b", r"cam xuc",
         r"phan hoi nguoi choi", r"\bgame reviews?\b"),
        "market_insight", "review_sentiment_summary",
    ),
    (
        (r"\bcpi\b", r"\broas\b", r"\bctr\b", r"\bkpi\b", r"\barpdau\b",
         r"chi phi (cai dat|install)", r"hieu qua quang cao",
         r"doanh thu quang cao", r"chi so (game|quang cao)"),
        "sql_analytics", "game_kpi_summary",
    ),
    (
        (r"gui (email|mail|bao cao)", r"email (bao cao|report)",
         r"send (an? )?(email|report)"),
        "notification", "send_email_report",
    ),
    (
        (r"\bzalo\b", r"\bteams\b", r"\balert\b",
         r"canh bao.*(retention|kpi|doanh thu|chi so)"),
        "notification", "send_teams_card",
    ),
    (
        (r"content calendar", r"lich noi dung",
         r"noi dung (chien dich|marketing|quang cao|facebook|tiktok)",
         r"(tao|len) (task|the|cong viec).*(marketing|noi dung)"),
        "airtable", "create_marketing_task",
    ),
)

# Intent taxonomy gate: the HR/IT policy intents are answered from the document
# corpus and must NEVER be rerouted to an integration tool. Only ``general`` (the
# catch-all for questions that matched no enterprise pattern) is eligible for
# MCP routing — an unknown question still needs an explicit tool keyword.
MCP_ELIGIBLE_INTENTS: frozenset[str] = frozenset({"general"})

# The MCP server that carries the PromptOps library. Named here so the policy
# layer can talk about "the prompts server" without importing the server module
# (which would pull the prompt loader into every import of this file).
PROMPT_SERVER_NAME = "prompts"


def _split_list(raw: str | None) -> list[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def _fold(text: str) -> str:
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", text or "").lower()
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _match_route(folded: str, *, tool: str | None = None) -> tuple[str, str, str] | None:
    """Return ``(server, tool, matched_pattern)`` for the first matching route."""
    for patterns, server, routed_tool in _OFFLINE_ROUTES:
        if tool is not None and routed_tool != tool:
            continue
        for pattern in patterns:
            if re.search(pattern, folded):
                return server, routed_tool, pattern
    return None


@dataclass
class ToolCall:
    """One planned tool invocation."""

    server: str
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    @property
    def qualified_name(self) -> str:
        return f"{self.server}.{self.tool}"


@dataclass
class ToolCallReport:
    """Result of executing a plan: per-call rows plus a run verdict."""

    results: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    calls_planned: int = 0
    budget: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(r.get("ok") for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "calls_planned": self.calls_planned,
            "calls_executed": len(self.results),
            "budget": self.budget,
            "results": self.results,
            "skipped": self.skipped,
        }


def intent_matches_tool(
    question: str, *, tool: str, intent: str | None = None
) -> bool:
    """Whether ``tool`` is the offline route for ``question``.

    ``intent`` (when given) is enforced against
    :data:`MCP_ELIGIBLE_INTENTS` first: an HR/IT policy question must never be
    rerouted to an integration tool, even if the same words appear in it.
    """
    if intent is not None and intent not in MCP_ELIGIBLE_INTENTS:
        return False
    return _match_route(_fold(question), tool=tool) is not None


@dataclass
class PromptFetchReport:
    """Outcome of fetching prompts for one turn.

    Separate from :class:`ToolCallReport` on purpose: a tool call can change the
    world (an email, an Airtable row) while a prompt fetch cannot, so mixing
    them under one ``ok`` would let a failed integration hide behind a
    successful read, and vice versa.
    """

    fetched: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    available: list[str] = field(default_factory=list)
    budget: int = 0
    enabled: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.fetched) and all(r.get("ok") for r in self.fetched)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "enabled": self.enabled,
            "fetches_planned": len(self.fetched) + len(self.skipped),
            "fetches_executed": len(self.fetched),
            "budget": self.budget,
            "available": self.available,
            "fetched": self.fetched,
            "skipped": self.skipped,
        }


def intent_matches_prompt(question: str, *, prompt: str, intent: str | None = None) -> bool:
    """Whether ``prompt`` is the prompt that matches ``question``.

    Mirrors :func:`intent_matches_tool`'s taxonomy gate so both surfaces enforce
    the same rule: an HR/IT policy question is never answered by, or
    re-instructed with, a marketing/analytics artefact — even when the same
    words appear in it.

    Unlike the tool router this does **not** look at ``question`` beyond that
    gate. Prompts are never auto-matched on keywords: a prompt is an instruction
    the model asked for by name, and inferring one from question words would
    silently inject a marketing instruction into an unrelated turn.
    """
    if intent is not None and intent not in MCP_ELIGIBLE_INTENTS:
        return False
    return bool(prompt)


def plan_offline(
    question: str,
    *,
    arguments: dict[str, Any] | None = None,
    enabled_tools: Iterable[str] | None = None,
    intent: str | None = None,
) -> list[ToolCall]:
    """Deterministically map a question to at most one tool call.

    ``intent`` enforces the taxonomy gate (:data:`MCP_ELIGIBLE_INTENTS`):
    HR/IT policy questions are answered from the corpus, never by an integration.
    ``enabled_tools`` is a set of qualified names (``server.tool``); an unavailable
    target yields no plan rather than an error, because "that tool is not
    connected" is a normal state, not a bug.

    The returned plan records **which pattern matched**, so an audit can explain the
    routing decision instead of trusting it.
    """
    if intent is not None and intent not in MCP_ELIGIBLE_INTENTS:
        return []
    match = _match_route(_fold(question))
    if match is None:
        return []
    server, tool, pattern = match
    if enabled_tools is not None and f"{server}.{tool}" not in set(enabled_tools):
        return []
    return [
        ToolCall(
            server=server,
            tool=tool,
            arguments=dict(arguments or {}),
            reason=f"matched offline route {server}.{tool} via /{pattern}/",
        )
    ]


class MCPDispatchService:
    """Owns MCP clients for a set of servers and executes plans under policy."""

    def __init__(
        self,
        *,
        server_names: Sequence[str] | None = None,
        transport_mode: str | None = None,
        allowlist: Sequence[str] | None = None,
        max_calls: int | None = None,
        timeout: float | None = None,
        audit_path: str | None = None,
        client_factory: Any = None,
        prompts_enabled: bool | None = None,
        prompt_server: str | None = None,
        prompt_allowlist: Sequence[str] | None = None,
        max_prompt_fetches: int | None = None,
    ) -> None:
        self.server_names = [
            s.strip()
            for s in (server_names or settings.MCP_SERVERS.split(","))
            if s.strip()
        ]
        self.transport_mode = transport_mode or settings.MCP_BRIDGE_TRANSPORT
        configured = allowlist if allowlist is not None else [
            a.strip() for a in settings.MCP_TOOL_ALLOWLIST.split(",") if a.strip()
        ]
        self.allowlist = set(configured)
        self.max_calls = max_calls if max_calls is not None else settings.MCP_MAX_TOOL_CALLS
        self.timeout = timeout if timeout is not None else settings.MCP_TOOL_TIMEOUT_SEC
        self.audit = AuditSink(audit_path or settings.MCP_AUDIT_LOG_PATH)
        self._client_factory = client_factory
        self._clients: dict[str, MCPClient] = {}
        self._connect_errors: dict[str, str] = {}
        # Prompt surface. Opt-in separately from MCP_ENABLED: a prompt is an
        # instruction injected into the turn, not a side-effecting call, so it
        # does not ride along on the tool switch.
        self.prompts_enabled = (
            prompts_enabled if prompts_enabled is not None
            else settings.MCP_PROMPTS_ENABLED
        )
        self.prompt_server = prompt_server or PROMPT_SERVER_NAME
        self.prompt_allowlist = list(
            prompt_allowlist
            if prompt_allowlist is not None
            else _split_list(settings.MCP_PROMPT_ALLOWLIST)
        )
        self.max_prompt_fetches = max(
            0,
            max_prompt_fetches
            if max_prompt_fetches is not None
            else settings.MCP_MAX_PROMPT_FETCHES,
        )
        # The prompt server is not an integration, so it is not in MCP_SERVERS by
        # default. Add it only when the feature is on — otherwise the service
        # would advertise a prompt surface it can never reach.
        if self.prompts_enabled and self.prompt_server not in self.server_names:
            self.server_names.append(self.prompt_server)

    # ---- connection ------------------------------------------------------
    def connect(self) -> dict[str, str]:
        """Connect every enabled server; failures are recorded, not raised."""
        self._connect_errors.clear()
        for name in self.server_names:
            if name in self._clients:
                continue
            try:
                client = (
                    self._client_factory(name)
                    if self._client_factory
                    else MCPClient(
                        name,
                        make_transport(name, mode=self.transport_mode,
                                       timeout=self.timeout),
                        timeout=self.timeout,
                        audit=self.audit,
                    )
                )
                client.connect()
                self._clients[name] = client
            except Exception as exc:  # an integration outage must not break the turn
                self._connect_errors[name] = f"{type(exc).__name__}: {exc}"
        return dict(self._connect_errors)

    def close(self) -> None:
        for client in self._clients.values():
            try:
                client.close()
            except Exception:
                pass
        self._clients.clear()

    # ---- discovery -------------------------------------------------------
    @property
    def clients(self) -> dict[str, MCPClient]:
        return dict(self._clients)

    def available_tools(self) -> list[ToolInfo]:
        """Advertised tools, filtered by the allowlist when one is configured."""
        tools: list[ToolInfo] = []
        for client in self._clients.values():
            for tool in client.tools:
                if self.allowlist and tool.qualified_name not in self.allowlist:
                    continue
                tools.append(tool)
        return sorted(tools, key=lambda t: t.qualified_name)

    def tool_schemas(self) -> list[dict[str, Any]]:
        """OpenAI-style function schemas for every available tool."""
        return [t.to_openai_tool() for t in self.available_tools()]

    def is_allowed(self, server: str, tool: str) -> bool:
        """Connected AND advertised AND (no allowlist OR listed).

        Written as one boolean expression on purpose: this is the function a
        security reviewer reads, and splitting it into three early returns is how
        a future edit drops one of the three conditions.
        """
        if server not in self._clients or self._clients[server].tool(tool) is None:
            return False
        return not self.allowlist or f"{server}.{tool}" in self.allowlist

    # ---- prompts ---------------------------------------------------------
    def available_prompts(self) -> list[dict[str, Any]]:
        """Prompts the model may fetch this turn, with their argument specs.

        Empty when the feature is off, the server is not connected, or the
        server advertised no prompts — a caller can then simply say "no prompts
        available" instead of branching on three different failure causes.
        """
        if not self.prompts_enabled:
            return []
        client = self._clients.get(self.prompt_server)
        if client is None:
            return []
        allowed = self.prompt_allowlist
        out: list[dict[str, Any]] = []
        for prompt in client.list_prompts():
            name = str(prompt.get("name") or "")
            if not name:
                continue
            if allowed and f"{self.prompt_server}.{name}" not in allowed:
                continue
            out.append(prompt)
        return out

    def is_prompt_allowed(self, name: str) -> bool:
        """Advertised AND (no prompt allowlist OR listed).

        Same three-part reasoning as :meth:`is_allowed`, kept as one expression
        for the same reason.
        """
        client = self._clients.get(self.prompt_server)
        if client is None:
            return False
        if not any(str(p.get("name") or "") == name for p in client.list_prompts()):
            return False
        if not self.prompt_allowlist:
            return True
        return f"{self.prompt_server}.{name}" in self.prompt_allowlist

    def fetch_prompts(
        self,
        names: Sequence[str],
        *,
        arguments: dict[str, dict[str, Any]] | None = None,
        intent: str | None = None,
    ) -> PromptFetchReport:
        """Fetch named prompts under the budget + allowlist, auditing each one.

        ``arguments`` is keyed by prompt name. A name that fails the taxonomy
        gate, the allowlist, or the budget is recorded in ``skipped`` with a
        reason rather than raising — a turn must still complete when the model
        asks for something it may not have.
        """
        report = PromptFetchReport(
            budget=self.max_prompt_fetches, enabled=self.prompts_enabled
        )
        if not self.prompts_enabled:
            return report
        if intent is not None and intent not in MCP_ELIGIBLE_INTENTS:
            for name in names:
                report.skipped.append(
                    {"prompt": name, "reason": f"intent_not_eligible:{intent}"}
                )
            return report
        report.available = [str(p.get("name")) for p in self.available_prompts()]
        per_prompt = arguments or {}
        for name in names:
            if len(report.fetched) >= self.max_prompt_fetches:
                report.skipped.append({"prompt": name, "reason": "budget_exhausted"})
                continue
            if not self.is_prompt_allowed(name):
                report.skipped.append(
                    {
                        "prompt": name,
                        "reason": self._connect_errors.get(
                            self.prompt_server, "prompt_not_available_or_not_allowed"
                        ),
                    }
                )
                continue
            client = self._clients[self.prompt_server]
            started = time.perf_counter()
            row: dict[str, Any] = {"prompt": name}
            try:
                row["result"] = client.get_prompt(name, per_prompt.get(name) or {})
                row["ok"] = True
            except Exception as exc:
                # A prompt that cannot render (missing variable, server gone)
                # is a soft failure: the turn continues without it.
                row["ok"] = False
                row["reason"] = f"{type(exc).__name__}: {exc}"
            row["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
            # Audited like a tool call: which prompt, which arguments, outcome.
            # Argument *values* are not logged — they are user data.
            self.audit.write(
                {
                    "event": "prompt_fetch",
                    "server": self.prompt_server,
                    "prompt": name,
                    "argument_names": sorted(per_prompt.get(name) or {}),
                    "ok": row["ok"],
                    "duration_ms": row["duration_ms"],
                }
            )
            report.fetched.append(row)
        return report

    # ---- execution -------------------------------------------------------
    def execute(self, plan: Sequence[ToolCall]) -> ToolCallReport:
        """Execute ``plan`` under the budget + allowlist, auditing every call."""
        report = ToolCallReport(calls_planned=len(plan), budget=self.max_calls)
        for call in plan:
            if len(report.results) >= self.max_calls:
                report.skipped.append(
                    {"tool": call.qualified_name, "reason": "budget_exhausted"}
                )
                continue
            if not self.is_allowed(call.server, call.tool):
                report.skipped.append(
                    {
                        "tool": call.qualified_name,
                        "reason": self._connect_errors.get(
                            call.server, "tool_not_available_or_not_allowed"
                        ),
                    }
                )
                continue
            result = self._clients[call.server].call_tool(call.tool, call.arguments)
            result["reason"] = call.reason
            report.results.append(result)
        return report

    def run(self, question: str, *, arguments: dict[str, Any] | None = None,
            intent: str | None = None,
            prompt_names: Sequence[str] | None = None,
            prompt_arguments: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        """Connect, plan offline, execute, and return a merged report.

        ``prompt_names`` are fetched only when the caller names them. There is
        no prompt inference here — see :func:`intent_matches_prompt`.
        """
        connect_errors = self.connect()
        plan = plan_offline(
            question,
            arguments=arguments,
            enabled_tools=[t.qualified_name for t in self.available_tools()],
            intent=intent,
        )
        payload = self.execute(plan).to_dict()
        payload["question"] = question
        payload["servers"] = sorted(self._clients)
        payload["connect_errors"] = connect_errors
        payload["tools_available"] = [t.qualified_name for t in self.available_tools()]
        # Always report the catalogue (even on a turn that fetches nothing) so
        # a caller can tell "no prompt matches" from "prompts are switched off".
        payload["prompts_available"] = [
            str(p.get("name")) for p in self.available_prompts()
        ]
        payload["prompts_enabled"] = self.prompts_enabled
        if prompt_names:
            payload["prompts"] = self.fetch_prompts(
                prompt_names, arguments=prompt_arguments, intent=intent
            ).to_dict()
        return payload


def node_mcp_dispatch(state: Any) -> Any:
    """LangGraph node: run the offline MCP route for this question.

    Wired into the graph only when ``MCP_ENABLED`` is true (see
    ``langgraph_agent.build_graph``), so the default graph — and therefore every
    existing test — stays exactly as it was.
    """
    state.trace("mcp_dispatch")
    service = MCPDispatchService()
    try:
        payload = service.run(state.question, arguments=dict(state.slots or {}),
                              intent=state.intent)
    finally:
        service.close()
    executed = [r for r in payload.get("results", []) if r.get("ok")]
    state.plan = {**state.plan, "mcp": payload}
    state.action_result = payload
    state.flags.append("mcp_tools_enabled")
    if executed:
        state.answer = (
            "Đã gọi " + ", ".join(sorted(r["tool"] for r in executed))
            + " và nhận kết quả."
        )
    else:
        state.answer = (
            "Tôi chưa gọi được công cụ dữ liệu nào cho câu hỏi này. "
            "Bạn có thể xem báo cáo KPI hoặc review insight trên dashboard."
        )
    state.status = "answered"
    return state


