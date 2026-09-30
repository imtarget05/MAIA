"""Streaming latency evidence: TTFT + E2E over a fixed deterministic workload.

Mode: MOCK (stubbed agent, no LLM server). What this measures honestly is the
SSE delivery overhead of POST /chat/stream (auth + framing + first-token
flush), NOT provider generation speed. Run: python3 scripts/stream_latency.py
"""
import json
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient

import maia.agent.agent as agent_mod
from maia.agent.session import session_store
from maia.api import app, get_current_active_user

QUESTIONS = [
    "How many annual leave days do employees get?",
    "What is the IT security policy for lost devices?",
    "How do I request reimbursement for travel expenses?",
    "What benefits are included in the wellness program?",
    "How to access VPN at vpn.company.com?",
    "What is the onboarding process for new hires?",
    "Who do I contact for laptop repair?",
    "What is the expense approval threshold?",
    "How many sick days are allowed per year?",
    "What is the policy for remote work?",
    "How do I reset my SSO password?",
    "What insurance plans are available?",
    "How to book a meeting room?",
    "What is the data retention policy?",
    "How do I submit an IT helpdesk ticket?",
    "What training courses are mandatory?",
    "How does the performance review cycle work?",
    "What is the dress code policy?",
    "How do I update my emergency contacts?",
    "What holidays are observed this year?",
]

ANSWER = ("Company policy allows twelve days of annual leave every year for "
          "full time employees with manager approval required. [S1]")


class _StubAgent:
    def __init__(self, tenant_id=None):
        self.tenant_id = tenant_id or "default"

    def chat(self, question, session_id="default", employee_id=None,
             top_k_final=None, tenant_id=None, gen=None, requester_email=None):
        tid = tenant_id or self.tenant_id
        session_store.append(session_id, "user", question, "general", tenant_id=tid)
        session_store.append(session_id, "assistant", ANSWER, "general", tenant_id=tid)
        return {"answer": ANSWER, "status": "answered",
                "citations": [{"chunk_id": "c1", "filename": "IT_Handbook.md",
                               "tag": "[S1]"}], "intent": "general"}


def main() -> None:
    agent_mod.EnterpriseAgent = _StubAgent
    app.dependency_overrides[get_current_active_user] = lambda: SimpleNamespace(
        tenant_id="lat", employee_id="emp_lat", email="lat@company.com",
        id="lat1", is_active=True)
    client = TestClient(app, raise_server_exceptions=True)
    ttft, e2e = [], []
    for i, q in enumerate(QUESTIONS):
        sid = f"lat-{i}"
        t0 = time.monotonic()
        first = None
        with client.stream("POST", "/chat/stream",
                           json={"question": q, "session_id": sid}) as r:
            assert r.status_code == 200, r.text
            for line in r.iter_lines():
                if line.startswith("event: token"):
                    if first is None:
                        first = time.monotonic() - t0
                if line.startswith("event: done"):
                    break
        total = time.monotonic() - t0
        ttft.append(first or total)
        e2e.append(total)
    ttft_s, e2e_s = sorted(ttft), sorted(e2e)
    n = len(QUESTIONS)
    out = {
        "sample_size": n,
        "provider": "stubbed-agent (no LLM server)",
        "model": "n/a (deterministic canned answer)",
        "stream_mode": "MOCK",
        "ttft_ms": {"p50": round(statistics.median(ttft_s) * 1000, 1),
                    "p95": round(ttft_s[max(0, min(n - 1, int(0.95 * n)))] * 1000, 1)},
        "e2e_ms": {"p50": round(statistics.median(e2e_s) * 1000, 1),
                   "p95": round(e2e_s[max(0, min(n - 1, int(0.95 * n)))] * 1000, 1)},
    }
    print(json.dumps(out, indent=2))
    Path("docs/evidence/stream_latency.json").write_text(json.dumps(out, indent=2))
    print("wrote docs/evidence/stream_latency.json")


if __name__ == "__main__":
    main()
