"""SQLAlchemy models for authentication and authorization."""
import uuid
from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class UserRole(PyEnum):
    ADMIN = "admin"
    USER = "user"


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(Enum(UserRole), nullable=False, default=UserRole.USER)
    tenant_id = Column(String(255), nullable=False, default="default")
    # Employee identity: links the login to HR records (leave balance, tickets).
    employee_id = Column(String(64), unique=True, index=True, nullable=True)
    full_name = Column(String(255), nullable=True)
    department = Column(String(255), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    email_verified = Column(Boolean, nullable=False, default=False)
    failed_login_attempts = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # Relationships
    sessions = relationship("UserSession", back_populates="user", cascade="all, delete-orphan")
    reset_tokens = relationship("PasswordResetToken", back_populates="user", cascade="all, delete-orphan")


class UserSession(Base):
    __tablename__ = "user_sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    refresh_token = Column(String(255), unique=True, index=True, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Relationships
    user = relationship("User", back_populates="sessions")


class PasswordResetToken(Base):
    """Single-use password-reset tokens (sha256 hash stored, never the raw)."""

    __tablename__ = "password_reset_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    token_hash = Column(String(64), unique=True, index=True, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Relationships
    user = relationship("User", back_populates="reset_tokens")


def ensure_auth_schema(engine) -> list[str]:
    """Additive schema repair for existing sqlite DBs (dev convenience).

    Base.metadata.create_all() creates missing TABLES but never adds COLUMNS
    to existing ones. This adds the columns/tables introduced after the first
    release (employee identity, reset tokens) via PRAGMA inspection + ALTER
    TABLE. Returns the list of applied alterations. Safe to run on every boot.
    """
    from sqlalchemy import inspect, text

    applied: list[str] = []
    try:
        insp = inspect(engine)
        if "users" in insp.get_table_names():
            existing = {c["name"] for c in insp.get_columns("users")}
            for col, ddl in (
                ("employee_id", "ALTER TABLE users ADD COLUMN employee_id VARCHAR(64)"),
                ("full_name", "ALTER TABLE users ADD COLUMN full_name VARCHAR(255)"),
                ("department", "ALTER TABLE users ADD COLUMN department VARCHAR(255)"),
            ):
                if col not in existing:
                    with engine.begin() as conn:
                        conn.execute(text(ddl))
                    applied.append(col)
        # New tables (reset tokens) are handled by create_all, but ensure here too.
        PasswordResetToken.__table__.create(bind=engine, checkfirst=True)
    except Exception:
        pass
    return applied