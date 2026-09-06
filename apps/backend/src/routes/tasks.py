import logging
import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlmodel import Session, select

from src.core.auth import current_user
from src.core.database import get_session
from src.models.enums import RunStatus
from src.models.usage_record import UsageRecord
from src.models.user import User
from src.repositories import agent_runs as agent_run_repo
from src.repositories import tasks as task_repo
from src.schemas.agent_run import AgentRunRead
from src.schemas.task import TaskCreate, TaskRead
from src.services.agent_dispatch import enqueue_agent_run

logger = logging.getLogger(__name__)

tasks_router = APIRouter(prefix="/tasks", tags=["Tasks"])

limiter = Limiter(key_func=get_remote_address)


@tasks_router.post(
    "/",
    response_model=AgentRunRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a task and queue an agent run",
)
@limiter.limit("30/minute")
async def create_task(
    request: Request,
    body: TaskCreate,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> AgentRunRead:
    """
    Creates a task submission and queues an agent run.

    **Normal flow** (no `parent_run_id`):
    - Validate `repository_id` belongs to the current user.
    - Create a `Task` row.
    - Create an `AgentRun` row with `status = queued`.
    - Enqueue `dispatch_agent_run` via Celery (Stream 3).
    - Return `AgentRunRead` with HTTP 201.

    **Follow-up flow** (`parent_run_id` set):
    - Validate `repository_id` belongs to the current user.
    - Validate the parent run belongs to the current user and has `status = succeeded`.
    - Reuse the parent's `task_id` and inherit `branch_name`.
    - Set `follow_up_instruction` on the new `AgentRun`.
    - Return `AgentRunRead` with HTTP 201.
    """

    repo = task_repo.get_repository_for_user(session, body.repository_id, user.id)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found or does not belong to you.",
        )

    today = date.today()
    usage = session.exec(
        select(UsageRecord).where(
            UsageRecord.user_id == user.id,
            UsageRecord.date == today,
        )
    ).first()
    if usage is not None and usage.run_count >= user.daily_run_quota:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Daily run quota exceeded",
        )

    parent_run = None
    if body.parent_run_id is not None:
        parent_run = agent_run_repo.get_agent_run_for_user(
            session, body.parent_run_id, user.id
        )
        if parent_run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Parent agent run not found or does not belong to you.",
            )
        if parent_run.status != RunStatus.succeeded:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Parent agent run must have status 'succeeded' to create a "
                    f"follow-up (current status: '{parent_run.status.value}')."
                ),
            )
        follow_up_target = agent_run_repo.resolve_follow_up_target(session, parent_run)
        if follow_up_target is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Parent agent run must have an open pull request and a matching "
                    "head branch before creating a follow-up."
                ),
            )
        branch_name, _parent_pr = follow_up_target
        active_follow_up = agent_run_repo.get_active_follow_up_for_branch(
            session, parent_run.task_id, branch_name
        )
        if active_follow_up is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "A follow-up run is already queued or running for this pull "
                    "request. Wait for it to finish before submitting more feedback."
                ),
            )
        if parent_run.branch_name != branch_name:
            parent_run.branch_name = branch_name
            session.add(parent_run)

    try:
        task = task_repo.create_task(session, user.id, body)
        if task is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    "Could not resolve task — parent run not found or not owned by you."
                ),
            )

        agent_run = agent_run_repo.create_agent_run(session, task.id, body, parent_run)

        session.commit()
        session.refresh(agent_run)
        # Ensure AgentRunRead has task/pull_request available for _inject_instruction
        session.refresh(agent_run, attribute_names=["task"])

    except HTTPException:
        session.rollback()
        raise
    except Exception as exc:
        session.rollback()
        logger.exception(
            "Unexpected error while creating task/agent run for user %s: %s",
            user.id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred. Please try again.",
        ) from exc

    try:
        enqueue_agent_run(agent_run.id)
    except Exception as exc:
        logger.exception("Celery enqueue failed for run %s: %s", agent_run.id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Queue unavailable, try again later.",
        ) from exc

    if usage is None:
        session.add(UsageRecord(user_id=user.id, date=today, run_count=1))
    else:
        usage.run_count += 1
        session.add(usage)
    session.commit()

    return AgentRunRead.model_validate(agent_run)

@tasks_router.get(
    "/{task_id}",
    response_model=TaskRead,
    summary="Get one task by ID",
)
async def get_task(
    task_id: uuid.UUID,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> TaskRead:
    """
    Returns a single task.
    Raises **404** if the task is missing or belongs to another user.
    """
    task = task_repo.get_task_for_user(session, task_id, user.id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found or does not belong to you.",
        )
    return TaskRead.model_validate(task)


@tasks_router.get(
    "/",
    response_model=list[TaskRead],
    summary="List tasks for the current user",
)
async def list_tasks(
    limit: int = Query(
        default=50, ge=1, le=100, description="Max number of tasks to return (1–100)"
    ),
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
) -> list[TaskRead]:
    """
    Returns the current user's tasks ordered by newest first.
    Supports `limit` query parameter (default 50, max 100).
    """
    tasks = task_repo.list_tasks_by_user(session, user.id, limit=limit)
    return [TaskRead.model_validate(t) for t in tasks]
