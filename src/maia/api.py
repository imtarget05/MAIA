"""FastAPI application layer (§9)."""
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from maia.auth import (
    authenticate_user,
    consume_password_reset_token,
    create_password_reset_token,
    create_user_session,
    generate_employee_id,
    get_password_hash,
    get_user_by_email,
    get_user_by_id,
    record_failed_login,
    update_last_login,
    verify_refresh_token,
)
from maia.config import settings
from maia.ingestion_pipeline import (
    delete_source,
    ingest_data_dir,
    ingest_url,
    list_sources,
)
from maia.models import Base, User, UserSession, ensure_auth_schema
from maia.pipeline_query import build_stack, query

# JWT settings - use settings.jwt_secret_key property (raises in production if not set)
SECRET_KEY = settings.jwt_secret_key
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 15  # short-lived access token
REFRESH_TOKEN_EXPIRE_DAYS = 7     # longer-lived refresh token

# Database setup
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Use SQLite for simplicity; in production, use PostgreSQL or another database
SQLALCHEMY_DATABASE_URL = "sqlite:///./maia_auth.db"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Create tables + additive repair for DBs created by older releases
# (new tables via create_all; new columns via ensure_auth_schema).
Base.metadata.create_all(bind=engine)
ensure_auth_schema(engine)


def ensure_employee_identity(db: Session, user: User) -> User:
    """Backfill employee_id for accounts created before identity existed."""
    if not getattr(user, "employee_id", None):
        user.employee_id = generate_employee_id(db)
        db.commit()
        db.refresh(user)
    return user


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# FastAPI app — must be created before any @app.* route is defined
app = FastAPI(title="MAIA — Enterprise Employee Assistant", version="0.4.0")

# CORS (middleware was imported but never configured before)
# Default to [] (same-origin only) instead of ["*"] for security.
# Production MUST set CORS_ORIGINS explicitly.
cors_origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Startup validation: warn if CORS_ORIGINS not set in production
if not cors_origins and settings.ENVIRONMENT != "development":
    import warnings
    warnings.warn(
        "CORS_ORIGINS is not set — CORS defaults to same-origin only. "
        "Set CORS_ORIGINS in .env for production deployments.",
        RuntimeWarning,
        stacklevel=1,
    )


# ---------------------------------------------------------------- Pydantic models
# NOTE: these must be defined *before* the routers below reference them
# (previously they sat at the bottom of the file, causing NameError on import).


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class TokenRefresh(BaseModel):
    refresh_token: str


class UserLogin(BaseModel):
    email: str
    password: str


class UserRegister(BaseModel):
    email: str
    password: str
    role: str = "user"
    tenant_id: str = "default"
    full_name: str | None = None
    department: str | None = None


class RegisterReq(BaseModel):
    email: str
    password: str
    full_name: str | None = None
    department: str | None = None


class DecideReq(BaseModel):
    approved: bool = True


class UserResponse(BaseModel):
    id: str
    email: str
    role: str
    tenant_id: str
    employee_id: str | None = None
    full_name: str | None = None
    department: str | None = None
    is_active: bool
    email_verified: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True


def _public_user(u: User) -> dict:
    role = getattr(u.role, "value", u.role)
    return {"id": str(u.id), "email": u.email, "role": role,
            "tenant_id": u.tenant_id, "employee_id": u.employee_id,
            "full_name": u.full_name, "department": u.department,
            "is_active": u.is_active, "email_verified": u.email_verified,
            "created_at": u.created_at, "updated_at": u.updated_at}


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


class GoogleAuthRequest(BaseModel):
    """Body for /auth/google/callback — client sends the authorization code."""
    code: str
    redirect_uri: str | None = None


class QueryReq(BaseModel):
    question: str
    top_k: int | None = None
    tenant_id: str | None = None
    session_id: str | None = None


class ChatReq(BaseModel):
    question: str
    session_id: str | None = None
    employee_id: str | None = None
    top_k: int | None = None
    tenant_id: str | None = None


class ConfirmReq(BaseModel):
    session_id: str
    employee_id: str | None = None
    approved: bool = True
    idempotency_key: str | None = None


# Security dependencies
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    """Get the current authenticated user from a valid *access* token."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        # A refresh token must never be accepted in place of an access token.
        if payload.get("type") != "access":
            raise credentials_exception
        user_id: str | None = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = get_user_by_id(db, user_id=user_id)
    if user is None:
        raise credentials_exception
    return user


def get_current_active_user(current_user: User = Depends(get_current_user)) -> User:
    """Get the current active user."""
    if not current_user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")
    return current_user


def get_current_admin_user(current_user: User = Depends(get_current_active_user)) -> User:
    """Get the current admin user."""
    from maia.models import UserRole
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Not enough permissions")
    return current_user


# Authentication Router
from fastapi import APIRouter

auth_router = APIRouter(prefix="/auth", tags=["auth"])


@auth_router.post("/register", response_model=UserResponse, status_code=201)
def register(req: RegisterReq, db: Session = Depends(get_db)):
    """Public sign-up. Role is always 'user' (admins are promoted by an admin).

    The very first account becomes admin when BOOTSTRAP_FIRST_ADMIN is on,
    so a fresh deployment can be administered without manual DB edits.
    """
    from maia.models import UserRole
    email = (req.email or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(status_code=400, detail="Invalid email address")
    if len(req.password or "") < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    if get_user_by_email(db, email):
        raise HTTPException(status_code=400, detail="Email already registered")
    role = UserRole.USER
    if settings.BOOTSTRAP_FIRST_ADMIN and db.query(User).count() == 0:
        role = UserRole.ADMIN
    user = User(email=email, password_hash=get_password_hash(req.password),
                role=role, tenant_id=settings.TENANT_ID,
                employee_id=generate_employee_id(db),
                full_name=(req.full_name or "").strip() or None,
                department=(req.department or "").strip() or None)
    db.add(user)
    db.commit()
    db.refresh(user)
    return _public_user(user)


@auth_router.post("/login", response_model=Token)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    """Login with email and password."""
    user = authenticate_user(db, form_data.username, form_data.password)
    if not user:
        record_failed_login(db, form_data.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Check if account is locked
    if user.locked_until and user.locked_until > datetime.utcnow():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Account is locked due to too many failed login attempts"
        )
    
    # Check if user is active
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user"
        )
    
    # Reset failed login attempts on successful login
    update_last_login(db, str(user.id))
    ensure_employee_identity(db, user)
    
    # Create tokens
    access_token, refresh_token = create_user_session(db, str(user.id))
    
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer"
    }


@auth_router.post("/refresh", response_model=Token)
def refresh_token(token_request: TokenRefresh, db: Session = Depends(get_db)):
    """Refresh access token using refresh token."""
    session = verify_refresh_token(db, token_request.refresh_token)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Create new access token
    user = get_user_by_id(db, str(session.user_id))
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    access_token, new_refresh_token = create_user_session(db, str(user.id))
    
    # Delete old refresh token
    db.delete(session)
    db.commit()
    
    return {
        "access_token": access_token,
        "refresh_token": new_refresh_token,
        "token_type": "bearer"
    }


@auth_router.post("/logout")
def logout(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    """Logout user by invalidating refresh token."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if user_id:
            # Delete all sessions for this user (simple logout approach)
            from maia.auth import user_uuid
            uid = user_uuid(user_id)
            if uid is not None:
                db.query(UserSession).filter(UserSession.user_id == uid).delete()
                db.commit()
    except JWTError:
        pass  # Token is invalid, but we still want to logout
    
    return {"message": "Successfully logged out"}


@auth_router.post("/forgot-password")
def forgot_password(request: ForgotPasswordRequest, db: Session = Depends(get_db)):
    """Request password reset: email a single-use link (or queue to outbox)."""
    from maia import notifier as _nt
    user = get_user_by_email(db, (request.email or "").strip().lower())
    if user and user.is_active:
        raw = create_password_reset_token(db, user)
        link = f"{settings.APP_BASE_URL}?reset_token={raw}"
        try:
            _nt.send_password_reset_email(user.email, link)
        except Exception:
            pass
    # Always return the same message to prevent email enumeration
    return {"message": "If the email exists, a reset link has been sent"}


@auth_router.post("/reset-password")
def reset_password(request: ResetPasswordRequest, db: Session = Depends(get_db)):
    """Reset password using a single-use token from /forgot-password."""
    if len(request.new_password or "") < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    user = consume_password_reset_token(db, request.token or "")
    if not user:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")
    user.password_hash = get_password_hash(request.new_password)
    user.failed_login_attempts = 0
    user.locked_until = None
    db.commit()
    return {"message": "Password has been reset"}


@auth_router.get("/google/login")
def google_login():
    """Return the Google OAuth consent-screen URL."""
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        raise HTTPException(status_code=503, detail="Google Sign-In is not configured")
    from urllib.parse import urlencode
    base = "https://accounts.google.com/o/oauth2/v2/auth"
    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": f"{settings.APP_BASE_URL}/auth/google/callback",
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "consent",
    }
    return {"url": f"{base}?{urlencode(params)}"}


@auth_router.post("/google/callback")
def google_callback(request: GoogleAuthRequest, db: Session = Depends(get_db)):
    """Exchange Google auth code for a JWT session."""
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        raise HTTPException(status_code=503, detail="Google Sign-In is not configured")
    import requests as http
    # 1) Exchange code -> tokens
    token_res = http.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": request.code,
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "redirect_uri": f"{settings.APP_BASE_URL}/auth/google/callback",
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if token_res.status_code != 200:
        raise HTTPException(status_code=400, detail="Google code exchange failed")
    id_token = token_res.json().get("id_token")
    # 2) Verify id_token -> user info
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token
    try:
        info = google_id_token.verify_oauth2_token(
            id_token, google_requests.Request(), settings.GOOGLE_CLIENT_ID)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid Google token")
    email = (info.get("email") or "").lower()
    if not email:
        raise HTTPException(status_code=400, detail="Google account has no email")
    full_name = info.get("name") or email.split("@")[0]
    # 3) Find or create user
    from maia.models import UserRole
    user = get_user_by_email(db, email)
    if not user:
        user = User(
            email=email,
            password_hash="",
            role=UserRole.USER,
            tenant_id=settings.TENANT_ID,
            employee_id=generate_employee_id(db),
            full_name=full_name,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        user.full_name = full_name or user.full_name
        db.commit()
    ensure_employee_identity(db, user)
    update_last_login(db, str(user.id))
    access_token, refresh_token = create_user_session(db, str(user.id))
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": _public_user(user),
    }


@auth_router.get("/me", response_model=UserResponse)
def get_current_user_info(current_user: User = Depends(get_current_active_user),
                          db: Session = Depends(get_db)):
    """Get current user information."""
    ensure_employee_identity(db, current_user)
    return _public_user(current_user)


# Admin Router
admin_router = APIRouter(prefix="/admin", tags=["admin"])


@admin_router.get("/users", response_model=list[UserResponse])
def list_users(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """List all users (admin only)."""
    users = db.query(User).offset(skip).limit(limit).all()
    return users


@admin_router.post("/users", response_model=UserResponse)
def create_user(
    user: UserRegister,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Create a new user (admin only)."""
    from maia.models import UserRole
    # Check if user already exists
    db_user = get_user_by_email(db, (user.email or "").strip().lower())
    if db_user:
        raise HTTPException(
            status_code=400,
            detail="Email already registered"
        )
    try:
        role = UserRole(user.role) if isinstance(user.role, str) else user.role
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid role (admin|user)")
    if len(user.password or "") < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    # Create new user
    hashed_password = get_password_hash(user.password)
    db_user = User(
        email=user.email.strip().lower(),
        password_hash=hashed_password,
        role=role,
        tenant_id=user.tenant_id or settings.TENANT_ID,
        employee_id=generate_employee_id(db),
        full_name=(user.full_name or "").strip() or None,
        department=(user.department or "").strip() or None,
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return _public_user(db_user)


@admin_router.get("/users/{user_id}", response_model=UserResponse)
def get_user(
    user_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Get user by ID (admin only)."""
    db_user = get_user_by_id(db, user_id)
    if db_user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return db_user


@admin_router.put("/users/{user_id}", response_model=UserResponse)
def update_user(
    user_id: str,
    user_update: UserRegister,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Update user (admin only)."""
    db_user = get_user_by_id(db, user_id)
    if db_user is None:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Update fields
    from maia.models import UserRole
    if user_update.email:
        db_user.email = user_update.email.strip().lower()
    try:
        db_user.role = UserRole(user_update.role) if isinstance(user_update.role, str) else user_update.role
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid role (admin|user)")
    db_user.tenant_id = user_update.tenant_id or db_user.tenant_id
    if user_update.full_name is not None:
        db_user.full_name = (user_update.full_name or "").strip() or None
    if user_update.department is not None:
        db_user.department = (user_update.department or "").strip() or None
    if user_update.password:  # Only update password if provided
        if len(user_update.password) < 6:
            raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
        db_user.password_hash = get_password_hash(user_update.password)

    db.commit()
    db.refresh(db_user)
    return _public_user(db_user)


class ActiveToggle(BaseModel):
    is_active: bool = True


@admin_router.post("/users/{user_id}/active", response_model=UserResponse)
def set_user_active(
    user_id: str,
    req: ActiveToggle,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Lock/unlock an account (admin only)."""
    db_user = get_user_by_id(db, user_id)
    if db_user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if str(db_user.id) == str(current_user.id) and not req.is_active:
        raise HTTPException(status_code=400, detail="Cannot lock your own admin account")
    db_user.is_active = req.is_active
    db.commit()
    db.refresh(db_user)
    return _public_user(db_user)


@admin_router.delete("/users/{user_id}")
def delete_user(    user_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Delete user (admin only)."""
    db_user = get_user_by_id(db, user_id)
    if db_user is None:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Instead of deleting, deactivate the user
    db_user.is_active = False
    db.commit()
    return {"message": "User deactivated successfully"}


@admin_router.get("/documents")
def list_all_documents(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """List indexed documents across tenants (admin only)."""
    try:
        _, store, _, _, _ = build_stack()
        corpus = store.scroll_all()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"store_unreachable: {e}")
    docs: dict[str, dict] = {}
    for c in corpus:
        meta = c.get("metadata", {})
        key = meta.get("doc_id", meta.get("document_id", meta.get("filename", "?")))
        d = docs.setdefault(key, {"doc_id": key, "filename": meta.get("filename", ""),
                                  "chunks": 0, "tenant_ids": set()})
        d["chunks"] += 1
        if meta.get("tenant_id"):
            d["tenant_ids"].add(meta["tenant_id"])
    items = [{"doc_id": k, **{kk: (sorted(vv) if isinstance(vv, set) else vv)
                              for kk, vv in v.items() if kk != "doc_id"}}
             for k, v in docs.items()]
    return {"total_docs": len(items), "documents": items[skip:skip + limit]}


@admin_router.delete("/documents/{doc_id}")
def delete_document_admin(
    doc_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Delete a document (admin only)."""
    # This would need to be implemented based on your document storage
    # For now, returning a placeholder
    return {"message": f"Document {doc_id} deletion would be implemented here"}


@admin_router.get("/stats")
def get_admin_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """System statistics (admin only)."""
    from maia import workflow as _wf
    from maia.models import UserRole
    user_count = db.query(User).count()
    active_user_count = db.query(User).filter(User.is_active == True).count()
    admin_count = db.query(User).filter(User.role == UserRole.ADMIN).count()
    try:
        _, store, _, _, llm = build_stack()
        kb_points = store.count()
        llm_mode = llm.mode
    except Exception:
        kb_points, llm_mode = -1, "unknown"
    return {
        "total_users": user_count,
        "active_users": active_user_count,
        "admin_users": admin_count,
        "kb_points": kb_points,
        "llm_mode": llm_mode,
        "workflow": _wf.counts(),
    }


# ---- Approval workflow (admin only) ------------------------------------
@admin_router.get("/requests")
def admin_list_requests(status: str = "", type: str = "",
                        db: Session = Depends(get_db),
                        current_user: User = Depends(get_current_admin_user)):
    """List approval workflow requests (admin only). Tenant-scoped unless admin."""
    from maia import workflow as _wf
    return {"requests": _wf.list_requests(status=status, type=type, limit=200)}


@admin_router.post("/requests/{request_id}/decide")
def admin_decide_request(request_id: int, req: DecideReq,
                         db: Session = Depends(get_db),
                         current_user: User = Depends(get_current_admin_user)):
    """Approve/reject a workflow request (admin only).

    If the pending session lives in this process, the side-effect executes
    (or is cancelled) via the normal confirm path. Otherwise the decision is
    recorded + notified without double-executing.
    """
    from maia import notifier as _nt
    from maia import workflow as _wf
    from maia.agent import hris as hris_conn
    from maia.agent.session import session_store
    row = _wf.get_request(request_id)
    if not row:
        raise HTTPException(status_code=404, detail="Request not found")
    if row["status"] != "pending":
        return {"ok": False, "error": f"already_{row['status']}", "request": row}
    pending = session_store.get_pending(row.get("session_id", ""), tenant_id=current_user.tenant_id) if row.get("session_id") else None
    side_effect_result = None
    if pending and pending.get("tool") == row.get("tool"):
        from maia.agent.agent import EnterpriseAgent
        out = EnterpriseAgent(tenant_id=current_user.tenant_id).confirm_action(
            row["session_id"], employee_id=current_user.employee_id or "admin",
            approved=req.approved, is_admin=True)
        return {"ok": True, "executed": True, "result": out,
                "request": _wf.get_request(request_id)}
    tool = row.get("tool", "")
    emp = row.get("employee_id") or current_user.employee_id or "admin"
    params = row.get("params", {}) if isinstance(row.get("params"), dict) else {}
    if tool == "create_leave_request" and req.approved:
        side_effect_result = hris_conn.create_leave_request(
            employee_id=emp,
            days=int(params.get("days", 1)),
            start_date=params.get("start_date"),
            tenant_id=current_user.tenant_id,
        )
    elif tool == "create_it_ticket" and req.approved:
        side_effect_result = hris_conn.create_it_ticket(
            employee_id=emp,
            ticket_type=params.get("ticket_type", "general"),
            description=params.get("description", ""),
            tenant_id=current_user.tenant_id,
        )
    decided = _wf.decide(request_id, req.approved,
                         decided_by=current_user.email or "admin",
                         result_ref=(side_effect_result or {}).get("request_id") or (side_effect_result or {}).get("ticket_id") or "",
                         result=side_effect_result)
    try:
        _nt.notify_request_decided(row.get("type", ""), row.get("summary", ""),
                                   row.get("requester_email", ""), req.approved,
                                   current_user.email or "admin")
    except Exception:
        pass
    return {"ok": True, "executed": bool(side_effect_result),
            "hint": "side-effect executed in admin process" if side_effect_result else "pending session not in this process; decision recorded + notified",
            "request": decided}


@admin_router.get("/activity")
def admin_activity(limit: int = 50,
                   db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_admin_user)):
    """Recent workflow/approval activity feed (admin only)."""
    from maia import workflow as _wf
    return {"events": _wf.recent_events(limit=min(limit, 200))}


@admin_router.get("/outbox")
def admin_outbox(limit: int = 100,
                 db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_admin_user)):
    """Queued/sent emails (admin only). SMTP unsent mails land here."""
    from maia import notifier as _nt
    return {"emails": _nt.read_outbox(limit=min(limit, 200)),
            "smtp_configured": bool(settings.SMTP_HOST)}


# Routers are included at the end so every endpoint above is registered.
app.include_router(auth_router)
app.include_router(admin_router)


@app.get("/health")
def health():
    """Lightweight liveness probe (no auth, no ML stack).

    MUST stay cheap: Render free tier (512Mi) OOM-crashed when /health
    loaded the FastEmbed model per request. Full dependency state lives
    on GET /ready.
    """
    return {"status": "ok", "version": app.version}


@app.get("/ready")
def ready():
    """Readiness probe WITHOUT loading the embedding model.

    MUST stay cheap: even /ready OOM-crashed Render free tier (512Mi) when
    it called build_stack(), because every Embedder() loads the FastEmbed
    ONNX model (~hundreds of MB). The model now lives in a process-wide
    singleton (embeddings.get_embedder) loaded once; this probe only checks
    Qdrant connectivity + reports llm/rerank modes (both cheap, no model).
    """
    try:
        from maia.llm import CloudflareLLM
        from maia.reranker import Reranker
        from maia.vector_store import QdrantStore

        store = QdrantStore(url=settings.QDRANT_URL, collection=settings.QDRANT_COLLECTION,
                            dim=settings.EMBED_DIM, api_key=settings.QDRANT_API_KEY)
        llm = CloudflareLLM(settings.CLOUDFLARE_ACCOUNT_ID, settings.CLOUDFLARE_API_TOKEN,
                             settings.CLOUDFLARE_MODEL)
        reranker = Reranker()
        return {"status": "ok", "qdrant_points": store.count(),
                "collection": settings.QDRANT_COLLECTION,
                "llm_mode": llm.mode, "rerank_mode": reranker.mode,
                "embed_model": settings.EMBED_MODEL}
    except Exception as e:
        return {"status": "degraded", "error": f"{type(e).__name__}: {e}"}


@app.post("/ingest")
def ingest(tenant_id: str | None = None, current_user: User = Depends(get_current_active_user)):
    return ingest_data_dir(tenant_id=tenant_id)


@app.post("/ingest/enterprise")
def ingest_enterprise(tenant_id: str | None = None, current_user: User = Depends(get_current_active_user)):
    """Ingest enterprise docs (data/enterprise) into Qdrant."""
    return ingest_data_dir(settings.ENTERPRISE_DATA_DIR, tenant_id=tenant_id)


@app.post("/ingest/upload")
async def ingest_upload(files: list[UploadFile] = File(...), current_user: User = Depends(get_current_active_user)):
    dest = Path(settings.DATA_DIR)
    dest.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in files:
        # Sanitize filename: strip directories and reject traversal attempts
        safe_name = Path(f.filename or "upload.bin").name
        if not safe_name or safe_name in {".", ".."} or ".." in safe_name:
            raise HTTPException(status_code=400, detail=f"Invalid filename: {f.filename!r}")
        target = (dest / safe_name).resolve()
        if not str(target).startswith(str(dest.resolve())):
            raise HTTPException(status_code=400, detail=f"Invalid filename: {f.filename!r}")
        target.write_bytes(await f.read())
        saved.append(safe_name)
    result = ingest_data_dir(str(dest))
    result["saved"] = saved
    return result


@app.post("/query")
def do_query(req: QueryReq, current_user: User = Depends(get_current_active_user)):
    # MAIA-02: tenant is taken from the authenticated user, NEVER from the
    # request body — a caller-chosen tenant_id would allow cross-tenant reads.
    return query(req.question, top_k_final=req.top_k,
                 tenant_id=current_user.tenant_id,
                 session_id=req.session_id)


class UrlIngestReq(BaseModel):
    url: str
    session_id: str = ""
    # NOTE: tenant_id intentionally NOT accepted from the client (MAIA-02).


@app.post("/ingest/url")
def ingest_url_endpoint(req: UrlIngestReq, current_user: User = Depends(get_current_active_user)):
    """Notebook-style: fetch one page/PDF into this session's sources."""
    try:
        return {"ok": True, **ingest_url(req.url,
                                         tenant_id=current_user.tenant_id,
                                         session_id=req.session_id or "")}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


@app.get("/sources")
def sources(session_id: str = "", current_user: User = Depends(get_current_active_user)):
    """List URL sources added to a session (newest first). Tenant from auth."""
    return {"session_id": session_id,
            "sources": list_sources(session_id, current_user.tenant_id)}


@app.delete("/sources/{doc_id}")
def source_delete(doc_id: str, session_id: str = "", current_user: User = Depends(get_current_active_user)):
    """Delete one session URL source (ownership verified). Tenant from auth."""
    return delete_source(doc_id, session_id, current_user.tenant_id)


# ---- Enterprise Agent (Receptionist - Agentic RAG) ----
@app.post("/chat")
def chat(req: ChatReq, current_user: User = Depends(get_current_active_user)):
    """Agentic RAG: Decide → Memory(rewrite) → Iterative Retrieve → Evidence → Tool → Grounding."""
    from maia.agent.agent import EnterpriseAgent
    # Use authenticated user's tenant_id and employee_id for security
    agent = EnterpriseAgent(tenant_id=current_user.tenant_id)
    return agent.chat(req.question, session_id=req.session_id or "default", employee_id=current_user.employee_id, top_k_final=req.top_k, tenant_id=current_user.tenant_id,
                      requester_email=current_user.email)


@app.post("/chat/stream")
def chat_stream(req: ChatReq, current_user: User = Depends(get_current_active_user)):
    """SSE streaming for chat (splits answer into chunks)."""
    from fastapi.responses import StreamingResponse

    from maia.agent.agent import EnterpriseAgent

    agent = EnterpriseAgent(tenant_id=current_user.tenant_id)

    def gen():
        for chunk in agent.stream_answer(req.question, session_id=req.session_id or "default", employee_id=current_user.employee_id, tenant_id=current_user.tenant_id, requester_email=current_user.email):
            yield f"data: {chunk}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/chat/history/{session_id}")
def chat_history(session_id: str, current_user: User = Depends(get_current_active_user)):
    from maia.agent.session import session_store
    return {"session_id": session_id, "history": session_store.history(session_id, tenant_id=current_user.tenant_id)}


@app.delete("/chat/history/{session_id}")
def chat_history_clear(session_id: str, current_user: User = Depends(get_current_active_user)):
    from maia.agent.session import session_store
    session_store.clear(session_id, tenant_id=current_user.tenant_id)
    return {"cleared": session_id}


@app.post("/actions/confirm")
def action_confirm(req: ConfirmReq, current_user: User = Depends(get_current_active_user)):
    """Execute (approved=true) or cancel a pending side-effect action (C1)."""
    from maia.agent.agent import EnterpriseAgent
    # Use authenticated user's tenant_id and employee_id for security (P1-1)
    agent = EnterpriseAgent(tenant_id=current_user.tenant_id)
    return agent.confirm_action(req.session_id, employee_id=current_user.employee_id,
                                 approved=req.approved, idempotency_key=req.idempotency_key)


@app.get("/actions/pending/{session_id}")
def action_pending(session_id: str, current_user: User = Depends(get_current_active_user)):
    """Inspect the pending action awaiting approval in a session."""
    from maia.agent.session import session_store
    pending = session_store.get_pending(session_id, tenant_id=current_user.tenant_id)
    return {"session_id": session_id, "pending": pending}


# ---- LangGraph control-plane agent (/agent/chat) --------------------------
# Mirrors the legacy ``POST /chat`` (EnterpriseAgent) but runs the compiled
# LangGraph ``graph`` so the same request can stream node events, pause for
# human approval (HITL interrupt), and resume across requests (SqliteSaver).

class AgentChatReq(BaseModel):
    question: str
    session_id: str = "default"
    stream: bool = False
    # When present, RESUMES an interrupted run (human approved/rejected).
    resume: dict | None = None


def _state_to_response(state: dict) -> dict:
    """Map a LangGraph AgentState dict to the legacy chat() response shape."""
    action_result = state.get("action_result") or {}
    has_result = bool(action_result.get("result"))
    return {
        "answer": state.get("answer", ""),
        "intent": state.get("intent", "general"),
        "status": state.get("status", "answered"),
        "citations": state.get("citations", []),
        "has_evidence": state.get("has_evidence", False),
        "grounding_score": state.get("grounding_score", 0.0),
        "cites_valid": state.get("cites_valid", True),
        "slots": state.get("slots", {}),
        "pending_action": state.get("pending_action"),
        "action_result": action_result if has_result else None,
        "action": (
            {"type": action_result.get("type"), "result": action_result.get("result")}
            if has_result else None
        ),
        "needs_approval": state.get("approval_needed", False),
        "node_trace": state.get("node_trace", []),
    }


def _agent_chat_stream(g, req, current_user, config: RunnableConfig):
    """SSE stream of LangGraph node events for the agent run.

    Uses the stable ``graph.stream(stream_mode="updates")`` API — NOT the
    experimental ``stream_events`` v3 protocol.  ``stream()`` is the
    production-hardened LangGraph API: it yields ``(node_name, partial_state)``
    tuples after each node completes, which we map to ``event: node`` frames.

    Streaming is an ephemeral live connection, so it deliberately uses the
    module-level in-memory graph (MemorySaver) rather than the durable
    SqliteSaver one — SqliteSaver is sync-only, so async-capable streaming
    stays on the MemorySaver path while HITL interrupt/resume stays on the
    durable ``g`` path passed by the caller.  Splitting is intentional: a
    stream cannot be resumed across requests, and an approval pause is
    blocking (no stream to keep alive) anyway.
    """
    import json as _json

    from fastapi.responses import StreamingResponse

    from maia.agent.langgraph_agent import AgentState
    from maia.agent.langgraph_agent import graph as mem_graph

    init_state = AgentState(
        question=req.question,
        session_id=req.session_id,
        tenant_id=current_user.tenant_id,
        employee_id=current_user.employee_id,
        requester_email=current_user.email,
    )

    def event_stream():
        try:
            # stream_mode="updates" → stable API, yields {node_name: partial_state}
            for update in mem_graph.stream(
                init_state, config=config, stream_mode="updates"
            ):
                for node_name, partial in update.items():
                    yield f"event: node\ndata: {_json.dumps({'node': node_name, 'keys': list(partial.keys())}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"event: error\ndata: {_json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
            return
        try:
            snap = mem_graph.get_state(config)
            final = _state_to_response(dict(snap.values))
            interrupts = getattr(snap, "interrupts", None)
            if interrupts:
                final["status"] = "needs_approval"
                final["pending_action"] = interrupts[0].value if interrupts else None
                final["needs_approval"] = True
        except Exception:
            final = {"status": "error", "answer": "Could not read final state"}
        yield f"event: done\ndata: {_json.dumps(final, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/agent/chat")
def agent_chat(req: AgentChatReq, current_user: User = Depends(get_current_active_user)):
    """LangGraph-backed agentic RAG.

    - Default: returns the final JSON response (same shape as ``POST /chat``).
    - ``stream=true``: SSE stream of graph node events.
    - When the graph pauses for human approval, returns ``status=needs_approval``
      with a ``pending_action`` card; resubmit with ``{"resume": {"approved": true/false}}``
      to continue.
    """
    from langgraph.types import Command

    from maia.agent.langgraph_agent import AgentState, get_durable_graph

    g = get_durable_graph()
    thread_id = f"{current_user.tenant_id}:{req.session_id}"
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}

    if req.resume:
        result = g.invoke(Command(resume=req.resume), config)
        return _state_to_response(result)

    if req.stream:
        return _agent_chat_stream(g, req, current_user, config)

    init_state = AgentState(
        question=req.question,
        session_id=req.session_id,
        tenant_id=current_user.tenant_id,
        employee_id=current_user.employee_id,
        requester_email=current_user.email,
    )
    result = g.invoke(init_state, config)

    interrupts = result.get("__interrupt__")
    if interrupts:
        proposal = interrupts[0].value if interrupts else None
        resp = _state_to_response(result)
        resp["status"] = "needs_approval"
        resp["pending_action"] = proposal
        resp["needs_approval"] = True
        return resp

    return _state_to_response(result)


@app.post("/tools/leave/request")
def leave_request(days: int = 1, start_date: str | None = None, current_user: User = Depends(get_current_active_user)):
    from maia.agent.tools import create_leave_request
    # Use authenticated user's employee_id
    return create_leave_request(current_user.employee_id or settings.DEFAULT_EMPLOYEE_ID,
                                days, start_date, tenant_id=current_user.tenant_id)


@app.post("/tools/it/ticket")
def it_ticket(ticket_type: str = "general", description: str = "", current_user: User = Depends(get_current_active_user)):
    from maia.agent.tools import create_it_ticket
    # Use authenticated user's employee_id
    return create_it_ticket(current_user.employee_id or settings.DEFAULT_EMPLOYEE_ID,
                            ticket_type, description, tenant_id=current_user.tenant_id)


# ---- Phase 2-4: memory / teams / connectors / voice / finetune ---------
class MemoryStoreReq(BaseModel):
    model_config = ConfigDict(extra="ignore")  # tenant comes from auth, not body
    user_id: str = "emp_001"
    type: str = "preference"
    content: str = ""


@app.post("/memory/store")
def memory_store(req: MemoryStoreReq, current_user: User = Depends(get_current_active_user)):
    """Persist a user preference/fact (long-term memory)."""
    from maia.agent.memory import LongTermMemory
    if not settings.LTM_ENABLED:
        return {"ok": False, "error": "ltm_disabled",
                "hint": "Set LTM_ENABLED=true to persist cross-session memory."}
    # Use authenticated user's ID and tenant_id
    mem_id = LongTermMemory().store(current_user.employee_id or settings.DEFAULT_EMPLOYEE_ID,
                                    current_user.tenant_id,
                                    req.type, req.content)
    return {"ok": mem_id is not None, "id": mem_id}


@app.get("/memory/recall")
def memory_recall(current_user: User = Depends(get_current_active_user), q: str = "", k: int = 5):
    """Recall top-k long-term memories by token overlap (offline)."""
    from maia.agent.memory import LongTermMemory
    if not settings.LTM_ENABLED:
        return {"ok": False, "error": "ltm_disabled"}
    # Use authenticated user's ID and tenant_id
    hits = LongTermMemory().recall(current_user.employee_id or settings.DEFAULT_EMPLOYEE_ID,
                                   current_user.tenant_id, q, k=k)
    return {"ok": True, "memories": hits}


@app.delete("/memory/{memory_id}")
def memory_forget(memory_id: int, current_user: User = Depends(get_current_active_user)):
    from maia.agent.memory import LongTermMemory
    if not settings.LTM_ENABLED:
        return {"ok": False, "error": "ltm_disabled"}
    # Use authenticated user's ID and tenant_id
    ok = LongTermMemory().forget(current_user.employee_id or settings.DEFAULT_EMPLOYEE_ID,
                                 current_user.tenant_id, memory_id)
    return {"ok": ok, "id": memory_id}


# ---- Archived: teams / mcp / voice / finetune live in _archive/ --------
# (Simplification Step 1: out of spec scope; see _archive/README.md)


@app.get("/collections/count")
def count(current_user: User = Depends(get_current_active_user)):
    _, store, _, _, _ = build_stack()
    return {"collection": settings.QDRANT_COLLECTION, "points": store.count()}


