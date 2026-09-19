"""Append-only application audit (S4/T1).

Every state-changing operation writes one row here inside the same
transaction as the state change. Updates/deletes are not exposed by this
module by design; the DB table itself stays write-through-API only.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from maia.servicedesk.models import SDAuditEvent
from maia.servicedesk.schemas import Principal


def _hash(obj: Any) -> str | None:
    if obj is None:
        return None
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def record_audit(
    session: Session,
    *,
    tenant_id: str,
    operation: str,
    principal: Principal | None = None,
    entity_id: str | UUID | None = None,
    before: Any = None,
    after: Any = None,
    trace_id: str | None = None,
) -> SDAuditEvent:
    event = SDAuditEvent(
        tenant_id=tenant_id,
        actor_id=principal.user_id if principal else None,
        actor_role=principal.role if principal else None,
        entity_id=str(entity_id) if entity_id else None,
        operation=operation,
        before_hash=_hash(before),
        after_hash=_hash(after),
        trace_id=trace_id,
    )
    session.add(event)
    return event
