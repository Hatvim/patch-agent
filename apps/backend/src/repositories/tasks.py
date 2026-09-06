import uuid

from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from src.models.agent_run import AgentRun
from src.models.repository import Repository
from src.models.task import Task
from src.schemas.task import TaskCreate


def get_repository_for_user(
    session: Session, repository_id: uuid.UUID, user_id: uuid.UUID
) -> Repository | None:
    statement = select(Repository).where(
        Repository.id == repository_id,
        Repository.user_id == user_id,
    )
    return session.exec(statement).first()


def get_task_for_user(
    session: Session, task_id: uuid.UUID, user_id: uuid.UUID
) -> Task | None:
    statement = select(Task).where(
        Task.id == task_id,
        Task.user_id == user_id,
    )
    return session.exec(statement).first()


def create_task(
    session: Session, user_id: uuid.UUID, data: TaskCreate
) -> Task | None:
    if data.parent_run_id:
        statement = (
            select(AgentRun)
            .where(AgentRun.id == data.parent_run_id)
            .options(selectinload(AgentRun.task))
        )
        parent_run = session.exec(statement).first()
        if not parent_run or parent_run.task.user_id != user_id:
            return None
        return parent_run.task

    raw = (data.instruction or "").strip()
    title = raw[:80] if raw else "New Task"

    task = Task(
        user_id=user_id,
        repository_id=data.repository_id,
        title=title,
        instruction=data.instruction,
        target_branch=data.target_branch,
    )
    session.add(task)
    session.flush()
    return task


def get_task(session: Session, task_id: uuid.UUID) -> Task | None:
    return session.get(Task, task_id)


def list_tasks_by_user(
    session: Session, user_id: uuid.UUID, limit: int = 50
) -> list[Task]:
    if limit > 100:
        limit = 100
    statement = (
        select(Task)
        .where(Task.user_id == user_id)
        .order_by(Task.created_at.desc())
        .limit(limit)
    )
    return list(session.exec(statement).all())
