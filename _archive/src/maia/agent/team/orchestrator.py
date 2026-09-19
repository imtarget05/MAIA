"""TeamOrchestrator: route -> delegate -> collect (hop guard + soft timeout).

Flow per question (hops = member delegations, router excluded):
    user -> router (intent, route) -> member[route] -> result
        -> out_of_scope? reroute to knowledge (costs a hop)
        -> hops exhausted? partial error, never infinite loop
        -> soft timeout exceeded? timeout error (checked between hops;
           sync LLM calls can't be preempted mid-flight — documented)

Side effects are never executed here: a needs_approval member result is
surfaced with its pending_action intact for the human to confirm.
"""
from __future__ import annotations

import time
from typing import Any

from ...config import settings
from .agents import default_members, route_for_intent
from .base import TeamAgent, TeamBus


class TeamOrchestrator:
    def __init__(self, members: dict[str, TeamAgent] | None = None,
                 bus: TeamBus | None = None,
                 max_hops: int | None = None,
                 timeout_sec: float | None = None):
        self.bus = bus or TeamBus()
        self.members = members if members is not None else default_members(self.bus)
        for m in self.members.values():
            if m.bus is None:
                m.bus = self.bus
        self.max_hops = settings.TEAM_MAX_HOPS if max_hops is None else max_hops
        self.timeout_sec = settings.TEAM_TIMEOUT_SEC if timeout_sec is None else timeout_sec

    def _timed_out(self, start: float) -> bool:
        try:
            return (time.time() - start) > float(self.timeout_sec)
        except Exception:
            return False

    def _timeout_result(self, trace_id: str, question: str, hops: int) -> dict:
        try:
            from ...stream.metrics import registry
            registry.inc("maia_team_chat_total")
            registry.inc("maia_team_timeout_total")
        except Exception:
            pass
        return {"status": "error", "member": "orchestrator", "route": "timeout",
                "answer": "Nhóm agent hết thời gian xử lý. Vui lòng thử lại với câu hỏi ngắn gọn hơn.",
                "intent": "general", "citations": [], "slots": {},
                "team_trace": self.bus.history(trace_id), "trace_id": trace_id,
                "hops": hops, "question": question}

    def run(self, question: str, session_id: str = "team_default",
            employee_id: str | None = None, tenant_id: str | None = None,
            top_k: int | None = None) -> dict:
        start = time.time()
        router = self.members.get("router")
        if router is None:
            return {"status": "error", "member": "orchestrator", "route": "none",
                    "answer": "Nhóm agent chưa được cấu hình (thiếu router).",
                    "intent": "general", "citations": [], "slots": {},
                    "team_trace": [], "trace_id": "", "hops": 0, "question": question}
        if self._timed_out(start):
            return self._timeout_result("", question, 0)

        task = {"question": question, "session_id": session_id,
                "employee_id": employee_id, "tenant_id": tenant_id, "top_k": top_k}
        rmsg = router.handle(self.bus.post("orchestrator", "router", "task", task))
        route = (rmsg.payload.get("route") or "knowledge")
        intent = rmsg.payload.get("intent", "general")
        if route not in self.members:
            route = "knowledge"

        hops = 0
        target, tried = route, {route}
        last_result: dict[str, Any] | None = None
        while True:
            if self._timed_out(start):
                return self._timeout_result(rmsg.trace_id, question, hops)
            if hops >= max(1, self.max_hops):
                try:
                    from ...stream.metrics import registry
                    registry.inc("maia_team_chat_total")
                    registry.inc("maia_team_hop_exhausted_total")
                except Exception:
                    pass
                base = dict(last_result) if last_result else {}
                base.update({"status": "error", "member": "orchestrator",
                             "route": target, "intent": intent,
                             "answer": "Nhóm agent vượt quá số bước cho phép. Vui lòng thử lại.",
                             "team_trace": self.bus.history(rmsg.trace_id),
                             "trace_id": rmsg.trace_id, "hops": hops, "question": question})
                return base
            member = self.members[target]
            tmsg = self.bus.post("orchestrator", target, "task", task, rmsg.trace_id)
            out = member.handle(tmsg)
            hops += 1
            payload = dict(out.payload)
            if payload.get("status") == "out_of_scope" and "knowledge" not in tried \
                    and "knowledge" in self.members:
                tried.add("knowledge")
                last_result = payload
                target = "knowledge"  # reroute costs another hop
                continue
            payload.update({"member": payload.get("member", target), "route": target,
                            "intent": payload.get("intent", intent),
                            "team_trace": self.bus.history(rmsg.trace_id),
                            "trace_id": rmsg.trace_id, "hops": hops,
                            "question": question})
            try:
                from ...stream.metrics import registry
                registry.inc("maia_team_chat_total")
            except Exception:
                pass
            return payload
