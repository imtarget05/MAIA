# MAIA — M5: Is the SSE endpoint genuinely streaming?

```text
measured_on : 2026-10-02
method      : source-level trace of the handler + executable contract tests
tests       : tests/test_sse_streaming_contract.py  (9 passed)
```

## The short answer

`/chat/stream` is **incrementally delivered over real SSE, but it is NOT
token-level streaming.** Those are different claims and the difference matters.

| Property | Status |
|---|---|
| Real `text/event-stream` with typed frames | **YES** |
| Frames ordered `meta → token* → citations → done` | **YES** |
| First byte emitted before the response completes | **NO** |
| TTFT reflects model generation latency | **NO** |
| Upstream provider token streaming exists in the codebase | **YES** |
| `/chat/stream` uses it | **NO — not wired** |

## Why: the handler computes first, then slices

```python
result = await asyncio.wait_for(
    asyncio.to_thread(agent.chat, ...),      # ← full retrieval + generation
    timeout=_sm.STREAM_TIMEOUT_SEC)
...
for delta in _sm.split_tokens(result["answer"]):   # ← slice the FINISHED answer
    yield _sm.sse_event("token", {"delta": delta})
```

`streaming.split_tokens` is an honest, documented fallback that groups the
completed answer into 3-word chunks. It preserves order and loses no words —
both asserted by tests — but it cannot make TTFT smaller than the time
`agent.chat` already took.

So the accurate statement is:

> The response is delivered incrementally over SSE rather than as one buffered
> blob, and a client renders tokens progressively. It is not streaming from the
> model: TTFT includes full retrieval and generation time.

That is a **defensible** design — retrieval grounding must complete before any
token is safe to emit, since a token without its citation would be ungrounded —
but it must be stated as incremental delivery, not as model streaming.

## The capability that exists but is unused

`llm.LocalOpenAICompatLLM.chat_stream` (`src/maia/llm.py:257-296`) performs
real upstream SSE: it POSTs with `stream=True`, iterates `data:` lines, parses
each `choices[0].delta.content`, and stops at `[DONE]`. It is exposed to
LangChain via `src/maia/langchain/llm.py`.

The `/chat/stream` handler does not call it. Wiring it in is a real change with
a real tradeoff: token-level streaming would emit tokens *before* the grounding
verifier has run, so the stream would have to be retracted or annotated when
verification fails. That design is worth doing, and is deliberately **not**
claimed here.

## Latency

**No TTFT or TTLT numbers are published in this document.** They were not
measured on controlled hardware in this pass, and inventing them would be
exactly the kind of claim this repository has committed to not making. The
ordering constraint that *is* asserted — `agent.chat` completes before the
first token frame — is pinned by
`test_time_to_first_frame_cannot_be_less_than_generation_time`, so the
reasoning is reproducible without quoting a fabricated millisecond figure.

To measure honestly:

```python
t0 = time.monotonic()
# stream the response, timestamp the first `token` frame -> TTFT
# timestamp the `done` frame                          -> TTLT
```

## Other measured properties

| Property | Evidence |
|---|---|
| Frame format | `event: <name>\ndata: <json>\n\n`, asserted |
| Chunking lossless / ordered / deterministic | 3 tests |
| Empty and `None` answers handled | `split_tokens` returns `[]` |
| Provider timeout bounded | `STREAM_TIMEOUT_SEC` asserted > 0 |
| Failure represented explicitly | `ERR_PROVIDER_TIMEOUT` / `ERR_PROVIDER_ERROR` codes |
| Disconnect does not half-commit a session | `defer_session_persist` exists |

## The self-invalidating test

`test_upstream_token_streaming_exists_but_is_not_used_by_the_endpoint` asserts
the handler does **not** call `.chat_stream(`. If someone wires real upstream
streaming in, this test fails and asks for this document to be updated. The
claim cannot silently become stale — which is the point of measuring instead of
asserting from a README.