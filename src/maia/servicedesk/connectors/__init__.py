"""Service Desk connectors: typed adapters, error taxonomy, Jira reads.

Public names are re-exported here; implementations live in sibling modules
(``base``, ``jira``, and — from T7/T8 — ``webhooks``/``reconcile``).
"""
from __future__ import annotations

from maia.servicedesk.connectors.base import (
    JiraError,
    JiraErrorCategory,
    JiraReads,
)

__all__ = ["JiraError", "JiraErrorCategory", "JiraReads"]
