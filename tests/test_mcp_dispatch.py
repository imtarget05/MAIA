"""Agent-side MCP tool-calling tests: planning, allowlist, budget, degradation.

These tests pin the *policy* layer, which is where a real incident would come
from: an allowlist that leaks, a budget that does not stop a loop, a transport
that raises, or a question that routes to a tool nobody asked for.
"""
from __future__ import annotations

import pytest

from maia.agent.mcp_dispatch import (
    MCPDispatchService,
    ToolCall,
    intent_matches_tool,
    plan_offline,
)
from maia.mcp.client import MCPClient
from maia.mcp.servers import build_server
from maia.mcp.transport import InProcessTransport


def _service(**kwargs) -> MCPDispatchService:
    """In-process service over real servers, with an isolated audit sink."""
    kwargs.setdefault("audit_path", None)
    service = MCPDispatchService(server_names=["airtable", "notification"], **kwargs)
    service.audit = type(service.audit)(None)  # keep assertions hermetic
    return service


# --------------------------------------------------------------------------- #
# Offline planning
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "question,server,tool",
    [
        ("Tổng hợp cảm xúc review của game", "market_insight",
         "review_sentiment_summary"),
        ("CPI thay đổi thế nào", "sql_analytics", "game_kpi_summary"),
        ("Gửi báo cáo cho lead game", "notification", "send_email_report"),
        ("Cảnh báo retention qua teams", "notification", "send_teams_card"),
        ("Lên lịch nội dung mùa hè", "airtable", "create_marketing_task"),
    ],
)
def test_offline_router_picks_the_expected_tool(question, server, tool):
    plan = plan_offline(question)
    assert len(plan) == 1
    assert (plan[0].server, plan[0].tool) == (server, tool)
    assert plan[0].reason  # the matched pattern is recorded for audit


@pytest.mark.parametrize(
    "question",
    [
        # HR/IT policy text that a loose substring matcher used to hijack:
        "Chính sách bảo hiểm y tế doanh nghiệp chi trả 100% chi phí khám.",
        "Liên hệ [PII-EMAIL] máy lẻ 202.",
        "Nhân viên B email nvB@example.com SĐT 0912345678.",
        "Chính sách nghỉ phép và lịch làm việc linh hoạt là gì?",
        "Hỗ trợ VPN: [PII-EMAIL]",
        "xin chào, bạn khoẻ không?",
    ],
)
def test_offline_router_does_not_hijack_policy_questions(question):
    assert plan_offline(question) == []


def test_intent_gate_blocks_every_enterprise_intent():
    """Even a *real* tool keyword must not bypass the taxonomy gate."""
    question = "Tạo báo cáo KPI cho phòng của tôi"
    assert plan_offline(question)  # general question → routed
    for intent in ("benefits", "expense", "hr_policy", "leave_request",
                   "leave_balance", "it_help", "vpn", "onboarding", "security"):
        assert plan_offline(question, intent=intent) == [], intent
    assert intent_matches_tool(question, tool="game_kpi_summary", intent="benefits") is False


def test_offline_router_respects_the_available_tool_set():
    plan = plan_offline("Tổng hợp cảm xúc review",
                        enabled_tools=["sql_analytics.game_kpi_summary"])
    assert plan == []  # the matching tool is not connected


def test_intent_matches_tool_is_a_cheap_prefilter():
    assert intent_matches_tool("Cảm xúc review như thế nào",
                               tool="review_sentiment_summary") is True
    assert intent_matches_tool("Chào bạn", tool="review_sentiment_summary") is False


# --------------------------------------------------------------------------- #
# Connection handling
# --------------------------------------------------------------------------- #
def test_service_connects_and_lists_tool_schemas():
    service = _service()
    try:
        assert service.connect() == {}
        assert sorted(service.clients) == ["airtable", "notification"]
        names = [t.qualified_name for t in service.available_tools()]
        assert "airtable.create_marketing_task" in names
        assert all(s["type"] == "function" for s in service.tool_schemas())
    finally:
        service.close()


def test_a_broken_server_is_recorded_and_does_not_stop_the_others():
    def _factory(name: str) -> MCPClient:
        if name == "airtable":
            raise RuntimeError("port in use")
        return MCPClient(name, InProcessTransport(build_server(name)))

    service = MCPDispatchService(server_names=["airtable", "notification"],
                                client_factory=_factory)
    errors = service.connect()
    assert "airtable" in errors and "port in use" in errors["airtable"]
    assert "notification" in service.clients
    service.close()


def test_allowlist_filters_the_tool_surface():
    service = _service(allowlist=["airtable.list_content_calendar"])
    try:
        service.connect()
        assert [t.qualified_name for t in service.available_tools()] == [
            "airtable.list_content_calendar"
        ]
        assert service.is_allowed("airtable", "list_content_calendar") is True
        assert service.is_allowed("airtable", "create_marketing_task") is False
    finally:
        service.close()


# --------------------------------------------------------------------------- #
# Execution policy
# --------------------------------------------------------------------------- #
def test_budget_caps_the_number_of_tool_calls():
    service = _service(max_calls=2)
    try:
        service.connect()
        plan = [ToolCall(server="airtable", tool="list_content_calendar", arguments={})
                for _ in range(5)]
        report = service.execute(plan)
        assert len(report.results) == 2
        assert len(report.skipped) == 3
        assert {s["reason"] for s in report.skipped} == {"budget_exhausted"}
    finally:
        service.close()


def test_calls_to_unavailable_tools_are_skipped_not_executed():
    service = _service()
    try:
        service.connect()
        report = service.execute([ToolCall(server="sql_analytics",
                                           tool="game_kpi_summary", arguments={})])
        assert report.results == []
        assert report.skipped[0]["reason"] == "tool_not_available_or_not_allowed"
    finally:
        service.close()


def test_run_reports_tools_servers_and_connect_errors():
    service = _service()
    try:
        payload = service.run("Gửi báo cáo retention cho lead",
                              arguments={"recipient": "lead@game.example",
                                         "subject": "Retention", "body": "Drop 13%"})
        assert payload["servers"] == ["airtable", "notification"]
        assert payload["connect_errors"] == {}
        assert "notification.send_email_report" in payload["tools_available"]
        assert payload["calls_planned"] == 1
        # The dry-run dispatcher reports the tool as executed but not delivered.
        executed = payload["results"][0]
        assert executed["structured"]["delivered"] is False
        assert executed["reason"]
    finally:
        service.close()


def test_every_execution_is_audited():
    service = _service()
    try:
        service.run("Lên lịch nội dung", arguments={
            "campaign_id": "mua-4", "channel": "facebook",
            "content": "Ban do moi da mo, moi chi huy tham gia."})
        events = [r["event"] for r in service.audit.records]
        assert events.count("connect") == 2
        assert "tool_call" in events
    finally:
        service.close()


def test_unknown_question_yields_an_empty_plan_not_an_error():
    service = _service()
    try:
        payload = service.run("xin chào bạn khoẻ không")
        assert payload["calls_planned"] == 0
        assert payload["results"] == []
        assert payload["ok"] is False
    finally:
        service.close()


# --------------------------------------------------------------------------- #
# LangGraph integration (opt-in)
# --------------------------------------------------------------------------- #
def test_graph_never_routes_to_mcp_while_disabled(monkeypatch):
    """With MCP off (the default) the graph must behave exactly as before."""
    monkeypatch.setattr("maia.config.settings.MCP_ENABLED", False, raising=False)
    from maia.agent.langgraph_agent import AgentState, route_after_classify

    state = AgentState(question="Cho tôi CPI theo kênh", intent="general")
    assert route_after_classify(state) == "retrieve"


def test_graph_routes_data_questions_to_mcp_when_enabled(monkeypatch):
    monkeypatch.setattr("maia.config.settings.MCP_ENABLED", True, raising=False)
    from maia.agent.langgraph_agent import AgentState, route_after_classify

    state = AgentState(question="Cho tôi CPI theo kênh", intent="general")
    assert route_after_classify(state) == "mcp_dispatch"


@pytest.mark.parametrize(
    "question,intent",
    [
        # A benefits policy sentence that contains a metric-ish phrase.
        ("Chính sách bảo hiểm y tế doanh nghiệp chi trả 100% chi phí khám.", "benefits"),
        # A contact-lookup answer containing the literal word "EMAIL".
        ("Liên hệ [PII-EMAIL] máy lẻ 202.", "general"),
        # Leave questions are full of "lịch" (schedule) — must stay on retrieval.
        ("Tôi muốn xin nghỉ phép, lịch làm việc tuần sau thế nào?", "leave_request"),
        # Security incident with an alert-sounding word.
        ("Có cảnh báo bảo mật nào mới không?", "security"),
    ],
)
def test_graph_never_reroutes_policy_questions_to_a_tool(
    question, intent, monkeypatch
):
    """The default-on MCP branch must not hijack the enterprise (HR/IT) path."""
    monkeypatch.setattr("maia.config.settings.MCP_ENABLED", True, raising=False)
    from maia.agent.langgraph_agent import AgentState, route_after_classify

    state = AgentState(question=question, intent=intent)
    assert route_after_classify(state) == "retrieve"


def test_mcp_node_answers_without_raising(monkeypatch, tmp_path):
    from maia.agent.langgraph_agent import AgentState
    from maia.agent.mcp_dispatch import node_mcp_dispatch

    monkeypatch.setattr("maia.config.settings.MCP_SERVERS", "airtable", raising=False)
    # Keep the test out of the repo's storage directory.
    monkeypatch.setattr("maia.config.settings.MCP_AUDIT_LOG_PATH",
                        str(tmp_path / "audit.jsonl"), raising=False)
    state = node_mcp_dispatch(AgentState(question="Cảm xúc review game", slots={}))
    assert state.status == "answered"
    assert state.answer
    assert "mcp_tools_enabled" in state.flags
    assert "mcp" in state.plan


def test_mcp_node_reports_when_no_tool_matched():
    from maia.agent.langgraph_agent import AgentState
    from maia.agent.mcp_dispatch import node_mcp_dispatch

    state = node_mcp_dispatch(AgentState(question="xin chào", slots={}))
    assert state.status == "answered"
    assert "chưa gọi được" in state.answer

