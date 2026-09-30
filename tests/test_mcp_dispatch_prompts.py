"""Agent-side prompt dispatch: opt-in, allowlist, budget, taxonomy gate.

The prompt surface is a policy boundary, not a convenience. A prompt is an
instruction injected into the model's turn, so these tests pin the things that
would make it dangerous:

* it is **off by default**, and turning MCP tools on does not turn it on;
* the model must **name** a prompt — nothing is inferred from question words;
* the same three guards as a tool call apply (advertised, allowlisted, budgeted);
* the same taxonomy gate applies, so an HR/IT question never picks up a
  marketing instruction;
* a prompt that cannot render is a soft failure, not a broken turn.
"""
from __future__ import annotations

from maia.agent.mcp_dispatch import (
    MCP_ELIGIBLE_INTENTS,
    MCPDispatchService,
    intent_matches_prompt,
)

NL_TO_SQL_ARGS = {
    "question": "CPI bao nhiêu?",
    "schema_block": "TABLE marketing_metrics(cpi REAL)",
    "dialect": "sqlite",
    "max_rows": "200",
}


def _service(*, hermetic: bool = True, **kwargs) -> MCPDispatchService:
    """Service over the real servers, with a sane starting configuration."""
    kwargs.setdefault("server_names", ["airtable"])
    kwargs.setdefault("audit_path", None)
    service = MCPDispatchService(**kwargs)
    if hermetic and service.audit.path is None:
        service.audit = type(service.audit)(None)  # keep assertions hermetic
    return service


# --- opt-in -------------------------------------------------------------- #

def test_prompts_are_off_by_default():
    assert _service().prompts_enabled is False


def test_mcp_tools_on_does_not_turn_prompts_on():
    """Tool calling being on is not consent to inject instructions."""
    service = _service()
    assert service.prompts_enabled is False
    assert "prompts" not in service.server_names


def test_prompts_available_is_empty_when_off():
    service = _service()
    service.connect()
    try:
        assert service.available_prompts() == []
    finally:
        service.close()


def test_fetch_is_a_noop_when_off():
    service = _service()
    report = service.fetch_prompts(["nl_to_sql"], arguments={"nl_to_sql": NL_TO_SQL_ARGS})
    assert report.enabled is False
    assert report.fetched == []
    assert report.ok is False


def test_enabling_prompts_connects_the_prompt_server():
    service = _service(prompts_enabled=True)
    assert service.prompt_server in service.server_names


def test_run_reports_the_catalogue_even_without_a_fetch():
    service = _service(prompts_enabled=True)
    service.connect()
    try:
        payload = service.run("xin chào")
        assert payload["prompts_enabled"] is True
        assert "nl_to_sql" in payload["prompts_available"]
        # No prompt key at all: nothing was asked for.
        assert "prompts" not in payload
    finally:
        service.close()


# --- no inference -------------------------------------------------------- #

def test_a_question_never_selects_a_prompt_by_itself():
    """The model must name the prompt; question words must not pick one.

    This is the whole point of the explicit surface. If this ever starts
    returning True for a marketing-sounding question, an HR turn can end up
    executing a marketing instruction.
    """
    assert intent_matches_prompt("Viết campaign mùa hè", prompt="") is False
    assert intent_matches_prompt("Chính sách nghỉ phép?", prompt="") is False


def test_named_prompt_passes_for_an_eligible_intent():
    assert intent_matches_prompt("anything", prompt="nl_to_sql", intent="general") is True


def test_prompts_follow_the_same_taxonomy_gate_as_tools():
    for intent in sorted(MCP_ELIGIBLE_INTENTS):
        assert intent_matches_prompt("q", prompt="nl_to_sql", intent=intent) is True
    for intent in ("hr_policy", "it_help", "leave_request", "benefits", "vpn"):
        assert intent_matches_prompt("q", prompt="nl_to_sql", intent=intent) is False


def test_hr_question_cannot_fetch_a_prompt_even_by_name():
    """The taxonomy gate blocks a prompt even when the name is spelled out.

    Verified by disabling the gate: without it this assertion fails, because the
    prompt renders and ``fetched`` is non-empty.
    """
    service = _service(prompts_enabled=True)
    service.connect()
    try:
        report = service.fetch_prompts(
            ["nl_to_sql"], arguments={"nl_to_sql": NL_TO_SQL_ARGS}, intent="hr_policy"
        )
        assert report.fetched == []
        assert report.skipped[0]["reason"] == "intent_not_eligible:hr_policy"
    finally:
        service.close()


# --- catalogue and allowlist -------------------------------------------- #

def test_available_prompts_lists_the_library():
    service = _service(prompts_enabled=True)
    service.connect()
    try:
        names = {p["name"] for p in service.available_prompts()}
        assert "nl_to_sql" in names
        # Argument specs travel with the catalogue so the model knows what to fill.
        nl2sql = next(p for p in service.available_prompts() if p["name"] == "nl_to_sql")
        assert {a["name"] for a in nl2sql["arguments"]} >= {"question", "schema_block"}
    finally:
        service.close()


def test_prompt_allowlist_narrows_the_catalogue():
    service = _service(prompts_enabled=True, prompt_allowlist=["prompts.nl_to_sql"])
    service.connect()
    try:
        assert [p["name"] for p in service.available_prompts()] == ["nl_to_sql"]
        assert service.is_prompt_allowed("nl_to_sql") is True
        assert service.is_prompt_allowed("campaign_copywriter") is False
    finally:
        service.close()


def test_fetching_an_unlisted_prompt_is_skipped_not_raised():
    service = _service(prompts_enabled=True, prompt_allowlist=["prompts.nl_to_sql"])
    service.connect()
    try:
        report = service.fetch_prompts(
            ["campaign_copywriter"],
            arguments={"campaign_copywriter": {
                "game_name": "G", "update_name": "U", "key_features": "F",
                "channels": "facebook", "tone": "v", "audience": "a",
                "legal_notes": "n", "language": "vi",
            }},
        )
        assert report.fetched == []
        assert report.skipped[0]["reason"] == "prompt_not_available_or_not_allowed"
    finally:
        service.close()


def test_unknown_prompt_name_is_skipped():
    service = _service(prompts_enabled=True)
    service.connect()
    try:
        report = service.fetch_prompts(["no_such_prompt"])
        assert report.fetched == []
        assert report.skipped[0]["prompt"] == "no_such_prompt"
    finally:
        service.close()


def test_is_prompt_allowed_is_false_without_a_connection():
    service = _service(prompts_enabled=True)
    assert service.is_prompt_allowed("nl_to_sql") is False


# --- budget -------------------------------------------------------------- #

def test_budget_caps_fetches_per_turn():
    service = _service(prompts_enabled=True, max_prompt_fetches=1)
    service.connect()
    try:
        report = service.fetch_prompts(["nl_to_sql", "game_review_insight"])
        assert len(report.fetched) == 1
        assert report.skipped[0]["reason"] == "budget_exhausted"
        assert report.budget == 1
    finally:
        service.close()


def test_zero_budget_fetches_nothing():
    service = _service(prompts_enabled=True, max_prompt_fetches=0)
    service.connect()
    try:
        assert service.fetch_prompts(["nl_to_sql"]).fetched == []
    finally:
        service.close()


# --- execution and degradation ------------------------------------------ #

def test_fetch_returns_rendered_messages():
    service = _service(prompts_enabled=True)
    service.connect()
    try:
        report = service.fetch_prompts(["nl_to_sql"], arguments={"nl_to_sql": NL_TO_SQL_ARGS})
        assert report.ok is True
        row = report.fetched[0]
        assert row["prompt"] == "nl_to_sql"
        messages = row["result"]["messages"]
        assert next(m["role"] for m in messages) == "system"
        assert "CPI bao nhiêu?" in messages[-1]["content"]["text"]
    finally:
        service.close()


def test_missing_argument_is_a_soft_failure_not_a_crash():
    service = _service(prompts_enabled=True)
    service.connect()
    try:
        report = service.fetch_prompts(["nl_to_sql"], arguments={"nl_to_sql": {"question": "q"}})
        # The call happened and failed; the turn is not broken.
        assert len(report.fetched) == 1
        assert report.fetched[0]["ok"] is False
        assert report.ok is False
        assert "dialect" in report.fetched[0]["reason"]
    finally:
        service.close()


def test_report_shape_separates_prompt_from_tool_outcomes():
    service = _service(prompts_enabled=True)
    service.connect()
    try:
        payload = service.run(
            "xin chào",
            prompt_names=["nl_to_sql"],
            prompt_arguments={"nl_to_sql": NL_TO_SQL_ARGS},
        )
        assert payload["ok"] is False          # no tool ran
        assert payload["prompts"]["ok"] is True  # but the prompt did
    finally:
        service.close()


def test_audit_records_the_fetch_without_argument_values(tmp_path):
    """The audit must prove which prompt ran without copying user data into a log."""
    audit_file = tmp_path / "audit.jsonl"
    service = _service(hermetic=False, prompts_enabled=True, audit_path=str(audit_file))
    service.connect()
    try:
        service.fetch_prompts(["nl_to_sql"], arguments={"nl_to_sql": NL_TO_SQL_ARGS})
    finally:
        service.close()
    text = audit_file.read_text(encoding="utf-8")
    assert "prompt_fetch" in text
    assert "nl_to_sql" in text
    assert "argument_names" in text
    # The question text is user data and must not be written to the audit log.
    assert "CPI bao nhiêu?" not in text

    try:
        report = service.fetch_prompts(
            ["nl_to_sql"], arguments={"nl_to_sql": NL_TO_SQL_ARGS}, intent="hr_policy"
        )
        assert report.fetched == []
        assert report.skipped[0]["reason"] == "intent_not_eligible:hr_policy"
    finally:
        service.close()
