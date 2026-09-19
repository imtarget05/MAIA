# Service Desk — session state (handoff)

Plan: `plans/plan-20260919-1820-maia-service-desk-v1.md`
Branch: `codex/maia-service-desk-v1` · Base SHA: `6278fda` (no drift)

## Status by task

| Task | State | Evidence |
|---|---|---|
| T0 baseline/provenance | ✅ committed `dd3f462` | baseline + locks + verifier (`docs/service-desk/evidence/baseline.txt`) |
| T1 identity + schema | ✅ committed `c96ee86` | 24 tests (migration/authorization) on real PostgreSQL |
| T2 Jira reads + projection | ✅ committed `50d2b18` | 13 adapter + 7 projection tests; live smoke `jira-adapter-live.txt` |
| T2 Jira sandbox gate | ✅ PASSED (2026-09-19) | site live, project ITSD (10001), Basic auth verified, metadata mapped |

Service Desk suite: **41 passed** (settings, migrations, authorization, adapter, projection).

## Jira sandbox facts (verified live, read-only)

- Site: `https://maia-sandbox.atlassian.net`, project `ITSD` id `10001`
  (classic software project), issue types Task/Sub-task.
- Priorities mapped: `1 Highest, 2 High, 3 Medium, 4 Low, 5 Lowest`
  — must come from tenant config, never hard-coded (S7).
- Statuses: To Do / In Progress / Done.
- **`/rest/api/3/search/jql` (POST, paginated) works**; the classic
  `/rest/api/3/search` is deprecated — the adapter uses the new endpoint.
- Credentials live only in the git-ignored `.env.servicedesk`
  (`JIRA_API_TOKEN`); evidence files contain no tokens.

## Concurrent-work collision (needs a decision)

A second agent session is implementing a **different** plan in the SAME
checkout and branch: `plans/plan-20260919-1925-maia-video-pipeline-v1.md`
(FFmpeg video pipeline: `src/maia/video/`, `alembic_video/`,
`deploy/docker/compose.video.yml`, `tests/test_video.py`,
`requirements-video.txt`, and a modified `src/maia/api.py`).

Consequences observed:

- `tests/test_no_stream_imports.py` fails while their work is mid-write:
  `src/maia/video/api.py` and `src/maia/video/pipeline.py` have syntax
  errors. Verified: every `src/maia` file outside `src/maia/video/` parses
  cleanly, so the failure is theirs and transient.
- Untracked files from both sessions coexist; T2 commits were therefore made
  with **explicit paths only** (never `git add -A`).

Recommendation: give each plan its own git worktree/clone (the plan's own
rule: "tạo một worktree MAIA cho scope này") so the two implementations stop
sharing a working tree and branch.

## Next steps

- T3: copy SmartDoc context helper (donor SHA pinned) + versioned knowledge
  ingestion; independent of Jira.
- T5/T6/T7: triage/draft, approvals, durable worker + Jira delivery
  (sandbox is now live, so T7 write path can be exercised for real).
- T8: signed webhook + full round trip (the G2 proof point).
