"""Router-delegation team: receptionist routes to scoped member agents.

Complements agent/teams.py (sequential researcher->writer pipeline): this
package answers "who should handle it" per question instead of running every
role. Side effects stay human-approved (C1) — the team only surfaces
pending_action, never executes it.
"""
from .agents import HRAgent, ITAgent, KnowledgeAgent, RouterAgent, default_members, route_for_intent
from .base import AgentMessage, AgentRole, TeamAgent, TeamBus
from .orchestrator import TeamOrchestrator

__all__ = ["AgentMessage", "AgentRole", "HRAgent", "ITAgent", "KnowledgeAgent",
           "RouterAgent", "TeamAgent", "TeamBus", "TeamOrchestrator",
           "default_members", "route_for_intent"]
