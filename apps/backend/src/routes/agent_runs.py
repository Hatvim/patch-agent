import logging
import uuid
from datetime import datetime, timezone
import docker
import httpx
from celery.exceptions import CeleryError
from docker.errors import DockerException, NotFound
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlmodel import Session, select

from src.celery_app import celery_app
from src.core.auth import current_user
from src.core.database import get_session
from src.models.agent_run import AgentRun
from src.models.agent_run_event import AgentRunEvent
from src.models.enums import EventType, RunStatus
from src.models.task import Task
from src.models.user import User
from src.repositories import agent_runs as agent_runs_repo
from src.schemas.agent_run import AgentRunListItemRead, AgentRunRead
from src.schemas.agent_run_event import AgentRunEventRead
from src.schemas.diff import DiffFileRead
from src.schemas.pull_request import PullRequestRead
from src.services.credentials import get_active_token
from src.services.events import publish_status_change
from src.services.github_pr import maybe_refresh_pr


class ReviewFindingRead(BaseModel):
    file_path: str
    severity: str
    category: str
    issue: str
    suggestion: str


class ReviewRunRead(BaseModel):
    reviewer_run_id: str
    status: RunStatus
    findings: list[ReviewFindingRead]
    fix_run_id: str | None = None

TERMINAL_STATUSES = {RunStatus.succeeded, RunStatus.failed, RunStatus.cancelled}

MAX_DIFF_CHARS = 200_000
MAX_FILE_CHARS = 50_000
MAX_DIFF_PAGES = 3

logger = logging.getLogger(__name__)

agent_runs_router = APIRouter(prefix="/agent_runs", tags=["Agent Runs"])

limiter = Limiter(key_func=get_remote_address)


@agent_runs_router.get("/", response_model=list[AgentRunListItemRead])
def list_agent_runs(
    limit: int = Query(50, ge=1, le=100),
    repository_id: uuid.UUID | None = Query(None),
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    runs = agent_runs_repo.list_agent_runs_for_user(
        session, user.id, limit, repository_id=repository_id
    )
    return [AgentRunListItemRead.model_validate(r) for r in runs]


@agent_runs_router.get("/{id}", response_model=AgentRunRead)
def get_agent_run(
    id: uuid.UUID,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    run = agent_runs_repo.get_agent_run_detail_for_user(session, id, user.id)
    if not run:
        raise HTTPException(status_code=404, detail="Agent run not found")
    return AgentRunRead.model_validate(run)


@agent_runs_router.get("/{id}/events", response_model=list[AgentRunEventRead])
def list_agent_run_events(
    id: uuid.UUID,
    limit: int = Query(50, ge=1, le=100),
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    run = agent_runs_repo.get_agent_run_for_user(session, id, user.id)
    if not run:
        raise HTTPException(status_code=404, detail="Agent run not found")
    events = agent_runs_repo.list_events_for_user(session, id, user.id, limit)
    return [AgentRunEventRead.model_validate(e) for e in events]


@agent_runs_router.get("/{id}/pull_request", response_model=PullRequestRead)
def get_pull_request(
    id: uuid.UUID,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    pr = agent_runs_repo.get_pull_request_for_run_for_user(session, id, user.id)
    if not pr:
        raise HTTPException(status_code=404, detail="Pull request not found")

    repository = agent_runs_repo.get_repository_for_pull_request_for_user(session, pr, user.id)
    if repository is not None:
        try:
            pat = get_active_token(session, user.id)
            pr = maybe_refresh_pr(pr, repository, pat, session)
        except Exception as exc:
            logger.warning("PR lazy-refresh skipped: %s", exc)

    return PullRequestRead.model_validate(pr)


@agent_runs_router.get("/{id}/diff", response_model=list[DiffFileRead])
@limiter.limit("30/minute")
def get_agent_run_diff(
    request: Request,
    id: uuid.UUID,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    pr = agent_runs_repo.get_pull_request_for_run_for_user(session, id, user.id)
    if not pr:
        raise HTTPException(status_code=404, detail="Pull request not found")

    repository = agent_runs_repo.get_repository_for_pull_request_for_user(session, pr, user.id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    pat = get_active_token(session, user.id)

    url = f"https://api.github.com/repos/{repository.github_owner}/{repository.github_repo}/pulls/{pr.github_pr_number}/files"
    headers = {
        "Authorization": f"token {pat}",
        "Accept": "application/vnd.github.v3+json",
    }

    try:
        out: list[DiffFileRead] = []
        total_chars = 0
        truncated = False
        for page in range(1, MAX_DIFF_PAGES + 1):
            response = httpx.get(
                url,
                headers=headers,
                params={"per_page": 100, "page": page},
                timeout=10.0,
            )
            if response.status_code == 401:
                raise HTTPException(status_code=401, detail="Invalid GitHub credential")
            if response.status_code == 403:
                raise HTTPException(status_code=403, detail="GitHub API forbidden")
            if response.status_code == 404:
                raise HTTPException(status_code=404, detail="GitHub PR not found")
            response.raise_for_status()
            files = response.json()
            if not isinstance(files, list):
                raise HTTPException(status_code=502, detail="GitHub API error")
            if not files:
                break
            capped = False
            for f in files:
                if not isinstance(f, dict):
                    continue
                patch = f.get("patch")
                if isinstance(patch, str):
                    if len(patch) > MAX_FILE_CHARS:
                        truncated = True
                        continue
                    if total_chars + len(patch) > MAX_DIFF_CHARS:
                        truncated = True
                        capped = True
                        break
                    total_chars += len(patch)
                try:
                    out.append(
                        DiffFileRead(
                            file_path=str(f["filename"]),
                            status=str(f["status"]),
                            additions=int(f["additions"]),
                            deletions=int(f["deletions"]),
                            patch=f.get("patch"),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    logger.warning("Skipping malformed GitHub file object: %r", f)
                    continue
            if capped:
                break
            if len(files) < 100:
                break
        if truncated:
            logger.warning("PR diff truncated truncated=True chars=%s", total_chars)
        return out
    except httpx.HTTPError as e:
        logger.error(f"GitHub API error: {e}")
        raise HTTPException(status_code=502, detail="GitHub API error")


@agent_runs_router.get("/{id}/review", response_model=ReviewRunRead)
def get_agent_run_review(
    id: uuid.UUID,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    """Return the auto-review result for a developer run, including all findings."""
    run = agent_runs_repo.get_agent_run_for_user(session, id, user.id)
    if not run:
        raise HTTPException(status_code=404, detail="Agent run not found")
    if run.reviewer_run_id is None:
        raise HTTPException(status_code=404, detail="No review available yet")

    reviewer_run = session.get(AgentRun, run.reviewer_run_id)
    if not reviewer_run:
        raise HTTPException(status_code=404, detail="Reviewer run not found")

    # Load review_finding events for the reviewer run
    finding_events = session.exec(
        select(AgentRunEvent)
        .where(
            AgentRunEvent.agent_run_id == run.reviewer_run_id,
            AgentRunEvent.event_type == EventType.review_finding,
        )
        .order_by(AgentRunEvent.sequence)
    ).all()

    findings = [ReviewFindingRead(**e.payload) for e in finding_events if isinstance(e.payload, dict)]

    # Check if a fixer run exists (parent_run_id == developer run, run_role == fixer)
    from src.models.enums import RunRole  # noqa: PLC0415
    fix_run = session.exec(
        select(AgentRun)
        .join(Task, AgentRun.task_id == Task.id)
        .where(
            AgentRun.parent_run_id == id,
            AgentRun.run_role == RunRole.fixer,
            Task.user_id == user.id,
        )
        .order_by(AgentRun.queued_at.desc())
    ).first()

    return ReviewRunRead(
        reviewer_run_id=str(run.reviewer_run_id),
        status=reviewer_run.status,
        findings=findings,
        fix_run_id=str(fix_run.id) if fix_run else None,
    )


@agent_runs_router.post("/{id}/cancel", response_model=AgentRunRead, status_code=202)
def cancel_agent_run(
    id: uuid.UUID,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    run = agent_runs_repo.get_agent_run_for_user(session, id, user.id)
    if not run:
        raise HTTPException(status_code=404, detail="Agent run not found")
    if run.status in TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail=f"Agent run already {run.status.value}")

    if run.celery_task_id:
        try:
            celery_app.control.revoke(run.celery_task_id, terminate=True, signal="SIGTERM")
        except CeleryError as exc:
            logger.warning("Celery revoke failed for %s: %s", run.celery_task_id, exc)

    if run.container_id:
        try:
            client = docker.from_env()
            container = client.containers.get(run.container_id)
            container.stop(timeout=5)
        except NotFound:
            pass  # container already gone — common when the run is finishing on its own
        except DockerException as exc:
            logger.warning("Docker stop failed for %s: %s", run.container_id, exc)

    run.status = RunStatus.cancelled
    run.finished_at = datetime.now(timezone.utc)
    session.add(run)
    session.commit()
    session.refresh(run)

    # Notify any open WebSocket so it closes via the terminal-status-change branch in ws.py.
    publish_status_change(str(run.id), RunStatus.cancelled.value, sequence=-(2**31))

    return AgentRunRead.model_validate(run)
