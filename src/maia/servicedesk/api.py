"""Service Desk public API (T1 scope: auth, me, tickets, health).

Assembly rules (Global Constraints): this is the ONLY public entry point of
the new deployment (``maia.servicedesk.api:app``). Legacy MAIA tool/chat/
action routes are NOT mounted here. Authority is resolved per request from
the database; client-supplied tenant/role/group data is never trusted.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from maia.auth import decode_token, verify_password
from maia.servicedesk.auth import AuthError, issue_tokens, resolve_principal
from maia.servicedesk.db import make_engine, make_session_factory
from maia.servicedesk.models import SDMembership, SDTicket, SDUser
from maia.servicedesk.policies import can_read_ticket
from maia.servicedesk.settings import Settings

bearer_scheme = HTTPBearer(auto_error=False)


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    password: str
    tenant_id: str


def get_session(request: Request):
    session = request.app.state.session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: Session = Depends(get_session),
):
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    try:
        claims = decode_token(credentials.credentials)
        return resolve_principal(session, claims)
    except (AuthError, Exception) as exc:  # JWT errors included
        if isinstance(exc, AuthError):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token") from exc


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="MAIA Service Desk", version="0.1.0")
    engine = make_engine(settings.database_url)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)

    @app.get("/health/live")
    def health_live() -> dict:
        return {"status": "live"}

    @app.get("/health/ready")
    def health_ready() -> dict:
        problems = settings.readiness_errors()
        db_ok = True
        try:
            with app.state.engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1")
        except Exception:
            db_ok = False
        ready = db_ok and not problems
        return {
            "status": "ready" if ready else "not_ready",
            "database": db_ok,
            "problems": problems,
        }

    @app.post("/api/v1/auth/login")
    def login(body: LoginInput, session: Session = Depends(get_session)) -> dict:
        user = session.query(SDUser).filter(SDUser.email == body.email).first()
        if user is None or not user.is_active or not verify_password(
            body.password, user.password_hash
        ):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
        membership = (
            session.query(SDMembership)
            .filter(
                SDMembership.user_id == user.id,
                SDMembership.tenant_id == body.tenant_id,
            )
            .first()
        )
        if membership is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
        pair = issue_tokens(session, user, body.tenant_id)
        return {
            "access_token": pair.access_token,
            "refresh_token": pair.refresh_token,
            "token_type": "bearer",
        }

    @app.get("/api/v1/me")
    def me(principal=Depends(get_principal), session: Session = Depends(get_session)):
        user = session.get(SDUser, principal.user_id)
        return {
            "user_id": str(principal.user_id),
            "email": user.email if user else None,
            "tenant_id": principal.tenant_id,
            "role": principal.role,
            "group_ids": sorted(principal.group_ids),
        }

    @app.get("/api/v1/tickets/{ticket_id}")
    def get_ticket(
        ticket_id: UUID,
        principal=Depends(get_principal),
        session: Session = Depends(get_session),
    ) -> dict:
        ticket = session.get(SDTicket, ticket_id)
        if ticket is None or not can_read_ticket(principal, ticket):
            # Out-of-scope reads are indistinguishable from missing rows (S8).
            raise HTTPException(status.HTTP_404_NOT_FOUND, "ticket not found")
        return {
            "id": str(ticket.id),
            "tenant_id": ticket.tenant_id,
            "external_key": ticket.external_key,
            "summary": ticket.summary,
            "description": ticket.description,
            "support_group": ticket.support_group,
            "remote_status": ticket.remote_status,
            "version": ticket.version,
        }

    return app


# Run with: uvicorn maia.servicedesk.api:create_app --factory
# No module-level app instance: import must never require DB credentials.
