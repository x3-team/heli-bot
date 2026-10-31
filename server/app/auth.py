import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Invite, LoginCode, User

CODE_TTL = timedelta(minutes=10)
TOKEN_TTL = timedelta(days=14)
TRIAL = timedelta(days=7)
MIN_PASSWORD = 8


class AuthError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _pepper() -> str:
    secret = get_settings().jwt_secret
    if not secret:
        raise AuthError("auth_not_configured")
    return secret


def hash_secret(value: str) -> str:
    return hashlib.sha256(f"{_pepper()}:{value}".encode()).hexdigest()


def hash_invite(code: str) -> str:
    return hash_secret(code.strip())


def create_invite(session: Session, max_uses: int = 1) -> str:
    raw = secrets.token_urlsafe(9)
    session.add(
        Invite(
            code_hash=hash_invite(raw),
            max_uses=max_uses,
            uses=0,
            created_at=datetime.now(timezone.utc),
        )
    )
    session.flush()
    return raw


def _find_invite(session: Session, raw_code: str) -> Invite | None:
    try:
        code_hash = hash_invite(raw_code)
    except AuthError:
        raise
    return session.scalar(select(Invite).where(Invite.code_hash == code_hash))


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError("password_weak")


def _password_hash(password: str) -> str:
    _check_password(password)
    return hash_secret(password)


def _password_ok(stored: str | None, password: str) -> bool:
    if not stored:
        return False
    try:
        candidate = hash_secret(password)
    except AuthError:
        return False
    return hmac.compare_digest(stored, candidate)


def register_user(
    session: Session,
    email: str,
    password: str,
    *,
    consent_pdn: bool,
    consent_training: bool,
) -> User:
    normalized = _normalize_email(email)
    if session.scalar(select(User).where(User.email == normalized)):
        raise AuthError("email_taken")
    if not consent_pdn:
        raise AuthError("pdn_consent_required")
    now = datetime.now(timezone.utc)
    settings = get_settings()
    user = User(
        email=normalized,
        consent_pdn=True,
        consent_training=consent_training,
        is_admin=False,
        monthly_limit_rub=settings.default_monthly_limit_rub,
        password_hash=_password_hash(password),
        trial_ends_at=now + TRIAL,
        created_at=now,
    )
    session.add(user)
    session.flush()
    return user


def login_with_password(session: Session, email: str, password: str) -> User:
    normalized = _normalize_email(email)
    user = session.scalar(select(User).where(User.email == normalized))
    if user is None or not _password_ok(user.password_hash, password):
        raise AuthError("credentials_invalid")
    return user


def issue_recovery_code(session: Session, email: str) -> str:
    normalized = _normalize_email(email)
    user = session.scalar(select(User).where(User.email == normalized))
    if user is None or not user.password_hash:
        raise AuthError("user_not_found")
    code = f"{secrets.randbelow(1_000_000):06d}"
    session.add(
        LoginCode(
            email=normalized,
            invite_id=None,
            purpose="reset",
            code_hash=hash_secret(code),
            expires_at=datetime.now(timezone.utc) + CODE_TTL,
            used=False,
        )
    )
    session.flush()
    return code


def reset_password(session: Session, email: str, code: str, password: str) -> User:
    normalized = _normalize_email(email)
    user = session.scalar(select(User).where(User.email == normalized))
    if user is None:
        raise AuthError("user_not_found")
    row = _consume_code(session, normalized, code, "reset")
    if row is None:
        raise AuthError("code_invalid")
    user.password_hash = _password_hash(password)
    session.flush()
    return user


def issue_login_code(session: Session, email: str, invite_code: str) -> str:
    invite = _find_invite(session, invite_code)
    if invite is None or invite.uses >= invite.max_uses:
        raise AuthError("invite_invalid")
    normalized = _normalize_email(email)
    return _store_code(session, normalized, invite.id, "login")


def issue_login_code_for_user(session: Session, email: str) -> str:
    normalized = _normalize_email(email)
    user = session.scalar(select(User).where(User.email == normalized))
    if user is None:
        raise AuthError("user_not_found")
    return _store_code(session, normalized, None, "login")


def _store_code(session: Session, email: str, invite_id: uuid.UUID | None, purpose: str) -> str:
    code = f"{secrets.randbelow(1_000_000):06d}"
    session.add(
        LoginCode(
            email=email,
            invite_id=invite_id,
            purpose=purpose,
            code_hash=hash_secret(code),
            expires_at=datetime.now(timezone.utc) + CODE_TTL,
            used=False,
        )
    )
    session.flush()
    return code


def _consume_code(session: Session, email: str, code: str, purpose: str) -> LoginCode | None:
    try:
        code_hash = hash_secret(code.strip())
    except AuthError:
        raise
    now = datetime.now(timezone.utc)
    row = session.scalar(
        select(LoginCode)
        .where(
            LoginCode.email == email,
            LoginCode.code_hash == code_hash,
            LoginCode.purpose == purpose,
            LoginCode.used.is_(False),
        )
        .order_by(LoginCode.expires_at.desc())
    )
    if row is None:
        return None
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        return None
    row.used = True
    return row


def verify_login_code(session: Session, email: str, code: str) -> User:
    normalized = _normalize_email(email)
    now = datetime.now(timezone.utc)
    row = _consume_code(session, normalized, code, "login")
    if row is None:
        raise AuthError("code_invalid")
    user = session.scalar(select(User).where(User.email == normalized))
    if user is None:
        if row.invite_id is None:
            raise AuthError("user_not_found")
        user = User(
            email=normalized,
            consent_pdn=False,
            consent_training=False,
            is_admin=False,
            monthly_limit_rub=get_settings().default_monthly_limit_rub,
            trial_ends_at=now + TRIAL,
            created_at=now,
        )
        session.add(user)
    if row.invite_id is not None:
        invite = session.get(Invite, row.invite_id)
        if invite is not None and invite.uses < invite.max_uses:
            invite.uses += 1
    session.flush()
    return user


def trial_active(user: User, now: datetime | None = None) -> bool:
    if user.trial_ends_at is None:
        return False
    moment = now or datetime.now(timezone.utc)
    ends = user.trial_ends_at
    if ends.tzinfo is None:
        ends = ends.replace(tzinfo=timezone.utc)
    return moment < ends


def issue_token(user_id: uuid.UUID) -> str:
    secret = get_settings().jwt_secret
    if not secret:
        raise AuthError("auth_not_configured")
    payload = {
        "sub": str(user_id),
        "exp": datetime.now(timezone.utc) + TOKEN_TTL,
    }
    token = jwt.encode(payload, secret, algorithm="HS256")
    if isinstance(token, bytes):
        return token.decode()
    return token


def read_user_id(token: str) -> uuid.UUID:
    secret = get_settings().jwt_secret
    if not secret:
        raise AuthError("auth_not_configured")
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
        return uuid.UUID(str(payload["sub"]))
    except (jwt.PyJWTError, ValueError, KeyError) as exc:
        raise AuthError("token_invalid") from exc
