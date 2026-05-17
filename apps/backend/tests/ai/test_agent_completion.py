import asyncio
from types import SimpleNamespace

import pytest

from src.ai.agent import (
    FINALIZATION_PROMPT,
    IMPLEMENTATION_RECOVERY_PROMPT,
    NO_CHANGE_AFTER_EDIT_PROMPT,
    _is_recoverable_block_after_edit,
    _next_recovery_prompt,
    run_agent,
)


class _Emitter:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.errors: list[str] = []
        self.summaries: list[dict] = []

    def emit_message(self, content: str) -> None:
        self.messages.append(content)

    def emit_error(self, message: str) -> None:
        self.errors.append(message)

    def emit_summary(self, payload: dict) -> None:
        self.summaries.append(payload)


class _StopsWithoutSubmitAgent:
    def __init__(self) -> None:
        self.inputs: list[str] = []

    async def arun(self, input: str, stream: bool = True):
        _ = stream
        self.inputs.append(input)
        yield SimpleNamespace(content="")


class _StopsThenSubmitsAgent:
    def __init__(self, done_flag: dict) -> None:
        self.done_flag = done_flag
        self.inputs: list[str] = []

    async def arun(self, input: str, stream: bool = True):
        _ = stream
        self.inputs.append(input)
        yield SimpleNamespace(content="")
        if len(self.inputs) == 2:
            self.done_flag["done"] = True
            self.done_flag["status"] = "submitted"


class _SubmitsAgent:
    def __init__(self, done_flag: dict) -> None:
        self.done_flag = done_flag

    async def arun(self, input: str, stream: bool = True):
        _ = (input, stream)
        yield SimpleNamespace(content="")
        self.done_flag["done"] = True


class _BlocksAgent:
    def __init__(self, done_flag: dict) -> None:
        self.done_flag = done_flag

    async def arun(self, input: str, stream: bool = True):
        _ = (input, stream)
        self.done_flag["done"] = True
        self.done_flag["status"] = "blocked"
        self.done_flag["reason"] = "The request is ambiguous."
        yield SimpleNamespace(content="")


def test_run_agent_fails_when_stream_ends_without_submit(monkeypatch):
    emitter = _Emitter()
    fake_agent = _StopsWithoutSubmitAgent()
    monkeypatch.setattr(
        "src.ai.agent._build_agent",
        lambda **kwargs: fake_agent,
    )

    with pytest.raises(
        RuntimeError, match="stopped before calling submit_pull_request"
    ):
        asyncio.run(run_agent(emitter=emitter, instruction="Do the task."))

    assert len(fake_agent.inputs) == 3
    assert fake_agent.inputs[1] == IMPLEMENTATION_RECOVERY_PROMPT
    assert emitter.errors == [
        "Agent stopped before calling submit_pull_request; no PR or follow-up artifact was created."
    ]
    assert emitter.summaries == []


def test_run_agent_finalization_retry_can_submit(monkeypatch):
    emitter = _Emitter()

    def fake_build_agent(**kwargs):
        return _StopsThenSubmitsAgent(kwargs["done_flag"])

    monkeypatch.setattr("src.ai.agent._build_agent", fake_build_agent)

    asyncio.run(run_agent(emitter=emitter, instruction="Do the task."))

    assert emitter.errors == []
    assert len(emitter.summaries) == 1


def test_run_agent_succeeds_after_submit_sentinel(monkeypatch):
    emitter = _Emitter()

    def fake_build_agent(**kwargs):
        return _SubmitsAgent(kwargs["done_flag"])

    monkeypatch.setattr("src.ai.agent._build_agent", fake_build_agent)

    asyncio.run(run_agent(emitter=emitter, instruction="Do the task."))

    assert emitter.errors == []
    assert len(emitter.summaries) == 1


def test_run_agent_fails_cleanly_after_blocked_sentinel(monkeypatch):
    emitter = _Emitter()

    def fake_build_agent(**kwargs):
        return _BlocksAgent(kwargs["done_flag"])

    monkeypatch.setattr("src.ai.agent._build_agent", fake_build_agent)

    with pytest.raises(RuntimeError, match="Agent marked the task as blocked"):
        asyncio.run(run_agent(emitter=emitter, instruction="Do the task."))

    assert emitter.errors == [
        "Agent marked the task as blocked: The request is ambiguous."
    ]
    assert emitter.summaries == []


def test_recovery_prompt_continues_implementation_without_edits(monkeypatch):
    monkeypatch.setattr("src.ai.agent._workspace_has_changes", lambda workspace: False)

    prompt = _next_recovery_prompt({"edit_succeeded": False}, "/workspace")

    assert prompt == IMPLEMENTATION_RECOVERY_PROMPT
    assert "Do not call git diff or submit_pull_request" in prompt


def test_recovery_prompt_handles_noop_successful_edit(monkeypatch):
    monkeypatch.setattr("src.ai.agent._workspace_has_changes", lambda workspace: False)

    prompt = _next_recovery_prompt({"edit_succeeded": True}, "/workspace")

    assert prompt == NO_CHANGE_AFTER_EDIT_PROMPT
    assert "git status shows no repository changes" in prompt


def test_recovery_prompt_finalizes_when_workspace_has_changes(monkeypatch):
    monkeypatch.setattr("src.ai.agent._workspace_has_changes", lambda workspace: True)

    prompt = _next_recovery_prompt({"edit_succeeded": False}, "/workspace")

    assert prompt == FINALIZATION_PROMPT


def test_recovery_prompt_finalizes_after_successful_edit_with_changes(monkeypatch):
    monkeypatch.setattr("src.ai.agent._workspace_has_changes", lambda workspace: True)

    prompt = _next_recovery_prompt({"edit_succeeded": True}, "/workspace")

    assert prompt == FINALIZATION_PROMPT
    assert 'exec_command("git status --porcelain")' in prompt
    assert "untracked file" in prompt


def test_recoverable_blocker_detects_missing_verifier_and_empty_diff_confusion():
    assert _is_recoverable_block_after_edit("pytest: command not found")
    assert _is_recoverable_block_after_edit("git --no-pager diff returns no changes")
    assert _is_recoverable_block_after_edit(
        "The repository already satisfies the requested change and git status is clean."
    )
    assert not _is_recoverable_block_after_edit("The request is ambiguous.")
