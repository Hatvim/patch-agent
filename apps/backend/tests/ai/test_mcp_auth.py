"""MCP caller-scoped auth tests (Task 11).

Verifies every MCP tool enforces Repository.user_id == caller /
Task.user_id == caller and returns found=False (or [] for list tools)
on cross-user access without leaking existence.
"""

import datetime
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import JSON, String, TypeDecorator, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import Uuid as SA_Uuid
from sqlmodel import Session, SQLModel, create_engine

import src.models  # noqa: F401  (register models)
from src.models.agent_run import AgentRun
from src.models.agent_run_event import AgentRunEvent
from src.models.enums import EventType, RunStatus
from src.models.repository import Repository
from src.models.task import Task
from src.models.user import User


class GUID(TypeDecorator):
    impl = String(36)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            if not isinstance(value, uuid.UUID):
                return uuid.UUID(value).hex
            return value.hex
        return value

    def process_result_value(self, value, dialect):
        if value is not None:
            return uuid.UUID(value) if not isinstance(value, uuid.UUID) else value
        return value


@event.listens_for(SQLModel.metadata, "before_create")
def _remap_pg_types_for_sqlite(target, connection, **kw):
    if connection.dialect.name != "sqlite":
        return
    for table in target.sorted_tables:
        for column in table.columns:
            col_type = column.type
            if isinstance(col_type, JSONB):
                column.type = JSON()
            elif isinstance(col_type, PG_UUID):
                column.type = GUID()
            elif isinstance(col_type, SA_Uuid):
                column.type = GUID()


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


@pytest.fixture(name="engine")
def fixture_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture(name="seed")
def fixture_seed(engine, monkeypatch) -> dict:
    import src.ai.mcp.server as mcp_server

    @contextmanager
    def _scope() -> Iterator[Session]:
        with Session(engine) as s:
            yield s

    monkeypatch.setattr(mcp_server, "session_scope", _scope)

    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    with Session(engine) as s:
        s.add(
            User(
                id=user_a,
                email="a@example.com",
                name="A",
                hashed_password="x",
                created_at=_now(),
                updated_at=_now(),
            )
        )
        s.add(
            User(
                id=user_b,
                email="b@example.com",
                name="B",
                hashed_password="x",
                created_at=_now(),
                updated_at=_now(),
            )
        )
        s.commit()

        repo_a = Repository(
            id=uuid.uuid4(),
            user_id=user_a,
            github_owner="owner-a",
            github_repo="repo-a",
            default_branch="main",
            clone_url="https://github.com/owner-a/repo-a.git",
            created_at=_now(),
            updated_at=_now(),
        )
        repo_b = Repository(
            id=uuid.uuid4(),
            user_id=user_b,
            github_owner="owner-b",
            github_repo="repo-b",
            default_branch="main",
            clone_url="https://github.com/owner-b/repo-b.git",
            created_at=_now(),
            updated_at=_now(),
        )
        s.add(repo_a)
        s.add(repo_b)
        s.commit()
        s.refresh(repo_a)
        s.refresh(repo_b)

        task_a = Task(
            id=uuid.uuid4(),
            user_id=user_a,
            repository_id=repo_a.id,
            title="task a",
            instruction="do a",
            target_branch="main",
            created_at=_now(),
            updated_at=_now(),
        )
        task_b = Task(
            id=uuid.uuid4(),
            user_id=user_b,
            repository_id=repo_b.id,
            title="task b",
            instruction="do b",
            target_branch="main",
            created_at=_now(),
            updated_at=_now(),
        )
        s.add(task_a)
        s.add(task_b)
        s.commit()
        s.refresh(task_a)
        s.refresh(task_b)

        run_a = AgentRun(
            id=uuid.uuid4(),
            task_id=task_a.id,
            status=RunStatus.succeeded,
            model_id="m",
            prompt_version="v1",
            max_turns=15,
            queued_at=_now(),
        )
        run_b = AgentRun(
            id=uuid.uuid4(),
            task_id=task_b.id,
            status=RunStatus.succeeded,
            model_id="m",
            prompt_version="v1",
            max_turns=15,
            queued_at=_now(),
        )
        s.add(run_a)
        s.add(run_b)
        s.commit()
        s.refresh(run_a)
        s.refresh(run_b)

        s.add(
            AgentRunEvent(
                id=uuid.uuid4(),
                agent_run_id=run_a.id,
                sequence=1,
                event_type=EventType.message,
                payload={"text": "hello a"},
                created_at=_now(),
            )
        )
        s.add(
            AgentRunEvent(
                id=uuid.uuid4(),
                agent_run_id=run_b.id,
                sequence=1,
                event_type=EventType.message,
                payload={"text": "hello b"},
                created_at=_now(),
            )
        )
        s.commit()

        return {
            "user_a": user_a,
            "user_b": user_b,
            "repo_a": repo_a.id,
            "repo_b": repo_b.id,
            "run_a": run_a.id,
            "run_b": run_b.id,
        }


def test_get_repository_context_other_user_denied(seed):
    from src.ai.mcp.server import get_repository_context

    data = get_repository_context(str(seed["repo_b"]), str(seed["user_a"]))
    assert data["found"] is False
    assert "clone_url" not in data


def test_get_repository_context_owner_allowed(seed):
    from src.ai.mcp.server import get_repository_context

    data = get_repository_context(str(seed["repo_a"]), str(seed["user_a"]))
    assert data["found"] is True
    assert data["repository"]["clone_url"] == "https://github.com/owner-a/repo-a.git"


def test_get_agent_run_other_user_denied(seed):
    from src.ai.mcp.server import get_agent_run

    data = get_agent_run(str(seed["run_b"]), str(seed["user_a"]))
    assert data["found"] is False


def test_get_agent_run_owner_allowed(seed):
    from src.ai.mcp.server import get_agent_run

    data = get_agent_run(str(seed["run_a"]), str(seed["user_a"]))
    assert data["found"] is True


def test_list_recent_agent_runs_scoped_to_caller(seed):
    from src.ai.mcp.server import list_recent_agent_runs

    rows = list_recent_agent_runs(str(seed["user_a"]))
    ids = {r["id"] for r in rows}
    assert str(seed["run_a"]) in ids
    assert str(seed["run_b"]) not in ids


def test_list_agent_run_events_other_user_denied(seed):
    from src.ai.mcp.server import list_agent_run_events

    assert list_agent_run_events(str(seed["run_b"]), str(seed["user_a"])) == []


def test_list_agent_run_events_owner_allowed(seed):
    from src.ai.mcp.server import list_agent_run_events

    rows = list_agent_run_events(str(seed["run_a"]), str(seed["user_a"]))
    assert len(rows) == 1
    assert rows[0]["payload"] == {"text": "hello a"}


def test_search_code_other_user_denied_without_vector_call(seed, monkeypatch):
    import asyncio

    import src.ai.mcp.server as mcp_server

    called = False

    async def _fail(query, repository_id, limit):
        nonlocal called
        called = True
        raise AssertionError("vector search must not run for cross-user access")

    monkeypatch.setattr(mcp_server, "_search_code", _fail)
    result = asyncio.run(
        mcp_server.search_code("q", str(seed["repo_b"]), str(seed["user_a"]))
    )
    assert result == []
    assert called is False
