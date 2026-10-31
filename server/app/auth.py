import hashlib
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


def issue_login_code(session: Session, email: str, invite_code: str) -> str:
    invite = _find_invite(session, invite_code)
    if invite is None or invite.uses >= invite.max_uses:
        raise AuthError("invite_invalid")
    normalized = email.strip().lower()
    code = f"{secrets.randbelow(1_000_000):06d}"
    session.add(
        LoginCode(
            email=normalized,
            invite_id=invite.id,
            code_hash=hash_secret(code),
            expires_at=datetime.now(timezone.utc) + CODE_TTL,
            used=False,
        )
    )
    session.flush()
    return code


def verify_login_code(session: Session, email: str, code: str) -> User:
    normalized = email.strip().lower()
    try:
        code_hash = hash_secret(code.strip())
    except AuthError:
        raise
    now = datetime.now(timezone.utc)
    row = session.scalar(
        select(LoginCode)
        .where(
            LoginCode.email == normalized,
            LoginCode.code_hash == code_hash,
            LoginCode.used.is_(False),
        )
        .order_by(LoginCode.expires_at.desc())
    )
    if row is None:
        raise AuthError("code_invalid")
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        raise AuthError("code_invalid")
    row.used = True
    user = session.scalar(select(User).where(User.email == normalized))
    if user is None:
        user = User(
            email=normalized,
            consent_pdn=False,
            consent_training=False,
            is_admin=False,
            monthly_limit_rub=get_settings().default_monthly_limit_rub,
            created_at=now,
        )
        session.add(user)
    invite = session.get(Invite, row.invite_id)
    if invite is not None and invite.uses < invite.max_uses:
        invite.uses += 1
    session.flush()
    return user


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
