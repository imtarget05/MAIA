"""Every MAIA LLM/embedding client path is wired to the centralized gateway.

MAIA routes all model traffic through the OpenAI-compatible ``llm-gateway`` at
``http://localhost:8787/v1`` and attributes it with the ``X-Project: MAIA``
header, so its usage shows up in the gateway's central telemetry.

These tests are fully offline: ``requests.post`` is replaced by a recorder, so
no socket is ever opened (no gateway, no LAN upstream, no network). The three
client paths asserted are the ones that can reach a model:

  1. ``LocalOpenAICompatLLM``            -> POST {base}/chat/completions
  2. ``CloudflareLangChainAdapter``      -> delegates to (1) via build_llm()
  3. ``LocalOpenAICompatEmbedder``       -> POST {base}/embeddings
"""
import json

import json as _json

import pytest
import requests

from maia import embeddings as emb
from maia import llm as llm_mod
from maia.config import settings
from maia.langchain.llm import CloudflareLangChainAdapter, cloudflare_lcel
from maia.llm import LocalOpenAICompatLLM, build_llm
from maia.llm_endpoints import (
    PROJECT_ID,
    project_headers,
    resolve_base_urls,
    split_timeout,
)

GATEWAY = "http://localhost:8787/v1"
DIRECT = "http://192.168.1.8:1234/v1"


class _Recorder:
    """Stand-in for ``requests.post`` that records instead of calling out."""

    def __init__(self, payload=None, connection_error_hosts=()):
        self.calls = []
        self.payload = payload or {"choices": [{"message": {"content": "pong"}}]}
        self.connection_error_hosts = set(connection_error_hosts)

    def __call__(self, url, headers=None, json=None, timeout=None, **kwargs):
        self.calls.append({"url": url, "headers": headers or {},
                           "json": json or {}, "timeout": timeout})
        for host in self.connection_error_hosts:
            if url.startswith(host):
                raise requests.exceptions.ConnectionError(f"refused: {url}")
        body = self.payload
        if callable(body):
            body = body(url, json or {})
        response = requests.Response()
        response.status_code = 200
        response._content = _json.dumps(body).encode()
        response.url = url
        return response


@pytest.fixture
def recorder(monkeypatch):
    """Replace requests.post in every client module with a recorder."""
    rec = _Recorder()
    monkeypatch.setattr(llm_mod.requests, "post", rec)
    monkeypatch.setattr(emb.requests, "post", rec)
    return rec


def _embed_payload(url, body):
    n = len(body.get("input") or [])
    return {"data": [{"embedding": [0.1, 0.2, 0.3]} for _ in range(n)]}


# --- 1. resolution order -------------------------------------------------

def test_resolution_order_is_explicit_then_gateway_then_direct():
    assert resolve_base_urls("", GATEWAY, DIRECT) == [GATEWAY, DIRECT]
    # An explicitly set canonical var wins, and is de-duplicated.
    assert resolve_base_urls(GATEWAY, GATEWAY, DIRECT) == [GATEWAY, DIRECT]
    assert resolve_base_urls("http://host:1234/v1/", GATEWAY) == [
        "http://host:1234/v1", GATEWAY]


def test_settings_default_to_the_gateway():
    assert settings.LLM_GATEWAY_URL == GATEWAY
    assert settings.LLM_PROJECT == "MAIA"
    assert PROJECT_ID == "MAIA"


def test_split_timeout_bounds_a_down_gateway():
    connect, read = split_timeout(120, 3)
    assert connect == 3 and read == 120


# --- 2. main LLM client --------------------------------------------------

def test_main_llm_client_targets_gateway_with_project_header(recorder):
    client = LocalOpenAICompatLLM(base_url=settings.LLM_BASE_URL,
                                  model=settings.LLM_CHAT_MODEL)
    assert client.base_urls[0] == GATEWAY

    assert client.chat([{"role": "user", "content": "ping"}]) == "pong"

    call = recorder.calls[-1]
    assert call["url"] == f"{GATEWAY}/chat/completions"
    assert call["headers"]["X-Project"] == "MAIA"
    assert call["json"]["model"] == settings.LLM_CHAT_MODEL
    assert call["timeout"][1] == client.timeout


def test_build_llm_client_is_gateway_first_with_direct_fallback(recorder):
    client = build_llm()
    assert isinstance(client, LocalOpenAICompatLLM)
    assert client.base_urls == [GATEWAY, DIRECT]


# --- 3. graceful degradation when the gateway is down -------------------

def test_gateway_down_falls_back_to_direct_upstream(monkeypatch):
    rec = _Recorder(connection_error_hosts=[GATEWAY])
    monkeypatch.setattr(llm_mod.requests, "post", rec)

    client = LocalOpenAICompatLLM()
    assert client.chat([{"role": "user", "content": "ping"}]) == "pong"

    assert [c["url"] for c in rec.calls] == [
        f"{GATEWAY}/chat/completions",
        f"{DIRECT}/chat/completions",
    ]
    # The fallback still carries the attribution header.
    assert rec.calls[-1]["headers"]["X-Project"] == "MAIA"


def test_reachable_endpoint_error_is_not_retried_elsewhere(monkeypatch):
    """An HTTP error is never re-sent to another host: no duplicated work."""
    calls = []

    def _boom(url, headers=None, json=None, timeout=None, **kwargs):
        calls.append(url)
        raise requests.exceptions.HTTPError("500 from upstream")

    monkeypatch.setattr(llm_mod.requests, "post", _boom)
    client = LocalOpenAICompatLLM()
    out = client.chat([{"role": "user", "content": "ping"}])

    assert calls == [f"{GATEWAY}/chat/completions"]
    assert "degraded" in out


def test_every_endpoint_down_degrades_instead_of_raising(monkeypatch):
    def _down(url, headers=None, json=None, timeout=None, **kwargs):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(llm_mod.requests, "post", _down)
    out = LocalOpenAICompatLLM().chat([{"role": "user", "content": "ping"}])
    assert "degraded" in out


# --- 4. LangChain client path -------------------------------------------

def test_langchain_client_sends_project_header_to_gateway(recorder):
    adapter = cloudflare_lcel()
    assert isinstance(adapter, CloudflareLangChainAdapter)

    from langchain_core.messages import HumanMessage

    result = adapter.invoke([HumanMessage(content="ping")])
    assert result.content == "pong"

    call = recorder.calls[-1]
    assert call["url"] == f"{GATEWAY}/chat/completions"
    assert call["headers"]["X-Project"] == "MAIA"


def test_langchain_stream_also_targets_the_gateway(recorder):
    from langchain_core.messages import HumanMessage

    list(CloudflareLangChainAdapter(LocalOpenAICompatLLM()).stream(
        [HumanMessage(content="ping")]))
    assert recorder.calls[-1]["url"] == f"{GATEWAY}/chat/completions"
    assert recorder.calls[-1]["headers"]["X-Project"] == "MAIA"


# --- 5. embeddings client -----------------------------------------------

def test_embeddings_client_targets_gateway_with_project_header(monkeypatch):
    monkeypatch.setattr(emb.settings, "EMBEDDINGS_DIM", 3)
    rec = _Recorder(payload=_embed_payload)
    monkeypatch.setattr(emb.requests, "post", rec)

    client = emb.LocalOpenAICompatEmbedder()
    assert client.base_urls[0] == GATEWAY

    out = client.embed(["alpha", "beta"])
    assert out.shape == (2, 3)

    call = rec.calls[-1]
    assert call["url"] == f"{GATEWAY}/embeddings"
    assert call["headers"]["X-Project"] == "MAIA"
    assert call["json"]["model"] == settings.EMBEDDINGS_MODEL


def test_embeddings_fall_back_to_direct_upstream_when_gateway_down(monkeypatch):
    monkeypatch.setattr(emb.settings, "EMBEDDINGS_DIM", 3)
    rec = _Recorder(payload=_embed_payload, connection_error_hosts=[GATEWAY])
    monkeypatch.setattr(emb.requests, "post", rec)

    out = emb.LocalOpenAICompatEmbedder().embed(["alpha"])
    assert out.shape == (1, 3)
    assert [c["url"] for c in rec.calls] == [
        f"{GATEWAY}/embeddings", f"{DIRECT}/embeddings"]
    assert rec.calls[-1]["headers"]["X-Project"] == "MAIA"


def test_embeddings_dim_mismatch_stays_loud(monkeypatch):
    monkeypatch.setattr(emb.settings, "EMBEDDINGS_DIM", 1024)
    rec = _Recorder(payload=_embed_payload)  # returns width 3
    monkeypatch.setattr(emb.requests, "post", rec)

    with pytest.raises(emb.EmbeddingDimMismatch):
        emb.LocalOpenAICompatEmbedder().embed(["alpha"])


def test_embeddings_provider_is_opt_in_and_off_by_default():
    """Default keeps the existing Cloudflare -> fastembed -> hash chain."""
    assert settings.EMBEDDINGS_PROVIDER == ""
    assert "local_openai" in emb.GATEWAY_EMBEDDING_PROVIDERS
    # "" must not select the gateway backend.
    assert "" not in emb.GATEWAY_EMBEDDING_PROVIDERS


def test_project_headers_helper_is_the_single_source():
    assert project_headers()["X-Project"] == "MAIA"
    assert project_headers({"Authorization": "Bearer k"})["Authorization"] == "Bearer k"


def test_embedder_uses_gateway_when_provider_opted_in(monkeypatch):
    # conftest pins MAIA_EMBED_FORCE_HASH=1 for the offline suite; the
    # provider selection is what this test is about.
    monkeypatch.delenv("MAIA_EMBED_FORCE_HASH", raising=False)
    monkeypatch.setattr(emb.settings, "EMBEDDINGS_PROVIDER", "local_openai")
    monkeypatch.setattr(emb.settings, "EMBEDDINGS_DIM", 3)
    e = emb.Embedder(dim=3)
    assert e.mode == "local_openai"
    assert isinstance(e._backend, emb.LocalOpenAICompatEmbedder)
