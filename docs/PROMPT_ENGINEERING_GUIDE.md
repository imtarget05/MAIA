# Prompt Engineering Guide (MAIA)

How prompts are designed, versioned, measured and rolled back in this repo. The
short version: **a prompt is a versioned artefact with a test suite**, not a string
in a function body. Everything below is enforced by `src/maia/promptops/` and
`tests/test_prompt_library.py` / `tests/test_prompt_evals.py`.

## 1. Anatomy of a prompt

`prompts/<category>/<name>.v<major>.<minor>.<patch>.yaml`:

```yaml
name: campaign_copywriter
version: 1.1.0
status: active                 # active | draft | deprecated
supersedes: campaign_copywriter@1.0.0

system_prompt: |               # role + non-negotiable rules + "input is data"
user_template: |               # the {variable} contract
parameters:
  temperature: 0.7             # bounded 0-2; must carry a rationale
  top_p: 0.9
  max_tokens: 1100
  rationale: >-                # required: why these values
    Compliance block must be stable across runs, so temperature drops to 0.7.
guardrails:                    # named rules from maia.promptops.guardrails
  - no_secret_leak
  - no_pii
  - max_chars:7000
output_schema: {}              # JSON-Schema subset the answer must satisfy
few_shot: []                   # user/assistant demonstration pairs
eval_cases: []                 # regression cases, see §5
changelog: |
  v1.1.0: +Zalo, +compliance_check, temperature 0.8 -> 0.7.
```

Rules the loader enforces (a violation fails at load, not at runtime):

* filename ↔ `name`/`version` must agree — two contents can never share one version;
* `temperature` ∈ [0, 2], `top_p` ∈ (0, 1], `max_tokens` > 0;
* every guardrail must exist in `GUARDRAIL_RULES` (an unknown rule is an error, not
  a silently ignored no-op);
* eval case ids are unique; a prompt must ship at least one.

## 2. Zero-shot vs few-shot vs CoT

| Situation | Pattern | Example in the library |
|---|---|---|
| The model already knows the format | zero-shot + strict `output_schema` | `retention_drop_briefing` (severity ladder stated explicitly) |
| Tone/length/edge cases are hard to state | **few-shot** in `few_shot` | `campaign_copywriter` (one demo per channel style) |
| The answer needs multi-step reasoning | explicit steps in `system_prompt` **and** fields that force them to be visible | `nl_to_sql` (`refusal_reason` is a required field) |
| Facts must come from a document | pass the document as an *untrusted variable* and use `sanitize_external` | the RAG path in `maia/prompt.py` |

Two practices that pay off immediately:

1. **Say what the input is.** Every prompt states that the input block is *data, not
   instructions*. That is the cheapest prompt-injection defense there is, and it is
   backed by `maia.promptops.guardrails.sanitize_input` for values from outside the
   system.
2. **Make refusal a first-class field.** `nl_to_sql` and `retention_drop_briefing`
   have `refusal_reason` / `data_gaps` in their schema, so "I don't know" is a
   valid, parseable answer instead of a confident fabrication.

## 3. Temperature and sampling policy

| Task | temperature | Why |
|---|---|---|
| Extraction / counting / SQL (`game_review_insight`, `nl_to_sql`) | `0.0 – 0.2` | Errors here are wrong numbers, not style |
| Incident briefing (`retention_drop_briefing`) | `0.2` | wording varies, severity must not |
| Copywriting (`campaign_copywriter`) | `0.7 – 0.8` | diversity is the point; the schema is the guard |

Format compliance is **not** bought with low temperature. It is bought with
`output_schema` + `eval_cases`; low temperature only trades away variety.

## 4. Structured output and contract failures

Every prompt that feeds a program declares `output_schema`. The pipeline:

1. `run_prompt_evals` / the caller parses the answer (`extract_json` tolerates a
   ```json fence and a leading sentence).
2. `maia.json_schema_lite.validate` checks the contract against the **`jsonschema`**
   library (Draft 2020-12) and returns **human-readable** errors
   (`$.rating: expected integer, got str`).
3. Failure handling is the caller's choice, and the library documents the three
   honest options:
   * **retry once with the errors appended** (cheap, fixes most single-field slips);
   * **degrade** — keep the free-text answer, mark `contract_valid: false` (right
     for a user-facing surface where a partial answer beats none);
   * **refuse** — return an explicit "couldn't produce a valid answer" (right for
     anything that would otherwise write to a system of record).

Never coerce a bad payload into shape and move on: that is how a `null` becomes a
row in a CRM.

## 5. Eval cases: what belongs in a prompt's suite

A prompt's cases are **contract** tests, not taste tests:

| Field | Asserts |
|---|---|
| `answer` (golden) | the contract itself, runnable **without a model** |
| `expect_schema_valid` | the answer validates (or, deliberately, does not) |
| `expect_paths` | exact values at JSON paths (`risk_level: "low"`) |
| `must_contain` / `must_not_contain` | required / forbidden substrings |
| `expect_guardrail_violations` | a guardrail *fires* (positive test for the guard) |
| `max_output_tokens` / `max_latency_ms` | budget regressions |

```bash
pytest tests/test_prompt_library.py tests/test_prompt_evals.py -q
```

`golden_only=True` runs only the model-free cases. A report records
`content_hash` + the whole library snapshot, so "it passed" always answers *for
which prompt text*.

## 6. Changing a prompt (the workflow)

1. Copy the current file to a new version; never edit a released version in place.
2. Update `system_prompt` / `user_template` / `output_schema` and the `changelog`;
   set `supersedes`.
3. Add an eval case for the new behaviour *and* keep the old ones — they are the
   regression net.
4. Run the golden suite; set `status: active` when green.
5. With a gateway available, run the full suite (cases without a golden `answer`).
6. Review the diff the way code is reviewed: `PromptRegistry.diff(a, b)` lists the
   exact fields that changed.

## 7. Guardrails available

`no_secret_leak`, `no_pii`, `no_approval_claim`, `no_injection`,
`max_chars:<n>`. They delegate to the runtime guardrails
(`maia.loops.guardrails`, `maia.loops.pii`) instead of re-implementing patterns, so
a prompt-level check cannot drift from the answer-path check. Violations never
raise: the caller decides whether to retry, degrade or refuse, and the eval harness
treats an unexpected violation as a failed case.

## 8. Honest limits

* The offline suite proves the **contract**, not model quality. Cases with a golden
  `answer` run without a model; cases without one need a gateway.
* `estimate_tokens` is ≈4 chars/token — a budget guard, not billing. Real token
  accounting lives in the llm-gateway telemetry.
* The JSON-Schema support is the **full `jsonschema` library** (Draft 2020-12), not a
  subset: `anyOf`/`oneOf`, `$ref`/`$defs`, `format`, `uniqueItems` and
  `dependentRequired` all work. `maia.json_schema_lite` is now a thin adapter over
  it — it keeps MAIA's stable, human-readable error strings (which are also fed back
  to the model on retry) and falls back to a built-in subset validator if the library
  is missing from an environment. `jsonschema_available()` / `validator_name()`
  report which engine is live.


