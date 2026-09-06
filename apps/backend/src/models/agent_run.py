import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Column, DateTime, Enum as SAEnum, ForeignKey, Numeric, Text, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlmodel import Field, Relationship, SQLModel

from src.models.enums import RunRole, RunStatus
from src.models.pull_request import PullRequest

if TYPE_CHECKING:
    from src.models.agent_run_event import AgentRunEvent
    from src.models.task import Task
    from src.models.tool_call import ToolCall


class AgentRun(SQLModel, table=True):
    __tablename__ = "agent_runs"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    task_id: uuid.UUID = Field(
        sa_column=Column(
            PG_UUID(as_uuid=True),
            ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    status: RunStatus = Field(
        sa_column=Column(SAEnum(RunStatus, name="run_status"), nullable=False)
    )
    run_role: RunRole = Field(
        default=RunRole.developer,
        sa_column=Column(
            SAEnum(RunRole, name="run_role"),
            nullable=False,
            server_default="developer",
        ),
    )

    parent_run_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            PG_UUID(as_uuid=True),
            ForeignKey("agent_runs.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )
    follow_up_instruction: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    reviewer_run_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            PG_UUID(as_uuid=True),
            ForeignKey("agent_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    container_id: str | None = Field(default=None, max_length=128)
    container_image: str | None = Field(default=None, max_length=512)
    branch_name: str | None = Field(default=None, max_length=255)
    celery_task_id: str | None = Field(default=None, max_length=128)

    model_id: str = Field(max_length=255)
    prompt_version: str = Field(default="v1", max_length=32, nullable=False)
    max_turns: int = Field(default=15, nullable=False)
    total_tool_calls: int | None = Field(default=None)
    total_tokens: int | None = Field(default=None)
    cost_usd: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(10, 4), nullable=True)
    )
    error_message: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    queued_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), server_default=func.now(), nullable=False
        )
    )
    started_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    finished_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )

    task: "Task" = Relationship(back_populates="agent_runs")
    events: list["AgentRunEvent"] = Relationship(
        back_populates="agent_run",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    tool_calls: list["ToolCall"] = Relationship(
        back_populates="agent_run",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    pull_request: PullRequest | None = Relationship(
        back_populates="agent_run",
        sa_relationship_kwargs={"cascade": "all, delete-orphan", "uselist": False},
    )
