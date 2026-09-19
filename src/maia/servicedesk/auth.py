"""Service Desk auth — reuses MAIA crypto/token primitives (plan T1).

Password hashing and JWT sign/verify are delegated to ``maia.auth`` so both
products share one audited crypto path while keeping separate stores.
Membership, role and groups are ALWAYS resolved from ``sd_memberships``
server-side; the JWT only carries the user id and active tenant.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from maia.auth import (
    create_access_token,
    create_refresh_token,
    get_password_hash,
    verify_password,
)
from maia.servicedesk.models import SDMembership, SDRole, SDUser
from maia.servicedesk.schemas import Principal

__all__ = [
    "AuthError",
    "TokenPair",
    "get_password_hash",
    "issue_tokens",
    "resolve_principal",
    "verify_password",
]


class AuthError(Exception):
    """Invalid/expired token or unknown user (API maps to 401)."""


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str


def resolve_principal(session: Session, claims: dict) -> Principal:
    """Build a Principal from verified JWT claims + server-side membership.

    The token only carries identity (``sub``) and the active tenant (``tid``).
    Role and group_ids are read from ``sd_memberships`` inside this call —
    a forged or stale role claim can never grant authority (Global
    Constraints; covered by T1 authorization tests).
    """
    sub = claims.get("sub")
    tid = claims.get("tid")
    if not sub or not tid:
        raise AuthError("token missing sub/tid claims")
    try:
        user_id = UUID(str(sub))
    except ValueError as exc:
        raise AuthError("invalid sub claim") from exc

    membership = (
        session.query(SDMembership)
        .filter(SDMembership.user_id == user_id, SDMembership.tenant_id == tid)
        .first()
    )
    if membership is None:
        raise AuthError("no membership for this tenant")
    user = session.get(SDUser, user_id)
    if user is None or not user.is_active:
        raise AuthError("user inactive or unknown")

    groups = (
        session.query(SDMembership.group_id)
        .filter(
            SDMembership.tenant_id == tid,
            SDMembership.user_id == user_id,
            SDMembership.group_id.is_not(None),
        )
        .all()
    )
    return Principal(
        user_id=user_id,
        tenant_id=tid,
        role=SDRole(membership.role).value,
        group_ids=frozenset(row[0] for row in groups),
    )


def issue_tokens(session: Session, user: SDUser, tenant_id: str) -> TokenPair:
    """Issue a short-lived access token + refresh token for one tenant.

    Authority lives in ``sd_memberships`` (read per request), never in the
    token payload beyond ``sub``/``tid``.
    """
    claims = {"sub": str(user.id), "tid": tenant_id}
    return TokenPair(
        access_token=create_access_token(data=claims),
        refresh_token=create_refresh_token(data=claims),
    )

