import uuid
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from src.core.database import engine
from src.models.agent_run import AgentRun
from src.models.agent_run_event import AgentRunEvent
from src.models.repository import Repository
from src.models.task import Task
from src.services.code_search import search_code as _search_code

mcp = FastMCP(
    "PATCH Code Agent Procedures",
    instructions=(
        "Read-only procedures for code-generation agents working on PATCH. "
        "Use these tools to inspect agent runs, event streams, and repository context."
    ),
    host="127.0.0.1",
    port=8010,
    stateless_http=True,
    json_response=True,
)


@contextmanager
def session_scope():
    with Session(engine) as session:
        yield session


def _uuid(value: str, field_name: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid UUID") from exc


def _jsonable(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _run_summary(run: AgentRun) -> dict[str, Any]:
    task = run.task
    repository = task.repository if task else None
    pull_request = run.pull_request

    return _jsonable(
        {
            "id": run.id,
            "task_id": run.task_id,
            "repository": {
                "id": repository.id,
                "github_owner": repository.github_owner,
                "github_repo": repository.github_repo,
                "default_branch": repository.default_branch,
                "language": repository.language,
            }
            if repository
            else None,
            "instruction": run.follow_up_instruction
            or (task.instruction if task else None),
            "status": run.status,
            "parent_run_id": run.parent_run_id,
            "branch_name": run.branch_name,
            "model_id": run.model_id,
            "prompt_version": run.prompt_version,
            "max_turns": run.max_turns,
            "total_tool_calls": run.total_tool_calls,
            "total_tokens": run.total_tokens,
            "cost_usd": run.cost_usd,
            "error_message": run.error_message,
            "queued_at": run.queued_at,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "pull_request": {
                "github_pr_number": pull_request.github_pr_number,
                "title": pull_request.title,
                "url": pull_request.url,
                "state": pull_request.state,
                "head_branch": pull_request.head_branch,
                "base_branch": pull_request.base_branch,
            }
            if pull_request
            else None,
        }
    )


def _event_payload(event: AgentRunEvent) -> dict[str, Any]:
    return _jsonable(
        {
            "id": event.id,
            "agent_run_id": event.agent_run_id,
            "sequence": event.sequence,
            "event_type": event.event_type,
            "payload": event.payload,
            "created_at": event.created_at,
        }
    )


@mcp.tool()
def list_recent_agent_runs(
    caller_user_id: str,
    limit: int = 20,
    repository_id: str | None = None,
) -> list[dict[str, Any]]:
    """List recent agent runs for the caller, optionally filtered by repository_id."""
    limit = max(1, min(limit, 100))
    caller_uuid = _uuid(caller_user_id, "caller_user_id")
    repository_uuid = _uuid(repository_id, "repository_id") if repository_id else None

    statement = (
        select(AgentRun)
        .join(Task, AgentRun.task_id == Task.id)
        .where(Task.user_id == caller_uuid)
        .options(
            selectinload(AgentRun.task).selectinload(Task.repository),
            selectinload(AgentRun.pull_request),
        )
        .order_by(AgentRun.queued_at.desc())
        .limit(limit)
    )
    if repository_uuid is not None:
        statement = statement.where(Task.repository_id == repository_uuid)

    with session_scope() as session:
        return [_run_summary(run) for run in session.exec(statement).all()]


@mcp.tool()
def get_agent_run(run_id: str, caller_user_id: str) -> dict[str, Any]:
    """Get one agent run with task, repository, pull request, and tool call metadata."""
    run_uuid = _uuid(run_id, "run_id")
    caller_uuid = _uuid(caller_user_id, "caller_user_id")
    statement = (
        select(AgentRun)
        .join(Task, AgentRun.task_id == Task.id)
        .where(AgentRun.id == run_uuid, Task.user_id == caller_uuid)
        .options(
            selectinload(AgentRun.task).selectinload(Task.repository),
            selectinload(AgentRun.pull_request),
            selectinload(AgentRun.tool_calls),
        )
    )

    with session_scope() as session:
        run = session.exec(statement).first()
        if run is None:
            return {"found": False, "run_id": run_id}

        data = _run_summary(run)
        data["found"] = True
        data["tool_calls"] = _jsonable(
            [
                {
                    "id": tool_call.id,
                    "sequence": tool_call.sequence,
                    "tool_name": tool_call.tool_name,
                    "tool_input": tool_call.tool_input,
                    "tool_output": tool_call.tool_output,
                    "status": tool_call.status,
                    "error_message": tool_call.error_message,
                    "duration_ms": tool_call.duration_ms,
                    "started_at": tool_call.started_at,
                    "finished_at": tool_call.finished_at,
                }
                for tool_call in sorted(run.tool_calls, key=lambda item: item.sequence)
            ]
        )
        return data


@mcp.tool()
def list_agent_run_events(
    run_id: str, caller_user_id: str, limit: int = 50
) -> list[dict[str, Any]]:
    """List ordered event frames for an agent run owned by the caller."""
    run_uuid = _uuid(run_id, "run_id")
    caller_uuid = _uuid(caller_user_id, "caller_user_id")
    limit = max(1, min(limit, 100))

    with session_scope() as session:
        owner_statement = (
            select(AgentRun)
            .join(Task, AgentRun.task_id == Task.id)
            .where(AgentRun.id == run_uuid, Task.user_id == caller_uuid)
        )
        if session.exec(owner_statement).first() is None:
            return []
        statement = (
            select(AgentRunEvent)
            .where(AgentRunEvent.agent_run_id == run_uuid)
            .order_by(AgentRunEvent.sequence.asc())
            .limit(limit)
        )
        return [_event_payload(event) for event in session.exec(statement).all()]


@mcp.tool()
def get_repository_context(
    repository_id: str, caller_user_id: str, limit: int = 20
) -> dict[str, Any]:
    """Get repository metadata plus recent tasks and agent runs for code-gen context."""
    repository_uuid = _uuid(repository_id, "repository_id")
    caller_uuid = _uuid(caller_user_id, "caller_user_id")
    limit = max(1, min(limit, 100))

    with session_scope() as session:
        repository = session.get(Repository, repository_uuid)
        if repository is None or repository.user_id != caller_uuid:
            return {"found": False, "repository_id": repository_id}

        tasks_statement = (
            select(Task)
            .where(
                Task.repository_id == repository_uuid,
                Task.user_id == caller_uuid,
            )
            .order_by(Task.created_at.desc())
            .limit(limit)
        )
        runs_statement = (
            select(AgentRun)
            .join(Task, AgentRun.task_id == Task.id)
            .where(
                Task.repository_id == repository_uuid,
                Task.user_id == caller_uuid,
            )
            .options(
                selectinload(AgentRun.task).selectinload(Task.repository),
                selectinload(AgentRun.pull_request),
            )
            .order_by(AgentRun.queued_at.desc())
            .limit(limit)
        )

        tasks = session.exec(tasks_statement).all()
        runs = session.exec(runs_statement).all()

        return _jsonable(
            {
                "found": True,
                "repository": {
                    "id": repository.id,
                    "github_owner": repository.github_owner,
                    "github_repo": repository.github_repo,
                    "github_repo_id": repository.github_repo_id,
                    "default_branch": repository.default_branch,
                    "language": repository.language,
                    "clone_url": repository.clone_url,
                    "created_at": repository.created_at,
                    "updated_at": repository.updated_at,
                },
                "recent_tasks": [
                    {
                        "id": task.id,
                        "title": task.title,
                        "instruction": task.instruction,
                        "target_branch": task.target_branch,
                        "created_at": task.created_at,
                        "updated_at": task.updated_at,
                    }
                    for task in tasks
                ],
                "recent_agent_runs": [_run_summary(run) for run in runs],
            }
        )


@mcp.tool()
async def search_code(
    query: str,
    repository_id: str,
    caller_user_id: str,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Semantic code search over indexed repository chunks owned by the caller.

    Embeds the query, searches the vector DB for similar code chunks
    filtered by repository_id, and returns top-k results with file path,
    line range, language, and content preview.
    """
    limit = max(1, min(limit, 20))
    try:
        repository_uuid = _uuid(repository_id, "repository_id")
        caller_uuid = _uuid(caller_user_id, "caller_user_id")
    except ValueError as exc:
        return [{"error": str(exc)}]
    with session_scope() as session:
        repository = session.get(Repository, repository_uuid)
        if repository is None or repository.user_id != caller_uuid:
            return []
    try:
        results = await _search_code(query, repository_id, limit)
        return [r.to_dict() for r in results]
    except Exception as exc:
        return [{"error": str(exc)}]


def main() -> None:
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
