"""Degradation must be OBSERVABLE, not just visible in the prose.

Root cause this file pins: every silent fallback in `maia.llm` returned the
mock text shaped like a real answer, and three of the four paths carried no
machine-detectable marker at all. The worst was
`LocalOpenAICompatLLM.chat` on HTTP 200 with an empty body: a caller that
checks only for non-empty output cannot tell a fabricated answer from a
generated one.

`mode` is a STATIC property of configuration (creds present, provider name),
never of what the last call did, so `/ready` publishing `llm_mode` could not
report that generation had degraded. That is what the `generation_state` /
`last_generation_state()` pair fixes:

* per-instance `generation_state`  -> what THIS object returned last
* module-level `last_generation_state()` -> what the PROCESS returned last

The module-level one is what `/ready` reads, because `build_stack()` builds a
fresh LLM object per request: a per-instance flag read from a freshly built
object would always be "unknown" and the probe would lie in the other
direction.

Fully offline: `requests.post` is replaced by a recorder in every test, so no
socket is opened (no gateway, no LAN upstream, no Cloudflare).
"""
from __future__ import annotations

import json as _json

import pytest
import requests

from maia import api
from maia import llm as llm_mod
from maia.llm import (
    GENERATION_DEGRADED,
    GENERATION_MOCK,
    GENERATION_OK,
    GENERATION_UNKNOWN,
    CloudflareLLM,
    LocalOpenAICompatLLM,
)

MESSAGES = [{"role": "user", "content": "ping [S1]"}]


def _response(payload: dict, status: int = 200) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = _json.dumps(payload).encode()  # noqa: SLF001
    return r


@pytest.fixture(autouse=True)
def _reset_generation_state():
    """The process-wide last-call state is module state; never leak into a test."""
    llm_mod._record_generation(GENERATION_UNKNOWN)  # noqa: SLF001
    yield
    llm_mod._record_generation(GENERATION_UNKNOWN)  # noqa: SLF001


# --------------------------------------------------------------------------- #
# 1. every fallback path is identifiable IN THE RETURNED TEXT
# --------------------------------------------------------------------------- #

def test_local_http_200_with_empty_content_is_marked_degraded(monkeypatch):
    """HTTP 200, empty choices content: was the one path with NO marker.

    The pre-fix code returned `self._mock(messages)` verbatim, byte-identical
    to a deliberate mock run -- which is exactly what the defect describes.
    """
    monkeypatch.setattr(
        llm_mod.requests, "post",
        lambda *a, **k: _response({"choices": [{"message": {"content": "  "}}]}),
    )
    client = LocalOpenAICompatLLM()
    out = client.chat(MESSAGES)

    assert out.startswith("[LLM local degraded:"), (
        f"an empty-but-200 upstream response produced UNMARKED text: {out!r}"
    )
    assert llm_mod.DEGRADED_MARKER in out
    assert client.generation_state == GENERATION_DEGRADED


def test_local_empty_choices_list_is_marked_degraded(monkeypatch):
    """Same path, one step further out: no `choices` key at all."""
    monkeypatch.setattr(
        llm_mod.requests, "post",
        lambda *a, **k: _response({}),
    )
    client = LocalOpenAICompatLLM()
    out = client.chat(MESSAGES)

    assert llm_mod.DEGRADED_MARKER in out
    assert client.generation_state == GENERATION_DEGRADED


def test_local_connection_error_on_every_endpoint_is_marked_degraded(monkeypatch):
    def _down(*a, **k):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(llm_mod.requests, "post", _down)
    client = LocalOpenAICompatLLM()
    out = client.chat(MESSAGES)

    assert llm_mod.DEGRADED_MARKER in out
    assert "refused" in out
    assert client.generation_state == GENERATION_DEGRADED


def test_local_reachable_but_failing_endpoint_is_marked_degraded(monkeypatch):
    def _boom(*a, **k):
        raise requests.exceptions.HTTPError("500 from upstream")

    monkeypatch.setattr(llm_mod.requests, "post", _boom)
    client = LocalOpenAICompatLLM()
    out = client.chat(MESSAGES)

    assert llm_mod.DEGRADED_MARKER in out
    assert client.generation_state == GENERATION_DEGRADED


def test_no_endpoint_configured_is_marked_degraded(monkeypatch):
    """`base_urls` empty -> the for-loop never runs. Was the fourth unmarked path."""
    monkeypatch.setattr(llm_mod.settings, "LLM_BASE_URL", "")
    monkeypatch.setattr(llm_mod.settings, "LLM_GATEWAY_URL", "")
    monkeypatch.setattr(llm_mod.settings, "LLM_DIRECT_UPSTREAM_URL", "")
    client = LocalOpenAICompatLLM()
    out = client.chat(MESSAGES)

    assert client.base_urls == []
    assert llm_mod.DEGRADED_MARKER in out
    assert client.generation_state == GENERATION_DEGRADED


def test_cloudflare_degraded_path_keeps_its_marker_and_records_state(monkeypatch):
    """The two marked paths must STAY marked: the human-visible prefix is the
    contract callers already parse, this change only adds the state."""
    from maia.loops.resilience import CircuitBreaker

    client = CloudflareLLM("acct", "token")
    assert client.mode == "cloudflare"

    cb = CircuitBreaker("llm-test-degraded", failure_threshold=1, recovery_timeout=60.0)
    cb.record_failure()

    monkeypatch.setattr(llm_mod, "_get_llm_breaker", lambda: cb)
    out = client.chat(MESSAGES)

    assert out.startswith("[LLM degraded:")
    assert client.generation_state == GENERATION_DEGRADED
    assert llm_mod.last_generation_state()[0] == GENERATION_DEGRADED


def test_cloudflare_mock_provider_is_reported_as_mock_not_degraded():
    """`LLM_PROVIDER=mock` is a DELIBERATE configuration, not a failure.

    Recording it as `degraded` would make the field useless: a permanently
    mock-mode deployment would look permanently broken.
    """
    client = CloudflareLLM(provider=llm_mod.PROVIDER_MOCK)
    assert client.mode == "mock"
    out = client.chat(MESSAGES)

    assert out.startswith("(MOCK LLM")
    assert client.generation_state == GENERATION_MOCK
    assert llm_mod.last_generation_state()[0] == GENERATION_MOCK


def test_cloudflare_requested_but_creds_missing_is_reported_degraded():
    """Provider is cloudflare, no creds -> the mode reads "mock" but nothing
    broke: this is a MISCONFIGURATION, so it must read as degraded."""
    client = CloudflareLLM("", "", provider=llm_mod.PROVIDER_CLOUDFLARE)
    assert client.mode == "mock"
    client.chat(MESSAGES)

    assert client.generation_state == GENERATION_DEGRADED
    assert llm_mod.last_generation_state()[0] == GENERATION_DEGRADED


# --------------------------------------------------------------------------- #
# 2. the success path must NOT be degraded
# --------------------------------------------------------------------------- #

def test_local_success_is_not_degraded(monkeypatch):
    monkeypatch.setattr(
        llm_mod.requests, "post",
        lambda *a, **k: _response({"choices": [{"message": {"content": "pong"}}]}),
    )
    client = LocalOpenAICompatLLM()
    out = client.chat(MESSAGES)

    assert out == "pong"
    assert llm_mod.DEGRADED_MARKER not in out
    assert client.generation_state == GENERATION_OK
    assert llm_mod.last_generation_state() == (GENERATION_OK, "")


def test_cloudflare_success_is_not_degraded(monkeypatch):
    """The real Workers AI call, stubbed at requests.post. The URL and the
    `choices` parser are part of the contract and must stay reachable."""
    calls: list[str] = []

    def _ok(url, headers=None, json=None, timeout=None, **kwargs):
        calls.append(url)
        return _response({"result": {"choices": [{"message": {"content": "pong"}}]}})

    monkeypatch.setattr(llm_mod.requests, "post", _ok)
    client = CloudflareLLM("acct", "token", provider=llm_mod.PROVIDER_CLOUDFLARE)
    out = client.chat(MESSAGES)

    assert out == "pong"
    assert calls == [
        "https://api.cloudflare.com/client/v4/accounts/acct/ai/run/"
        f"{llm_mod.settings.CLOUDFLARE_MODEL}"
    ]
    assert client.generation_state == GENERATION_OK


# --------------------------------------------------------------------------- #
# 3. process-wide last-call state, and what /ready publishes
# --------------------------------------------------------------------------- #

def test_last_generation_state_is_unknown_before_any_call():
    assert llm_mod.last_generation_state() == (GENERATION_UNKNOWN, "")


def test_last_generation_state_tracks_the_most_recent_call_of_a_fresh_object(monkeypatch):
    """The reason the flag is module-level: `build_stack()` builds a NEW LLM per
    request, so the object `/ready` builds has never been called."""
    monkeypatch.setattr(
        llm_mod.requests, "post",
        lambda *a, **k: _response({"choices": [{"message": {"content": "pong"}}]}),
    )
    LocalOpenAICompatLLM().chat(MESSAGES)

    fresh = LocalOpenAICompatLLM()
    assert fresh.generation_state == GENERATION_UNKNOWN, "a new object made no call"
    assert llm_mod.last_generation_state()[0] == GENERATION_OK, (
        "but the PROCESS did generate, and that is what /ready must report"
    )


def test_ready_publishes_generation_state_next_to_llm_mode(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(
        llm_mod.requests, "post",
        lambda *a, **k: _response({"choices": [{"message": {"content": "pong"}}]}),
    )
    LocalOpenAICompatLLM().chat(MESSAGES)

    body = TestClient(api.app).get("/ready").json()
    assert body["status"] in ("ok", "degraded")
    if body["status"] == "ok":
        assert body["llm_mode"] == "local", "the STATIC configured mode"
        assert body["llm_generation_state"] == GENERATION_OK, "the call OUTCOME"


def test_ready_reports_degraded_generation_truthfully(monkeypatch):
    """A degraded last call must be visible on the probe even though `mode`
    still says `local` -- the defect was that readiness could not say this."""
    from fastapi.testclient import TestClient

    def _down(*a, **k):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(llm_mod.requests, "post", _down)
    LocalOpenAICompatLLM().chat(MESSAGES)

    body = TestClient(api.app).get("/ready").json()
    if body["status"] == "ok":
        assert body["llm_mode"] == "local"
        assert body["llm_generation_state"] == GENERATION_DEGRADED