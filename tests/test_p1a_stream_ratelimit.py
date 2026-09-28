"""P1a regression: rate-limit 429 + /api/v1 aliases + LLM SSE fallback honesty."""
from maia import api as api_mod
from maia.llm import LocalOpenAICompatLLM


def test_rate_limit_trips_at_60():
    api_mod._rl_hits.clear()
    for _ in range(60):
        api_mod.check_chat_rate_limit("test-user")
    try:
        api_mod.check_chat_rate_limit("test-user")
        raise AssertionError("expected 429")
    except Exception as e:
        assert getattr(e, "status_code", None) == 429


def test_v1_routes_registered():
    from fastapi.testclient import TestClient
    c = TestClient(api_mod.app)
    r = c.get("/api/v1/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_llm_chat_stream_falls_back_without_upstream(monkeypatch):
    llm = LocalOpenAICompatLLM(base_url="http://127.0.0.1:1", model="x", timeout=2)
    chunks = list(llm.chat_stream([{"role": "user", "content": "hello [S1] world. second sentence."}]))
    assert "".join(chunks).strip() != ""
