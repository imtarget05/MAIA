# Third-Party Notices — MAIA Service Desk

The Service Desk slice (branch `codex/maia-service-desk-v1`) contains code
adapted from the projects below. Full provenance per file — source path,
pinned commit, donor hash, destination hash and a description of every
modification — is maintained in `docs/service-desk/reuse-manifest.json` and
verified by `scripts/verify_sd_provenance.py`.

---

## Smart-Document-Chatbot (donor, read-only)

- Repository: https://github.com/imtarget05/Smart-Document-Chatbot
- Commit used: `babe1c5badcf61f2a3059a5293cb059bdd2c49fd`
- License: MIT
- Code used in this repository:

| Donor path | Destination | Kind |
|---|---|---|
| `agent/memory/context_trim.py` | `src/maia/servicedesk/knowledge/context_budget.py` | copy + documented fixes |
| `agent/tests/test_context_trim.py` | `tests/servicedesk/test_context_budget.py` | adapted (project-1-only cases removed) |

Documented behavioural changes to the copied code are listed in the module
docstring of `context_budget.py` and in the reuse manifest. Concepts (not
code) reviewed from `DocumentAccessService.java`, `llm-router/app/db_jobs.py`
and the frontend citation components are recorded as `port_concept` /
`copy_adapt` entries; no Java, gateway or fine-tuning code was taken.

### MIT License text

```
MIT License

Copyright (c) 2026 imtarget05

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## MAIA (base repository)

This work is a continuation of the MAIA repository itself
(https://github.com/imtarget05/MAIA, base commit
`6278fda306c043d5363fa68940d4967ec14c4f52`). Reused MAIA modules are called
through adapters rather than forked where possible; the reuse manifest
records each one (for example `maia/chunking.py`, `maia/textnorm.py`).