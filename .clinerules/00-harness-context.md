Before starting work, read these file-backed harness state files to resume previous work and avoid losing context:

1. `.ai/harness/handoff/current.md` — last session handoff snapshot
2. `.ai/harness/handoff/resume.md` — resume packet with next steps and blockers
3. `tasks/current.md` — current task state derived from workflow artifacts
4. `docs/spec.md` — stable product intent
5. `.ai/context/context-map.json` — progressive context map for files being touched
6. `.ai/context/capabilities.json` — capability contracts for the repo

The repo root `AGENTS.md` and `CLAUDE.md` contain the workflow contract. Follow them.

Use the `codegraph` MCP for structural queries (callers, callees, definitions) instead of grep-and-read loops.

On finishing or pausing a task, update:

- `.ai/harness/handoff/current.md` — write the current-state handoff snapshot
- `tasks/current.md` — reflect the latest task state

Keep the harness workflow surfaces (plans/, tasks/, docs/spec.md) synchronized with substantive changes.