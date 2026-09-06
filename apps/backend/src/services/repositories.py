import logging
import re
from datetime import datetime, timezone
from uuid import UUID

from github import Github, GithubException
from sqlmodel import Session, select

from src.models.repository import Repository

logger = logging.getLogger(__name__)

_OWNER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,39}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


def _validate_owner_name(owner: str, name: str) -> None:
    if ".." in owner or not _OWNER_RE.match(owner):
        raise ValueError("Invalid repository owner")
    if ".." in name or not _REPO_RE.match(name):
        raise ValueError("Invalid repository name")


def connect_repo(
    user_id: UUID,
    owner: str,
    name: str,
    pat: str,
    session: Session,
) -> Repository:
    _validate_owner_name(owner, name)
    full_name = f"{owner}/{name}"

    existing = session.exec(
        select(Repository).where(
            Repository.user_id == user_id,
            Repository.github_owner == owner,
            Repository.github_repo == name,
        )
    ).first()

    if existing is not None:
        logger.debug("Repository %s already connected for user %s", full_name, user_id)
        return existing

    try:
        g = Github(pat)
        gh_repo = g.get_repo(full_name)
    except GithubException:
        raise ValueError(
            f"Repository {full_name} not found or not accessible with provided credentials."
        )

    repo = Repository(
        user_id=user_id,
        github_owner=owner,
        github_repo=name,
        github_repo_id=gh_repo.id,
        clone_url=gh_repo.clone_url,
        default_branch=gh_repo.default_branch,
        language=gh_repo.language,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    session.add(repo)
    session.commit()
    session.refresh(repo)

    logger.info("Connected repository %s for user %s", full_name, user_id)
    return repo


def disconnect_repo(
    user_id: UUID,
    repository_id: UUID,
    session: Session,
) -> None:
    repo = session.get(Repository, repository_id)
    if repo is None:
        raise ValueError(f"Repository {repository_id} not found.")

    if repo.user_id != user_id:
        raise PermissionError(
            f"User {user_id} is not authorized to disconnect repository {repository_id}."
        )

    session.delete(repo)
    session.commit()
    logger.info("Disconnected repository %s for user %s", repository_id, user_id)
