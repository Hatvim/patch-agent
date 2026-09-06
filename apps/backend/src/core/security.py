"""Password hashing, session JWTs, and GitHub-token encryption helpers."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from cryptography.fernet import Fernet, InvalidToken

from src.core.config import settings


_BCRYPT_ROUNDS = 12
_JWT_ALG = "HS256"


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(_BCRYPT_ROUNDS)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def create_session_token(user_id: uuid.UUID, session_version: int = 0) -> str:
    if not settings.jwt_secret:
        raise RuntimeError("JWT_SECRET is not configured")
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "ver": session_version,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=settings.session_ttl_hours)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_JWT_ALG)


def decode_session_token_payload(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[_JWT_ALG])


def decode_session_token(token: str) -> uuid.UUID:
    payload = decode_session_token_payload(token)
    return uuid.UUID(payload["sub"])


def _fernet() -> Fernet:
    if not settings.fernet_key:
        raise RuntimeError("FERNET_KEY is not configured")
    return Fernet(settings.fernet_key.encode("utf-8"))


def encrypt_github_token(token: str) -> bytes:
    return _fernet().encrypt(token.encode("utf-8"))


def decrypt_github_token(blob: bytes) -> str:
    try:
        return _fernet().decrypt(blob).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Failed to decrypt GitHub token") from exc
