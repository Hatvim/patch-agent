"""Authentication and GitHub OAuth routes."""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
import redis
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlmodel import Session, select

from src.core.auth import current_user
from src.core.config import settings
from src.core.database import get_session
from src.core.security import create_session_token, encrypt_github_token, hash_password
from src.models.github_credential import GithubCredential
from src.models.user import User
from src.schemas.user import UserRead

logger = logging.getLogger(__name__)

limiter = Limiter(key_func=get_remote_address)

auth_router = APIRouter(prefix="/auth", tags=["Auth"])
me_router = APIRouter(prefix="/me", tags=["Me"])

_OAUTH_STATE_COOKIE = "patch_oauth_state"
_GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
_GITHUB_USER_URL = "https://api.github.com/user"
_GITHUB_USER_EMAILS_URL = "https://api.github.com/user/emails"
_GITHUB_SCOPES = "repo read:user user:email"


def _is_production() -> bool:
    return settings.environment.lower() == "production"


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=_is_production(),
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        samesite="lax",
        secure=_is_production(),
        httponly=True,
    )


@auth_router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> Response:
    user.session_version += 1
    session.add(user)
    session.commit()
    _clear_session_cookie(response)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@me_router.get("", response_model=UserRead)
def get_me(user: User = Depends(current_user)) -> UserRead:
    return UserRead.model_validate(user)


# ---------------------------------------------------------------------------
# GitHub OAuth (also serves as login/signup)
# ---------------------------------------------------------------------------


def _oauth_redis_client() -> redis.Redis | None:
    if not settings.redis_url:
        return None
    try:
        return redis.Redis.from_url(settings.redis_url, socket_timeout=2)
    except Exception as exc:
        logger.warning("OAuth state Redis unavailable: %s", exc)
        return None


def _store_oauth_state(state: str) -> None:
    client = _oauth_redis_client()
    if client is None:
        return
    try:
        client.set(f"oauth_state:{state}", "1", ex=600, nx=True)
    except Exception as exc:
        logger.warning("OAuth state store failed, continuing cookie-only: %s", exc)
    finally:
        try:
            client.close()
        except Exception:
            pass


def _consume_oauth_state(state: str) -> bool | None:
    """Consume a one-time OAuth state. True=consumed, False=replayed/missing,
    None=Redis unreachable (caller falls back to cookie check)."""
    client = _oauth_redis_client()
    if client is None:
        return None
    try:
        try:
            deleted = client.getdel(f"oauth_state:{state}")
        except AttributeError:
            # redis-py without GETDEL: GET then DEL (best-effort one-time use).
            deleted = client.get(f"oauth_state:{state}")
            if deleted is not None:
                client.delete(f"oauth_state:{state}")
        return deleted is not None
    except Exception as exc:
        logger.warning("OAuth state lookup failed, falling back to cookie: %s", exc)
        return None
    finally:
        try:
            client.close()
        except Exception:
            pass


@auth_router.get("/github/start")
@limiter.limit("30/minute")
def github_start(request: Request) -> RedirectResponse:
    if not settings.github_oauth_client_id or not settings.github_oauth_client_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GitHub OAuth is not configured on this server",
        )

    state = secrets.token_urlsafe(32)
    _store_oauth_state(state)
    params = {
        "client_id": settings.github_oauth_client_id,
        "redirect_uri": settings.github_oauth_redirect_uri,
        "scope": _GITHUB_SCOPES,
        "state": state,
    }
    redirect = RedirectResponse(f"{_GITHUB_AUTHORIZE_URL}?{urlencode(params)}", status_code=302)
    redirect.set_cookie(
        key=_OAUTH_STATE_COOKIE,
        value=state,
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=_is_production(),
        path="/auth/github",
    )
    return redirect


def _login_error_redirect(reason: str) -> RedirectResponse:
    response = RedirectResponse(f"{settings.frontend_url}/login?error={reason}", status_code=302)
    response.delete_cookie(_OAUTH_STATE_COOKIE, path="/auth/github")
    return response


@auth_router.get("/github/callback")
async def github_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    if error:
        return _login_error_redirect("github_denied")
    if not code or not state:
        return _login_error_redirect("missing_code")

    expected_state = request.cookies.get(_OAUTH_STATE_COOKIE)
    if not expected_state or not secrets.compare_digest(expected_state, state):
        return _login_error_redirect("invalid_state")

    redis_verdict = _consume_oauth_state(state)
    if redis_verdict is False:
        return _login_error_redirect("invalid_state")

    async with httpx.AsyncClient(timeout=15.0) as client:
        token_resp = await client.post(
            _GITHUB_TOKEN_URL,
            data={
                "client_id": settings.github_oauth_client_id,
                "client_secret": settings.github_oauth_client_secret,
                "code": code,
                "redirect_uri": settings.github_oauth_redirect_uri,
            },
            headers={"Accept": "application/json"},
        )
        if token_resp.status_code != 200:
            logger.warning("GitHub token exchange failed: %s %s", token_resp.status_code, token_resp.text)
            return _login_error_redirect("token_exchange_failed")
        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        scopes = token_data.get("scope", "")
        if not access_token:
            logger.warning("GitHub returned no access token: %s", token_data)
            return _login_error_redirect("no_access_token")

        gh_headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        }

        user_resp = await client.get(_GITHUB_USER_URL, headers=gh_headers)
        if user_resp.status_code != 200:
            return _login_error_redirect("github_user_failed")
        user_data = user_resp.json()
        github_username = user_data.get("login") or "unknown"
        github_name = user_data.get("name") or github_username

        emails_resp = await client.get(_GITHUB_USER_EMAILS_URL, headers=gh_headers)
        if emails_resp.status_code != 200:
            return _login_error_redirect("github_emails_failed")
        emails = emails_resp.json() or []

    primary_email: str | None = None
    for entry in emails:
        if isinstance(entry, dict) and entry.get("primary") and entry.get("verified"):
            primary_email = entry.get("email")
            break
    if not primary_email:
        return _login_error_redirect("no_verified_email")

    user = session.exec(select(User).where(User.email == primary_email)).first()
    if user is None:
        user = User(
            email=primary_email,
            name=github_name,
            hashed_password=hash_password(secrets.token_urlsafe(32)),
        )
        session.add(user)
        session.commit()
        session.refresh(user)

    now = datetime.now(timezone.utc)
    existing = session.exec(
        select(GithubCredential).where(
            GithubCredential.user_id == user.id,
            GithubCredential.revoked_at.is_(None),  # type: ignore[attr-defined]
        )
    ).all()
    for cred in existing:
        cred.revoked_at = now
        session.add(cred)

    new_cred = GithubCredential(
        user_id=user.id,
        github_username=github_username,
        encrypted_token=encrypt_github_token(access_token),
        token_scopes=scopes,
    )
    session.add(new_cred)
    session.commit()

    response = RedirectResponse(f"{settings.frontend_url}/", status_code=302)
    _set_session_cookie(response, create_session_token(user.id, user.session_version))
    response.delete_cookie(_OAUTH_STATE_COOKIE, path="/auth/github")
    return response


# ---------------------------------------------------------------------------
# GitHub credential listing (read-only) under /me
# ---------------------------------------------------------------------------


class GithubCredentialRead(BaseModel):
    id: str
    github_username: str
    token_scopes: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


@me_router.get("/github-credentials", response_model=list[GithubCredentialRead])
def list_github_credentials(
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> list[GithubCredentialRead]:
    rows = session.exec(
        select(GithubCredential).where(GithubCredential.user_id == user.id)
    ).all()
    return [
        GithubCredentialRead(
            id=str(c.id),
            github_username=c.github_username,
            token_scopes=c.token_scopes,
            created_at=c.created_at,
            last_used_at=c.last_used_at,
            revoked_at=c.revoked_at,
        )
        for c in rows
    ]


@me_router.delete("/github-credentials/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_github_credential(
    credential_id: str,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> Response:
    import uuid as _uuid

    try:
        cred_uuid = _uuid.UUID(credential_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid credential id")

    cred = session.get(GithubCredential, cred_uuid)
    if cred is None or cred.user_id != user.id:
        raise HTTPException(status_code=404, detail="Credential not found")
    cred.revoked_at = datetime.now(timezone.utc)
    session.add(cred)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
