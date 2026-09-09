# _archive/ — Archived experimental features (Simplification Step 1)

Code moved here is **out of scope** for `docs/spec.md` (core RAG only) and is
NOT imported by `src/`, NOT collected by `pytest tests/`, NOT linted by CI
(`ruff`/`pyright` run on `src/` only).

| Path | What | Why archived |
|------|------|--------------|
| `src/maia/voice/` | STT/TTS providers + VoiceChatHandler | Not in spec; thin tests |
| `src/maia/finetune/` | Golden→triplet export + train runner | Not in spec; prototype |
| `src/maia/mcp/` | GitHub/Notion read-only connectors | Not in spec; experimental |
| `src/maia/agent/teams.py` | Researcher→analyst→writer→reviewer team | Not in spec; experimental |
| `src/maia/agent/team/` | Router-delegation team | Not in spec; experimental |
| `tests/test_{voice,finetune,mcp,teams,team}.py` | Their test suites | Moved with the code |

Kept (flag-gated, wired into core paths): `stream/` (store+metrics are
load-bearing test infra), `loops/corrective_rag.py` (behind `CRAG_ENABLED`),
`agent/memory.py` (behind `LTM_ENABLED`).

To restore a feature: `git mv` it back, re-add its flat fields to
`src/maia/config.py`, re-add API routes/CLI commands, move its tests back.
