"""Regression test for the live-probe finding (Azure, Qdrant unreachable).

When build_stack() fails, EnterpriseAgent.chat() must still return a
status=error dict (never raise): the error handler itself used to crash on
``self._llm.mode`` with AttributeError, turning a degraded pipeline into a
500 on /chat and a PROVIDER_ERROR on /chat/stream for the wrong reason.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import maia.pipeline_query as pq
from maia.agent.agent import EnterpriseAgent


def test_chat_returns_error_dict_when_stack_is_down(monkeypatch):
    def _boom(tenant_id=None):
        raise ConnectionError("Qdrant down (probe repro)")

    monkeypatch.setattr(pq, "build_stack", _boom)
    out = EnterpriseAgent(tenant_id="t-probe").chat(
        "How many leave days?", session_id="probe-repro")
    assert out["status"] == "error"
    assert out["llm_mode"] == "unknown"
    assert "error:" in " ".join(out.get("flags", []))
