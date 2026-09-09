"""Team primitives: roles, messages, in-process bus, member ABC.

The bus is a plain in-memory deque with per-trace history: enough for a
single-process orchestrator (same constraint as the C1 pending store —
multi-worker deploys would need a shared bus, out of scope for local use).
"""
from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Literal

MsgKind = Literal["task", "result", "clarify"]


@dataclass(frozen=True)
class AgentRole:
    """Static capability card for a team member."""
    name: str                       # "router" | "knowledge" | "hr" | "it"
    description: str
    tools: tuple[str, ...] = ()     # TOOL_REGISTRY names this member may USE
    can_delegate: bool = False      # only the orchestrator/router delegates


@dataclass
class AgentMessage:
    trace_id: str
    msg_no: int
    sender: str
    recipient: str
    kind: MsgKind                   # task | result | clarify
    payload: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


class TeamBus:
    """Append-only per-trace message log (doubles as the team trace)."""

    def __init__(self, maxlen: int = 200):
        self._log: dict[str, deque] = defaultdict(lambda: deque(maxlen=maxlen))
        self._seq = 0

    def post(self, sender: str, recipient: str, kind: MsgKind,
             payload: dict | None = None, trace_id: str | None = None) -> AgentMessage:
        tid = trace_id or uuid.uuid4().hex[:8]
        self._seq += 1
        msg = AgentMessage(trace_id=tid, msg_no=self._seq, sender=sender,
                           recipient=recipient, kind=kind, payload=payload or {})
        self._log[tid].append(msg)
        return msg

    def history(self, trace_id: str) -> list[dict]:
        return [{"msg_no": m.msg_no, "from": m.sender, "to": m.recipient,
                 "kind": m.kind, "payload_keys": sorted(m.payload.keys()), "ts": m.ts}
                for m in self._log.get(trace_id, [])]

    def clear(self, trace_id: str) -> None:
        self._log.pop(trace_id, None)


class TeamAgent(ABC):
    """One team member. handle() never raises: failures become error results."""

    role: AgentRole

    def __init__(self, bus: TeamBus | None = None):
        self.bus = bus

    @abstractmethod
    def handle(self, msg: AgentMessage) -> AgentMessage:
        """Process a task message, return a result/clarify message."""
        ...

    def _reply(self, msg: AgentMessage, kind: MsgKind, payload: dict) -> AgentMessage:
        reply = AgentMessage(trace_id=msg.trace_id, msg_no=0, sender=self.role.name,
                             recipient=msg.sender, kind=kind, payload=payload)
        if self.bus is not None:
            return self.bus.post(reply.sender, reply.recipient, kind, payload, msg.trace_id)
        return reply

    def _fail(self, msg: AgentMessage, error: str) -> AgentMessage:
        return self._reply(msg, "result", {"status": "error", "member": self.role.name,
                                           "answer": "Thành viên nhóm gặp lỗi. Vui lòng thử lại.",
                                           "error": error})
