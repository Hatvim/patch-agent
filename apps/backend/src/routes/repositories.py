import logging
import re
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, field_validator
from sqlmodel import Session, select

from src.core.auth import current_user
from src.core.database import get_session
from src.models.repository import Repository
from src.services.credentials import get_active_token
from src.services.repositories import connect_repo, disconnect_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/repositories", tags=["repositories"])


_OWNER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,39}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


class RepositoryCreate(BaseModel):
    owner: str
    name: str

    @field_validator("owner")
    @classmethod
    def _validate_owner(cls, v: str) -> str:
        if ".." in v or not _OWNER_RE.match(v):
            raise ValueError("Invalid repository owner")
        return v

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if ".." in v or not _REPO_RE.match(v):
            raise ValueError("Invalid repository name")
        return v


class RepositoryRead(BaseModel):
    id: str
    github_owner: str
    github_repo: str
    full_name: str
    default_branch: str
    language: str | None
    created_at: datetime
    updated_at: datetime


def _to_read(repo: Repository) -> RepositoryRead:
    return RepositoryRead(
        id=str(repo.id),
        github_owner=repo.github_owner,
        github_repo=repo.github_repo,
        full_name=f"{repo.github_owner}/{repo.github_repo}",
        default_branch=repo.default_branch,
        language=repo.language,
        created_at=repo.created_at,
        updated_at=repo.updated_at,
    )


@router.get("/", response_model=list[RepositoryRead])
def list_repositories(
    session: Session = Depends(get_session),
    user=Depends(current_user),
) -> list[RepositoryRead]:
    user_uuid = UUID(user.id) if isinstance(user.id, str) else user.id
    repos = session.exec(
        select(Repository).where(Repository.user_id == user_uuid)
    ).all()
    return [_to_read(r) for r in repos]


@router.post("/", response_model=RepositoryRead, status_code=status.HTTP_201_CREATED)
def create_repository(
    body: RepositoryCreate,
    session: Session = Depends(get_session),
    user=Depends(current_user),
) -> RepositoryRead:
    """Connect a GitHub repo. The agent reads source via ripgrep at runtime; no indexing step."""
    user_uuid = UUID(user.id) if isinstance(user.id, str) else user.id
    pat = get_active_token(session, user_uuid)

    try:
        repo = connect_repo(
            user_id=user_uuid,
            owner=body.owner,
            name=body.name,
            pat=pat,
            session=session,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    return _to_read(repo)


@router.delete("/{repository_id}", status_code=204)
def delete_repository(
    repository_id: UUID,
    session: Session = Depends(get_session),
    user=Depends(current_user),
) -> Response:
    user_uuid = UUID(user.id) if isinstance(user.id, str) else user.id
    try:
        disconnect_repo(
            user_id=user_uuid,
            repository_id=repository_id,
            session=session,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

    return Response(status_code=204)
