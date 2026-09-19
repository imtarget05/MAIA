"""SQLAlchemy models for authentication and authorization."""
import uuid
from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class UserRole(PyEnum):
    ADMIN = "admin"
    USER = "user"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), nullable=False, default=UserRole.USER)
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")
    # Employee identity: links the login to HR records (leave balance, tickets).
    employee_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True, nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    department: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    failed_login_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # Relationships
    sessions: Mapped[list["UserSession"]] = relationship(
        "UserSession", back_populates="user", cascade="all, delete-orphan")
    reset_tokens: Mapped[list["PasswordResetToken"]] = relationship(
        "PasswordResetToken", back_populates="user", cascade="all, delete-orphan")


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    refresh_token: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="sessions")


class PasswordResetToken(Base):
    """Single-use password-reset tokens (sha256 hash stored, never the raw)."""

    __tablename__ = "password_reset_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="reset_tokens")


def ensure_auth_schema(engine) -> list[str]:
    """Additive schema repair for existing sqlite DBs (dev convenience).

    Base.metadata.create_all() creates missing TABLES but never adds COLUMNS
    to existing ones. This adds the columns/tables introduced after the first
    release (employee identity, reset tokens) via PRAGMA inspection + ALTER
    TABLE. Returns the list of applied alterations. Safe to run on every boot.
    """
    from sqlalchemy import Table, inspect, text

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
        _reset_table = PasswordResetToken.__table__
        assert isinstance(_reset_table, Table)
        _reset_table.create(bind=engine, checkfirst=True)
    except Exception:
        pass
    return applied