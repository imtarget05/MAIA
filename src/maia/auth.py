"""Authentication utilities: password hashing, JWT tokens, Google OAuth."""
from datetime import datetime, timedelta

from jose import (  # python-jose (matches requirements.txt)
    ExpiredSignatureError,
    JWTError,
    jwt,
)
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from maia.config import settings
from maia.models import PasswordResetToken, User, UserSession

# Password hashing
# pbkdf2_sha256 is the default (stdlib hashlib, no 72-byte limit, no binary
# wheel issues); bcrypt hashes from older releases still verify.
pwd_context = CryptContext(schemes=["pbkdf2_sha256", "bcrypt"], deprecated="auto")

# JWT settings - use settings.jwt_secret_key property (raises in production if not set)
SECRET_KEY = settings.jwt_secret_key
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 15  # short-lived access token
REFRESH_TOKEN_EXPIRE_DAYS = 7     # longer-lived refresh token


class TokenExpiredError(Exception):
    """Access/refresh token past its exp (mapped to 401 by callers)."""


class InvalidTokenError(Exception):
    """Token fails signature/shape validation (mapped to 401 by callers)."""


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain password against its hash."""
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    """Hash a password."""
    return pwd_context.hash(password)


def authenticate_user(db: Session, email: str, password: str) -> User | None:
    """Authenticate a user by email and password."""
    user = db.query(User).filter(User.email == email).first()
    if not user:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """Create a JWT access token."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire, "type": "access"})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def create_refresh_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """Create a JWT refresh token."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire, "type": "refresh"})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def decode_token(token: str) -> dict:
    """Decode and validate a JWT token."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except ExpiredSignatureError:
        raise TokenExpiredError("Token has expired")
    except JWTError:
        raise InvalidTokenError("Invalid token")


def create_user_session(db: Session, user_id: str) -> tuple[str, str]:
    """Create a new user session with access and refresh tokens."""
    uid = _coerce_uuid(user_id)
    # Generate tokens
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": str(uid)}, expires_delta=access_token_expires
    )
    refresh_token = create_refresh_token(data={"sub": str(uid)})

    # Store refresh token in database
    db_session = UserSession(
        user_id=uid,
        refresh_token=refresh_token,
        expires_at=datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
    )
    db.add(db_session)
    db.commit()
    db.refresh(db_session)

    return access_token, refresh_token


def verify_refresh_token(db: Session, refresh_token: str) -> UserSession | None:
    """Verify a refresh token and return the associated session."""
    try:
        payload = decode_token(refresh_token)
        if payload.get("type") != "refresh":
            return None

        user_id = _coerce_uuid(payload.get("sub"))
        if not user_id:
            return None

        session = (
            db.query(UserSession)
            .filter(
                UserSession.user_id == user_id,
                UserSession.refresh_token == refresh_token,
                UserSession.expires_at > datetime.utcnow(),
            )
            .first()
        )
        return session
    except Exception:
        return None


def get_user_by_email(db: Session, email: str) -> User | None:
    """Get a user by email."""
    return db.query(User).filter(User.email == email).first()


def _coerce_uuid(value):
    """postgresql UUID columns need uuid.UUID objects, not strings."""
    import uuid as _uuid
    if value is None or isinstance(value, _uuid.UUID):
        return value
    try:
        return _uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return None


def user_uuid(value):
    """Public coercion helper for route layers (logout filters, etc.)."""
    return _coerce_uuid(value)


def get_user_by_id(db: Session, user_id: str) -> User | None:
    """Get a user by ID."""
    uid = _coerce_uuid(user_id)
    if uid is None:
        return None
    return db.query(User).filter(User.id == uid).first()


def update_last_login(db: Session, user_id: str) -> None:
    """Update user's last login information."""
    user = get_user_by_id(db, user_id)
    if user:
        user.failed_login_attempts = 0
        user.locked_until = None
        db.commit()


def increment_failed_attempts(db: Session, user_id: str) -> None:
    """Increment failed login attempts and potentially lock account."""
    user = get_user_by_id(db, user_id)
    if user:
        user.failed_login_attempts += 1
        # Lock account after 5 failed attempts for 30 minutes
        if user.failed_login_attempts >= 5:
            user.locked_until = datetime.utcnow() + timedelta(minutes=30)
        db.commit()


def record_failed_login(db: Session, email: str) -> None:
    """Failed-password bookkeeping keyed by email (login form has no user id)."""
    user = get_user_by_email(db, (email or "").strip().lower())
    if user:
        increment_failed_attempts(db, str(user.id))


# ---------------------------------------------------------------- employee identity
def generate_employee_id(db: Session) -> str:
    """Allocate the next free employee_id (emp_001, emp_002, ...)."""
    n = db.query(User).count() + 1
    while True:
        candidate = f"emp_{n:03d}"
        exists = db.query(User).filter(User.employee_id == candidate).first()
        if not exists:
            return candidate
        n += 1


# ---------------------------------------------------------------- password reset (single-use tokens)
def _hash_reset_token(raw: str) -> str:
    import hashlib
    return hashlib.sha256(raw.encode()).hexdigest()


def create_password_reset_token(db: Session, user: User) -> str:
    """Create a single-use reset token; returns the RAW token (emailed once)."""
    import secrets as _secrets
    raw = _secrets.token_urlsafe(32)
    rec = PasswordResetToken(
        user_id=user.id,
        token_hash=_hash_reset_token(raw),
        expires_at=datetime.utcnow() + timedelta(minutes=settings.PASSWORD_RESET_EXPIRE_MIN),
    )
    db.add(rec)
    db.commit()
    return raw


def consume_password_reset_token(db: Session, raw: str) -> User | None:
    """Validate + burn a reset token. Returns the user, or None if invalid."""
    rec = (
        db.query(PasswordResetToken)
        .filter(PasswordResetToken.token_hash == _hash_reset_token(raw or ""))
        .first()
    )
    if not rec or rec.used_at is not None or rec.expires_at <= datetime.utcnow():
        return None
    user = db.query(User).filter(User.id == rec.user_id).first()
    if not user or not user.is_active:
        return None
    rec.used_at = datetime.utcnow()
    db.commit()
    return user