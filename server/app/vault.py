"""AES-256-GCM vault. The model only ever sees the placeholder <CREDENTIAL>."""

from __future__ import annotations

import base64
import binascii
import logging
import os
import uuid
from datetime import datetime, timezone

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Credential, CredentialAccess, User

log = logging.getLogger(__name__)
PLACEHOLDER = "<CREDENTIAL>"


class VaultError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def store_credential(session: Session, user: User, site: str, username: str, secret: str) -> Credential:
    nonce, ciphertext = _seal(secret)
    row = Credential(
        user_id=user.id,
        site=site.strip(),
        username=username.strip(),
        nonce=nonce,
        ciphertext=ciphertext,
        created_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.flush()
    _audit(session, user.id, row.id, "create")
    return row


def list_credentials(session: Session, user: User) -> list[dict]:
    rows = session.scalars(
        select(Credential).where(Credential.user_id == user.id).order_by(Credential.created_at)
    )
    return [_public(row) for row in rows]


def delete_credential(session: Session, user: User, credential_id: uuid.UUID) -> None:
    row = _own(session, user, credential_id)
    _audit(session, user.id, row.id, "delete")
    session.delete(row)
    session.flush()


def fill_for_model(session: Session, user: User, credential_id: uuid.UUID) -> str:
    """What the model is allowed to see."""
    row = _own(session, user, credential_id)
    _audit(session, user.id, row.id, "fill")
    return PLACEHOLDER


def secret_for_browser(session: Session, user: User, credential_id: uuid.UUID) -> str:
    """Server-side substitution. Never return this from an HTTP handler."""
    row = _own(session, user, credential_id)
    _audit(session, user.id, row.id, "browser_fill")
    return _open(row)


def public_credential(row: Credential) -> dict:
    return _public(row)


def _public(row: Credential) -> dict:
    return {"id": str(row.id), "site": row.site, "username": row.username}


def _own(session: Session, user: User, credential_id: uuid.UUID) -> Credential:
    row = session.get(Credential, credential_id)
    if row is None or row.user_id != user.id:
        raise VaultError("not_found")
    return row


def _audit(session: Session, user_id: uuid.UUID, credential_id: uuid.UUID, action: str) -> None:
    session.add(
        CredentialAccess(
            user_id=user_id,
            credential_id=credential_id,
            action=action,
            created_at=datetime.now(timezone.utc),
        )
    )
    session.flush()
    log.info("credential access action=%s", action)


def _key() -> bytes:
    raw = os.environ.get("VAULT_MASTER_KEY", "").strip()
    if not raw:
        raise VaultError("vault_unavailable")
    try:
        key = base64.b64decode(raw, validate=True)
    except binascii.Error as exc:
        raise VaultError("vault_unavailable") from exc
    if len(key) != 32:
        raise VaultError("vault_unavailable")
    return key


def _seal(secret: str) -> tuple[bytes, bytes]:
    nonce = os.urandom(12)
    ciphertext = AESGCM(_key()).encrypt(nonce, secret.encode(), None)
    return nonce, ciphertext


def _open(row: Credential) -> str:
    try:
        plain = AESGCM(_key()).decrypt(row.nonce, row.ciphertext, None)
    except Exception as exc:
        raise VaultError("vault_unavailable") from exc
    return plain.decode()
