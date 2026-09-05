import uuid

from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from src.core.config import settings
from src.models.agent_run import AgentRun
from src.models.agent_run_event import AgentRunEvent
from src.models.enums import PRState, RunStatus
from src.models.pull_request import PullRequest
from src.models.repository import Repository
from src.models.task import Task
from src.schemas.task import TaskCreate


def list_agent_runs_for_user(
    session: Session,
    user_id: uuid.UUID,
    limit: int = 50,
    repository_id: uuid.UUID | None = None,
) -> list[AgentRun]:
    if limit > 100:
        limit = 100
    statement = (
        select(AgentRun)
        .join(Task, AgentRun.task_id == Task.id)
        .where(Task.user_id == user_id)
        .options(
            selectinload(AgentRun.pull_request),
            selectinload(AgentRun.task),
        )
        .order_by(AgentRun.queued_at.desc())
        .limit(limit)
    )
    if repository_id is not None:
        statement = statement.where(Task.repository_id == repository_id)
    return list(session.exec(statement).all())


def get_agent_run_for_user(
    session: Session, run_id: uuid.UUID, user_id: uuid.UUID
) -> AgentRun | None:
    statement = (
        select(AgentRun)
        .join(Task, AgentRun.task_id == Task.id)
        .where(AgentRun.id == run_id, Task.user_id == user_id)
    )
    return session.exec(statement).first()


def get_agent_run_detail_for_user(
    session: Session, run_id: uuid.UUID, user_id: uuid.UUID
) -> AgentRun | None:
    statement = (
        select(AgentRun)
        .join(Task, AgentRun.task_id == Task.id)
        .where(AgentRun.id == run_id, Task.user_id == user_id)
        .options(
            selectinload(AgentRun.tool_calls),
            selectinload(AgentRun.pull_request),
            selectinload(AgentRun.task),
        )
    )
    return session.exec(statement).first()


def list_events_for_user(
    session: Session,
    run_id: uuid.UUID,
    user_id: uuid.UUID,
    limit: int = 50,
) -> list[AgentRunEvent]:
    if limit > 100:
        limit = 100

    statement = (
        select(AgentRunEvent)
        .join(AgentRun, AgentRunEvent.agent_run_id == AgentRun.id)
        .join(Task, AgentRun.task_id == Task.id)
        .where(AgentRunEvent.agent_run_id == run_id, Task.user_id == user_id)
        .order_by(AgentRunEvent.sequence.asc())
        .limit(limit)
    )
    return list(session.exec(statement).all())


def get_pull_request_for_run_for_user(
    session: Session, run_id: uuid.UUID, user_id: uuid.UUID
) -> PullRequest | None:
    ownership_statement = (
        select(AgentRun.id)
        .join(Task, AgentRun.task_id == Task.id)
        .where(AgentRun.id == run_id, Task.user_id == user_id)
    )
    if not session.exec(ownership_statement).first():
        return None

    return get_pull_request_for_run(session, run_id)


def get_repository_for_pull_request_for_user(
    session: Session, pr: PullRequest, user_id: uuid.UUID
) -> Repository | None:
    statement = select(Repository).where(
        Repository.id == pr.repository_id, Repository.user_id == user_id
    )
    return session.exec(statement).first()


def create_agent_run(
    session: Session,
    task_id: uuid.UUID,
    data: TaskCreate,
    parent_run: AgentRun | None = None,
) -> AgentRun:
    branch_name = None
    model_id = settings.llm_model_id
    follow_up_instruction = data.follow_up_instruction

    if parent_run:
        branch_name = parent_run.branch_name
        model_id = parent_run.model_id
        if not follow_up_instruction:
            follow_up_instruction = data.instruction

    run = AgentRun(
        task_id=task_id,
        status=RunStatus.queued,
        parent_run_id=data.parent_run_id,
        follow_up_instruction=follow_up_instruction,
        branch_name=branch_name,
        model_id=model_id,
        prompt_version="v1",
        max_turns=15,
    )
    session.add(run)
    session.flush()
    return run


def get_agent_run(session: Session, run_id: uuid.UUID) -> AgentRun | None:
    statement = (
        select(AgentRun)
        .where(AgentRun.id == run_id)
        .options(
            selectinload(AgentRun.tool_calls),
            selectinload(AgentRun.pull_request),
            selectinload(AgentRun.task),
        )
    )
    return session.exec(statement).first()


def list_events(
    session: Session, run_id: uuid.UUID, limit: int = 50
) -> list[AgentRunEvent]:
    if limit > 100:
        limit = 100
    statement = (
        select(AgentRunEvent)
        .where(AgentRunEvent.agent_run_id == run_id)
        .order_by(AgentRunEvent.sequence.asc())
        .limit(limit)
    )
    return list(session.exec(statement).all())


def get_pull_request_for_run(session: Session, run_id: uuid.UUID) -> PullRequest | None:
    seen_run_ids: set[uuid.UUID] = set()
    current_run_id = run_id
    max_depth = 50
    depth = 0

    while current_run_id and depth < max_depth:
        if current_run_id in seen_run_ids:
            break
        seen_run_ids.add(current_run_id)

        statement = select(PullRequest).where(
            PullRequest.agent_run_id == current_run_id
        )
        pr = session.exec(statement).first()
        if pr:
            return pr

        run = session.get(AgentRun, current_run_id)
        if not run or not run.parent_run_id:
            break
        current_run_id = run.parent_run_id
        depth += 1

    return None


def get_active_follow_up_for_branch(
    session: Session,
    task_id: uuid.UUID,
    branch_name: str,
) -> AgentRun | None:
    statement = (
        select(AgentRun)
        .where(
            AgentRun.task_id == task_id,
            AgentRun.branch_name == branch_name,
            AgentRun.parent_run_id.is_not(None),  # type: ignore[attr-defined]
            AgentRun.status.in_([RunStatus.queued, RunStatus.running]),  # type: ignore[attr-defined]
        )
        .order_by(AgentRun.queued_at.asc())
        .limit(1)
    )
    return session.exec(statement).first()


def resolve_follow_up_target(
    session: Session,
    parent_run: AgentRun,
) -> tuple[str, PullRequest] | None:
    pr = get_pull_request_for_run(session, parent_run.id)
    if pr is None or pr.state not in {PRState.open, PRState.draft}:
        return None

    branch_name = parent_run.branch_name or pr.head_branch
    if not branch_name:
        return None
    if (
        parent_run.branch_name
        and pr.head_branch
        and parent_run.branch_name != pr.head_branch
    ):
        return None

    return branch_name, pr
