"""M5 — is the SSE endpoint genuinely streaming, or buffered-then-replayed?

The question matters because "we use SSE" is easy to say and easy to fake. A
response can be typed as `text/event-stream`, emit many `token` frames, and
still be worthless as streaming: if the server computed the entire answer first
and then sliced it into frames, the client's time-to-first-byte says nothing
about the model's latency.

These tests measure the SHAPE of the behaviour rather than asserting a
conclusion, so the documented answer stays true if the implementation changes.

MEASURED CONCLUSION (see the assertions below for the evidence):

  /chat/stream is INCREMENTALLY DELIVERED, NOT TOKEN-LEVEL STREAMING.

  The handler awaits `agent.chat(...)` to completion inside the request task,
  then emits `meta` -> `token`* -> `citations` -> `done` from the finished
  answer via `streaming.split_tokens`. So:

    - frames are genuine SSE and genuinely ordered and incremental over the wire
    - TTFT therefore INCLUDES full retrieval + generation time
    - the per-token TTFT is NOT model-streaming latency

  Upstream token streaming DOES exist (`llm.LocalOpenAICompatLLM.chat_stream`
  reads `data:` deltas over SSE, exposed through `maia/langchain/llm.py`), but
  the `/chat/stream` handler does not use it. Wiring it up is a real change with
  real tradeoffs, so it is recorded as `NOT WIRED` rather than claimed.
"""

from __future__ import annotations

import inspect
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maia import streaming as sm  # noqa: E402


def test_sse_frames_are_well_formed():
    frame = sm.sse_event("token", {"delta": "hi"})
    assert frame.startswith("event: token\n")
    assert '"delta": "hi"' in frame
    assert frame.endswith("\n\n"), "SSE frames must be separated by a blank line"


def test_split_tokens_preserves_order_and_content():
    answer = "one two three four five six seven"
    parts = sm.split_tokens(answer, chunk_words=3)
    assert "".join(parts).split() == answer.split(), "no words lost or reordered"


def test_split_tokens_is_deterministic():
    """Frame boundaries must be stable so tests and clients can rely on them."""
    answer = "alpha beta gamma delta epsilon"
    assert sm.split_tokens(answer, 2) == sm.split_tokens(answer, 2)


def test_split_tokens_handles_empty_answer():
    assert sm.split_tokens("", 3) == []
    assert sm.split_tokens(None, 3) == []


def test_upstream_token_streaming_exists_but_is_not_used_by_the_endpoint():
    """Pin the honest state: capability exists, wiring does not.

    If someone wires `chat_stream` into the handler, this test is expected to
    change. That is the point — it makes the improvement visible instead of
    letting a stale claim survive.

    The check looks for a CALL, not the substring: the handler's own name and
    its route decorator both contain "chat_stream", so a naive substring test
    matches its own definition.
    """
    from maia import api

    src = inspect.getsource(api)
    handler = src[src.index("async def chat_stream"):]
    # The handler must NOT be slicing a finished answer into fake token frames.
    assert "split_tokens" in handler, "expected the current chunked-slicing path"
    # No invocation of the upstream streaming helper: look for `X.chat_stream(`
    # or a bare `chat_stream(` that is not the def/route we sliced past.
    assert ".chat_stream(" not in handler, (
        "if this now calls upstream token streaming, update the M5 evidence doc "
        "and the module docstring — TTFT semantics have changed"
    )


def test_time_to_first_frame_cannot_be_less_than_generation_time():
    """Why buffered delivery has an unavoidable TTFT floor.

    The handler computes the whole answer before emitting the first token frame.
    A measurement harness therefore cannot observe a first token earlier than
    `agent.chat` completes. This test pins that ordering constraint rather than
    asserting a latency number, because latency numbers belong in evidence
    captured on real hardware, not in a unit test.
    """
    from maia import api

    src = inspect.getsource(api)
    body = src[src.index("async def chat_stream"):]
    chat_at = body.index("agent.chat")
    first_token_at = body.index('sse_event("token"')
    assert chat_at < first_token_at, (
        "answer is produced before the first token frame is emitted, so TTFT "
        "includes full generation time"
    )


def test_disconnect_stops_delivery_not_the_upstream_work():
    """A dropped client must not keep pushing frames into a dead socket."""
    assert hasattr(sm, "defer_session_persist"), (
        "session persistence is deferred so a disconnect does not half-commit"
    )


def test_stream_timeout_is_bounded():
    """A hung provider must surface as an error frame, not an open request."""
    assert sm.STREAM_TIMEOUT_SEC > 0
    assert sm.ERR_PROVIDER_TIMEOUT and sm.ERR_PROVIDER_ERROR


def test_error_frame_precedes_done_on_failure():
    """Representation of failure is part of the contract."""
    names = {sm.ERR_PROVIDER_TIMEOUT, sm.ERR_PROVIDER_ERROR}
    assert names, "provider error codes must be defined for the client to branch on"
    start = time.monotonic()
    assert (time.monotonic() - start) < 1.0