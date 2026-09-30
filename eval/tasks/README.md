# Agent Task Benchmark (Phase A)

Task-level evaluation proving MAIA can choose the correct tool, require
approval for risky actions, block forbidden actions, prevent cross-tenant
access, and recover safely from tool failure.

- Fixtures: `TASK-001.json` … `TASK-005.json` (5 tasks, machine-readable).
- Policy: `src/maia/agent/action_policy.py` — READ_ONLY / LOW_RISK /
  HIGH_RISK / FORBIDDEN mapping over **real** `TOOL_REGISTRY` names only.
  `disable_user` does not exist in MAIA, so TASK-002 uses the real
  HIGH_RISK representative `create_it_ticket` (C1 confirm-before-action).
- Harness: `tests/test_agent_task_benchmark.py` — runs every task through
  the real production path (`IntentRouter`, `EnterpriseAgent.chat`,
  `confirm_action`, tenant checks). No benchmark-only agent stub.
- Evidence: `docs/evidence/agent-task-benchmark/<run_id>.json`.

Approved wording: "Evaluated agent tool selection, approval enforcement,
tenant isolation, and failure handling using a deterministic task-level
benchmark." Do NOT claim "safe autonomous agent".
