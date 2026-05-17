"""
P.A.T.C.H. agent — ReAct-style reasoning over a Codex-style 4-tool surface.

Tools exposed to the model:
  - get_current_agent_run_context()
  - get_current_repository_context(limit=20)
  - list_current_agent_run_events(limit=50)
  - list_recent_repository_runs(limit=20)
  - search_code(query, limit=8)
  - exec_command(command, cwd="/workspace", timeout=60, background=False)
  - write_stdin(pid, data, expect_exit=False, timeout=30)
  - patch_file(path, diff)
  - submit_pull_request(title, body)
  - mark_blocked(reason)

Reasoning: agno's `reasoning=True` enables ReAct-style think/act/observe loops.
Observability: Langfuse via OpenTelemetry OTLP (LiteLLMInstrumentor).
"""

import asyncio
import base64
import os
import subprocess
import threading
import time
from collections.abc import Coroutine
from typing import Any, Callable

from agno.agent import Agent
from agno.models.litellm import LiteLLM
from agno.tools import tool

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from openinference.instrumentation.litellm import LiteLLMInstrumentor

from src.core.config import settings
from src.core.redaction import redact_value
from src.ai.mcp.server import (
    get_agent_run as mcp_get_agent_run,
    get_repository_context as mcp_get_repository_context,
    list_agent_run_events as mcp_list_agent_run_events,
    list_recent_agent_runs as mcp_list_recent_agent_runs,
    search_code as mcp_search_code,
)
from src.ai.tools import (
    exec_command,
    write_stdin,
    patch_file,
    write_file,
    submit_pull_request,
)
from src.services.events import RunawayAgentError, RunEmitter

# --- Langfuse / OTLP tracing setup ---
if (
    settings.langfuse_host
    and settings.langfuse_public_key
    and settings.langfuse_secret_key
):
    langfuse_auth = base64.b64encode(
        f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()
    ).decode()
    exporter = OTLPSpanExporter(
        endpoint=f"{settings.langfuse_host.rstrip('/')}/api/public/otel/v1/traces",
        headers={"Authorization": f"Basic {langfuse_auth}"},
    )
    resource = Resource.create({"service.name": "patch-agent"})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    LiteLLMInstrumentor().instrument(tracer_provider=provider)
tracer = trace.get_tracer(__name__)

# Mirror the LLM key into the provider-specific env var LiteLLM reads from.
_provider_prefix = (
    settings.llm_model_id.split("/", 1)[0].upper()
    if "/" in settings.llm_model_id
    else "OPENAI"
)
os.environ[f"{_provider_prefix}_API_KEY"] = settings.llm_api_key

WORKSPACE = "/workspace"
FINALIZATION_PROMPT = """\
You stopped after making repository changes but before finalizing the run.
The host already detected pending repository work. That can be either workspace
changes from `git status --porcelain` or a local commit that was created before
a previous push failed. New untracked files appear in status as `??` and may not
show up in `git diff` until staged.

You MUST make exactly one of these final tool-call paths now:
1. Call exec_command("git status --porcelain") to list all changed files.
2. If status is not empty, optionally call exec_command("git --no-pager diff --stat")
   or exec_command("git --no-pager diff") to review tracked-file changes. If diff
   output is empty but status contains `??`, that means the change is a new
   untracked file; it is NOT a no-op.
3. If status is empty after a previous submit attempt failed, still call
   submit_pull_request(title, body). The change may already be committed locally
   and only need another push/upsert attempt.
4. Call submit_pull_request(title, body). If verification could not run because
   a command such as pytest was unavailable, mention that in the PR body.
5. Only call mark_blocked(reason) if git status is clean, there is no previous
   failed submit attempt to retry, and the current changes are known to be
   invalid/unsafe.

Do not answer in prose. Do not stop without calling submit_pull_request or mark_blocked.
"""
IMPLEMENTATION_RECOVERY_PROMPT = """\
You stopped before changing the repository.

You MUST continue the implementation now:
1. If you know the correct file and edit, call patch_file(path, diff) or write_file(path, content).
2. If you still need one precise piece of context, call exactly one read/search tool, then edit.
3. If the task is impossible, unsafe, or ambiguous, call mark_blocked(reason).

Do not call git diff or submit_pull_request until after a write_file or patch_file call succeeds.
Do not answer in prose.
"""
NO_CHANGE_AFTER_EDIT_PROMPT = """\
Your edit tool returned successfully, but git status shows no repository changes.

You MUST resolve this no-op edit now:
1. If the edit targeted the wrong content or wrote identical content, call patch_file(path, diff)
   or write_file(path, content) with a real repository change.
2. If the requested change is already present and no code change is needed, call mark_blocked(reason)
   and explain that the repository already satisfies the task.
3. If the task is impossible, unsafe, or ambiguous, call mark_blocked(reason).

Do not call git diff or submit_pull_request while git status is clean.
Do not answer in prose.
"""
FINALIZATION_RETRY_LIMIT = 2

SYSTEM_PROMPT = """\
You are P.A.T.C.H. — an autonomous coding agent in a sandboxed Linux container.
The user's repository is already cloned at /workspace. You modify that real repository
and finalize by opening a Pull Request.

# Operating rule (non-negotiable)

You DO NOT chat. You DO NOT answer with prose, code blocks, or explanations.
Every action you take is a tool call. The ONLY way to make progress is to call
one of the tools below. If you respond without calling a tool, nothing happens
and the task fails.

# Procedure context tools (read-only)

1. get_current_agent_run_context()
   Read the current PATCH agent run, task instruction, repository metadata,
   status, pull request metadata, and recorded tool calls.

2. get_current_repository_context(limit=20)
   Read repository metadata plus recent tasks and recent agent runs for this repo.

3. list_current_agent_run_events(limit=50)
   Read the current run's ordered event stream.

4. list_recent_repository_runs(limit=20)
   Read recent runs for this repository.

# Repository action tools

5. search_code(query, limit=8)
   Semantic code search over the indexed repository. Returns file paths,
   line ranges, language, and content previews for the most relevant code chunks.
   USE THIS before editing files to find the right location semantically.
   Example: search_code("auth refresh token session renewal")

6. exec_command(command, cwd="/workspace", timeout=60, background=False)
   Runs any shell command in a PTY. Use this for EVERYTHING except editing files and
   submitting the PR:
   - search:   rg 'pattern' -n           rg --type py 'def foo'        find . -name '*.ts'
   - structural: ast-grep run -p 'function $A() { $$$ }' -l ts
   - read:     cat path/to/file          head -n 50 file               sed -n '120,180p' file
   - test/lint: uv run pytest -q         python -m pytest              ruff check .     npm test
   - git:      git status --porcelain    git --no-pager diff           git log --oneline -10
   - inspect:  ls -la                    tree -L 2                     which python
   The cwd MUST be `/workspace` or a real subdirectory under `/workspace`.
   Never put a question, explanation, plan, or user-facing sentence in `command` or `cwd`.

7. write_stdin(pid, data, expect_exit=False, timeout=30)
   Feed input to a backgrounded exec_command. Most tasks don't need this.

8. write_file(path, content)
   Write full file content to `path` (creates parent dirs). USE THIS for:
   - NEW files (any task creating a file that doesn't exist)
   - Full rewrites where >50% of the file changes
   Returns {ok, path, bytes_written}.

9. patch_file(path, diff)
   Apply a targeted patch to an EXISTING file. Accepts unified diffs with
   ---/+++ headers, numbered hunk-only diffs beginning with @@, and Codex-style
   *** Begin Patch / *** Update File blocks. Tries `git apply --recount` first,
   then falls back to `patch -F3` (fuzz 3).
   ONLY use for surgical edits to existing files. ALWAYS `exec_command("cat path")` FIRST
   so context lines are accurate. If patch_file fails with "malformed patch",
   switch to write_file with the full intended file content.

10. submit_pull_request(title, body)
   FINAL step. Stages, commits, pushes a new branch, opens (or updates) the PR.
   Call this exactly once after you have made edits and checked `git status --porcelain`.

11. mark_blocked(reason)
   FINAL failure step. Use this when the request is ambiguous, impossible, unsafe,
   or blocked by missing information. Do not use exec_command to ask questions.
   Do not call submit_pull_request when there are no changes.

# Required workflow

For every task, no exceptions:
  1. get_current_agent_run_context() to load PATCH task/run context.
  2. get_current_repository_context() to load repo-level PATCH context.
  3. search_code("<semantic description of the task>") to find relevant code locations.
  4. exec_command("ls -la") to see what's in the repo.
  5. (Only if needed) exec_command("cat README.md") and/or exec_command("rg 'KEYWORD' -n") to locate code.
  6. (Only if editing existing file) exec_command("cat path/to/file") to read full context.
  7. Apply the edit:
       - NEW file or full rewrite: write_file(path, content)
       - surgical edit to existing file: patch_file(path, diff)
  8. Verify ONLY if it matches the repo. See "Verification rules" below.
  9. Review status: exec_command("git status --porcelain") before PR submission.
     Then optionally call exec_command("git --no-pager diff --stat") or
     exec_command("git --no-pager diff"). Remember: untracked new files appear
     in status as `??` and may not appear in diff until staged.
  10. submit_pull_request(title, body) — STOP HERE. Do not call any more tools after this.

Inspection budget: after at most 6 exec_command calls, you must either edit with
write_file/patch_file or call mark_blocked(reason). Do not keep reading files once
you know where to make the change.

# Verification rules (very important — do NOT run irrelevant tests)

Only run a verification command if the repo clearly supports it:
  - Python tests ONLY if there's a `pyproject.toml` / `setup.py` / `tests/` dir
    AND a python project. If `uv.lock` exists, prefer `uv run pytest ...`.
    If bare `pytest` is unavailable, try `python -m pytest ...` or skip
    verification and mention it in the PR body.
  - `npm test` / `npm run build` ONLY if there's a `package.json` with a matching script.
  - `cargo test` ONLY if there's a `Cargo.toml`.
  - `go test ./...` ONLY if there's a `go.mod`.
  - For pure HTML/CSS/static-asset changes with NO test framework in the repo,
    DO NOT run any test command. Just verify the file was written
    (e.g. `exec_command("cat path/to/file | head -30")`) and proceed to submit.

If `ls -la` only shows static files (index.html, css/, etc.) and no package manager
config, the right verification is: read back the file you wrote. Then submit.

If a verification command is missing (`command not found`, module not installed,
or equivalent) after you have made valid repository changes, do NOT call
mark_blocked solely for that. Check `git status --porcelain`, then submit the PR
with a note that verification could not be run.

# After submit_pull_request

Once `submit_pull_request` returns `{ok: true, ...}` or a successful status,
your job is DONE.
Do NOT call any further tools. Do NOT run more tests. Do NOT add commits.
The next message in this stream from you would be ignored — STOP.

# If blocked or ambiguous

Do not ask a clarification question in prose. Do not place clarification text in
exec_command arguments. Call mark_blocked(reason) with a concise explanation.

# Hard rules

- NEVER write or read files outside /workspace.
- NEVER read .env, .pem, .key files.
- NEVER inspect environment variables, /proc/*/environ, .git internals, SSH keys,
  or credential helper configuration.
- Network-capable shell commands and dependency installs are blocked unless an
  administrator explicitly enables shell network access for the run. Even then,
  only deterministic Node installs are allowed: `npm ci --ignore-scripts` or
  `pnpm install --frozen-lockfile --ignore-scripts`.
- Keep changes minimal and focused. No drive-by refactors.
- If the task is impossible, ambiguous, or blocked, call mark_blocked(reason).
- New-file tasks: create the file in a sensible directory under /workspace
  (e.g. examples/, demo/, scripts/, public/). Don't ask where to put it — pick a path
  by inspecting the repo layout with `ls` first.

# Termination

There are exactly two legitimate ways to end this run:
1. Success: call submit_pull_request after making a real change.
2. Failure/blocker: call mark_blocked(reason).
Do NOT just stop emitting tool calls — that wastes budget and produces no artifact
for the user.
"""


def _truncate_for_model(result: Any, limit: int) -> Any:
    """Return a copy of `result` with any large string field shortened so the
    model's transcript doesn't balloon. The original `result` is still persisted
    untruncated via `emit_tool_result`, so the UI can show the full output."""
    if not isinstance(result, dict) or limit <= 0:
        return result
    out = dict(result)
    for k, v in list(out.items()):
        if isinstance(v, str) and len(v) > limit:
            half = max(limit // 2, 200)
            dropped = len(v) - half * 2
            out[k] = f"{v[:half]}\n…[truncated {dropped} chars]…\n{v[-half:]}"
    return out


def _run_coroutine_sync(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run an async MCP procedure from a sync tool function.

    Agno tools are defined synchronously here, but the agent loop itself is async.
    If a loop is already running in this thread, run the coroutine in a short-lived
    helper thread so we do not call asyncio.run() from inside an active loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, Any] = {}

    def _target() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=_target)
    thread.start()
    thread.join()

    if "error" in result:
        raise result["error"]
    return result.get("value")


def _git_probe_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get(
            "PATH",
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        ),
        "HOME": "/tmp",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
    }


def _workspace_has_changes(workspace_path: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=_git_probe_env(),
        )
    except Exception:
        return False
    if result.returncode == 0 and result.stdout.strip():
        return True

    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "--branches", "--not", "--remotes"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=_git_probe_env(),
        )
    except Exception:
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def _is_recoverable_block_after_edit(reason: str) -> bool:
    """Return true for common false blockers after a valid edit exists."""
    normalized = reason.lower()
    missing_verifier = any(
        phrase in normalized
        for phrase in (
            "command not found",
            "not available",
            "not installed",
            "no module named",
        )
    ) and any(
        tool in normalized
        for tool in (
            "pytest",
            "ruff",
            "mypy",
            "tsc",
            "npm",
            "pnpm",
            "yarn",
            "cargo",
            "go test",
        )
    )
    empty_diff_confusion = (
        "git" in normalized
        and ("diff" in normalized or "status" in normalized)
        and any(
            phrase in normalized
            for phrase in (
                "already satisfies",
                "already satisfied",
                "no changes",
                "no change",
                "empty",
                "clean",
                "no further modifications",
                "returns no",
                "returned no",
            )
        )
    )
    return missing_verifier or empty_diff_confusion


def _next_recovery_prompt(done_flag: dict, workspace_path: str) -> str:
    if _workspace_has_changes(workspace_path):
        return FINALIZATION_PROMPT
    if done_flag.get("edit_succeeded"):
        return NO_CHANGE_AFTER_EDIT_PROMPT
    return IMPLEMENTATION_RECOVERY_PROMPT


def _instrumented(
    emitter: RunEmitter | None, tool_name: str, fn: Callable[..., Any], kwargs: dict
) -> Any:
    """Run a tool, emitting tool_call before and tool_result after.
    Captures duration and success/error so the live stream mirrors Langfuse.
    Returns a possibly-truncated copy of the result for the model, while emitting
    the full result to the DB/UI."""
    if emitter is None:
        return fn(**kwargs)
    emitter.register_tool_call(tool_name, kwargs)
    emitter.emit_tool_call(tool_name, kwargs)
    start = time.monotonic()
    try:
        result = fn(**kwargs)
    except Exception as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        emitter.emit_tool_result(tool_name, {}, "error", duration_ms, str(exc))
        raise
    duration_ms = int((time.monotonic() - start) * 1000)
    is_error = isinstance(result, dict) and (
        result.get("ok") is False or result.get("error") is not None
    )
    redacted_result = redact_value(result)
    emitter.emit_tool_result(
        tool_name,
        redacted_result,
        "error" if is_error else "success",
        duration_ms,
        error_message=(
            result.get("error") if is_error and isinstance(result, dict) else None
        ),
    )
    return _truncate_for_model(redacted_result, settings.agent_max_tool_output_chars)


def _build_agent(
    instructions: str = SYSTEM_PROMPT,
    done_flag: dict | None = None,
    emitter: RunEmitter | None = None,
    agent_run_id: str = "",
    repository_id: str = "",
    workspace_path: str = WORKSPACE,
) -> Agent:
    model_kwargs: dict = {
        "id": settings.llm_model_id,
        "api_key": settings.llm_api_key,
        "temperature": 0.0,
        "request_params": {"tool_choice": "required"},
    }
    if settings.llm_base_url:
        model_kwargs["api_base"] = settings.llm_base_url
    model = LiteLLM(**model_kwargs)

    @tool(name="get_current_agent_run_context")
    def tool_get_current_agent_run_context() -> dict:
        """Read-only MCP procedure: return current PATCH agent run, task, repo, PR, and tool-call context."""
        if not agent_run_id:
            return {"ok": False, "error": "agent_run_id is unavailable"}
        return _instrumented(
            emitter,
            "get_current_agent_run_context",
            mcp_get_agent_run,
            {"run_id": agent_run_id},
        )

    @tool(name="get_current_repository_context")
    def tool_get_current_repository_context(limit: int = 20) -> dict:
        """Read-only MCP procedure: return repository metadata plus recent tasks and agent runs."""
        if not repository_id:
            return {"ok": False, "error": "repository_id is unavailable"}
        return _instrumented(
            emitter,
            "get_current_repository_context",
            mcp_get_repository_context,
            {"repository_id": repository_id, "limit": limit},
        )

    @tool(name="list_current_agent_run_events")
    def tool_list_current_agent_run_events(limit: int = 50) -> dict:
        """Read-only MCP procedure: return ordered event frames for the current PATCH run."""
        if not agent_run_id:
            return {"ok": False, "error": "agent_run_id is unavailable"}

        def _list_events(run_id: str, limit: int) -> dict:
            return {"events": mcp_list_agent_run_events(run_id=run_id, limit=limit)}

        return _instrumented(
            emitter,
            "list_current_agent_run_events",
            _list_events,
            {"run_id": agent_run_id, "limit": limit},
        )

    @tool(name="list_recent_repository_runs")
    def tool_list_recent_repository_runs(limit: int = 20) -> dict:
        """Read-only MCP procedure: return recent PATCH agent runs for the current repository."""
        if not repository_id:
            return {"ok": False, "error": "repository_id is unavailable"}

        def _list_runs(repository_id: str, limit: int) -> dict:
            return {
                "runs": mcp_list_recent_agent_runs(
                    limit=limit,
                    repository_id=repository_id,
                )
            }

        return _instrumented(
            emitter,
            "list_recent_repository_runs",
            _list_runs,
            {"repository_id": repository_id, "limit": limit},
        )

    @tool(name="exec_command")
    def tool_exec_command(
        command: str,
        cwd: str = WORKSPACE,
        timeout: int = 60,
        background: bool = False,
    ) -> dict:
        """Run a shell command in /workspace via PTY. Returns {output, exit_code, timed_out} or {pid, started} if background."""
        return _instrumented(
            emitter,
            "exec_command",
            exec_command,
            {
                "command": command,
                "cwd": cwd,
                "timeout": timeout,
                "background": background,
            },
        )

    @tool(name="search_code")
    def tool_search_code(query: str, limit: int = 8) -> dict:
        """Semantic code search over the indexed repository. Returns file paths, line ranges, language, and content previews for the most relevant code chunks."""
        if not repository_id:
            return {"ok": False, "error": "repository_id is unavailable"}

        def _search(query: str, repository_id: str, limit: int) -> dict:
            try:
                results = _run_coroutine_sync(
                    mcp_search_code(
                        query=query, repository_id=repository_id, limit=limit
                    )
                )
                return {"ok": True, "results": results}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        return _instrumented(
            emitter,
            "search_code",
            _search,
            {"query": query, "repository_id": repository_id, "limit": limit},
        )

    @tool(name="write_stdin")
    def tool_write_stdin(
        pid: int,
        data: str,
        expect_exit: bool = False,
        timeout: int = 30,
    ) -> dict:
        """Write data + newline to a backgrounded process; drain new output. Returns {output, exited, exit_code}."""
        return _instrumented(
            emitter,
            "write_stdin",
            write_stdin,
            {"pid": pid, "data": data, "expect_exit": expect_exit, "timeout": timeout},
        )

    @tool(name="patch_file")
    def tool_patch_file(path: str, diff: str) -> dict:
        """Apply a targeted patch to an EXISTING file under /workspace. Accepts unified, hunk-only, or Codex-style update patches. Use write_file for new files."""
        result = _instrumented(
            emitter, "patch_file", patch_file, {"path": path, "diff": diff}
        )
        if isinstance(result, dict) and result.get("ok") is True and done_flag is not None:
            done_flag["edit_succeeded"] = True
            done_flag.setdefault("edited_paths", []).append(path)
        return result

    @tool(name="write_file")
    def tool_write_file(path: str, content: str) -> dict:
        """Write full file content to path under /workspace. Use for new files or full rewrites — no diff needed."""
        result = _instrumented(
            emitter, "write_file", write_file, {"path": path, "content": content}
        )
        if isinstance(result, dict) and result.get("ok") is True and done_flag is not None:
            done_flag["edit_succeeded"] = True
            done_flag.setdefault("edited_paths", []).append(path)
        return result

    @tool(name="submit_pull_request", stop_after_tool_call=True)
    def tool_submit_pull_request(title: str, body: str) -> dict:
        """FINAL step. Commit, push, open (or update) the Pull Request. The agent MUST stop after this returns ok=true."""
        try:
            result = _instrumented(
                emitter,
                "submit_pull_request",
                submit_pull_request,
                {"title": title, "body": body},
            )
        except Exception as exc:
            if done_flag is not None:
                done_flag["submit_failed"] = True
                done_flag["submit_error"] = str(exc)
            raise
        is_done = (
            isinstance(result, dict)
            and result.get("error") is None
            and (
                result.get("ok") is True
                or result.get("status")
                in {"pr_created", "pr_recovered", "follow_up_committed"}
            )
        )
        if done_flag is not None and is_done:
            done_flag["done"] = True
            done_flag["status"] = "submitted"
        return result

    @tool(name="mark_blocked", stop_after_tool_call=True)
    def tool_mark_blocked(reason: str) -> dict:
        """FINAL failure step. Use when the request is ambiguous, impossible, unsafe, or blocked. Do not submit a PR."""
        if (
            done_flag is not None
            and done_flag.get("edit_succeeded")
            and _workspace_has_changes(workspace_path)
            and _is_recoverable_block_after_edit(reason)
        ):
            done_flag["blocked_retry_reason"] = reason
            return _instrumented(
                emitter,
                "mark_blocked",
                lambda reason: {
                    "status": "needs_finalization",
                    "reason": reason,
                    "next_step": (
                        "Workspace changes exist. Missing verification commands or an empty "
                        "git diff for untracked files are not blockers. Check git status and "
                        "submit the pull request with a verification note."
                    ),
                },
                {"reason": reason},
            )
        result = _instrumented(
            emitter,
            "mark_blocked",
            lambda reason: {"status": "blocked", "reason": reason},
            {"reason": reason},
        )
        if done_flag is not None:
            done_flag["done"] = True
            done_flag["status"] = "blocked"
            done_flag["reason"] = reason
        return result

    return Agent(
        name="P.A.T.C.H.",
        instructions=instructions,
        model=model,
        tools=[
            tool_get_current_agent_run_context,
            tool_get_current_repository_context,
            tool_list_current_agent_run_events,
            tool_list_recent_repository_runs,
            tool_search_code,
            tool_exec_command,
            tool_write_stdin,
            tool_write_file,
            tool_patch_file,
            tool_submit_pull_request,
            tool_mark_blocked,
        ],
        tool_choice="required",
    )


async def run_agent(
    emitter: RunEmitter,
    instruction: str,
    workspace_path: str = WORKSPACE,
    repository_id: str = "",
    branch: str = "",
    agent_run_id: str = "",
    follow_up_context: str | None = None,
) -> None:
    """Run the agent to completion. Every observable action (assistant text, tool call,
    tool result, error) is emitted via `emitter` — which writes to DB and Redis — so the
    UI live-stream matches what Langfuse traces."""
    agent_instructions = SYSTEM_PROMPT
    _ = branch  # reserved for future use; callers already pass it
    if follow_up_context:
        agent_instructions += f"\n\n# Follow-up Context\n{follow_up_context}"

    done_flag: dict = {
        "done": False,
        "status": None,
        "reason": None,
        "edit_succeeded": False,
        "edited_paths": [],
    }
    agent = _build_agent(
        instructions=agent_instructions,
        done_flag=done_flag,
        emitter=emitter,
        agent_run_id=agent_run_id,
        repository_id=repository_id,
        workspace_path=workspace_path,
    )

    with tracer.start_as_current_span(f"run_{agent_run_id}") as span:
        span.set_attribute("instruction", instruction)
        span.set_attribute("input.value", instruction)
        span.set_attribute("gen_ai.prompt", instruction)
        collected_output: list[str] = []
        # Buffer text deltas instead of emitting one frame per token. We flush
        # only on logical boundaries (sentence/newline or >200 chars) so the
        # timeline shows readable assistant messages, not "Pull / request /
        # created / successfully / ." spam.
        text_buffer: list[str] = []

        def _flush_text() -> None:
            if not text_buffer:
                return
            emitter.emit_message("".join(text_buffer).strip())
            text_buffer.clear()

        def _buffer_text(chunk: str) -> None:
            # Just accumulate. Agno streams content token-by-token, and any
            # mid-stream flush risks splitting a single assistant message into
            # word-sized fragments in the timeline. We flush exactly once at
            # end-of-stream (or in error/timeout handlers), producing one
            # coherent message frame per assistant turn.
            text_buffer.append(chunk)

        async def _run_agent_turn(input_text: str) -> None:
            async for event in agent.arun(input=input_text, stream=True):  # type: ignore[attr-defined]
                content = getattr(event, "content", None)
                if content:
                    collected_output.append(content)
                    _buffer_text(content)
                if done_flag["done"]:
                    break

        try:
            async with asyncio.timeout(settings.agent_max_wall_time_sec):
                await _run_agent_turn(instruction)
                for _ in range(FINALIZATION_RETRY_LIMIT):
                    if done_flag["done"]:
                        break
                    _flush_text()
                    await _run_agent_turn(_next_recovery_prompt(done_flag, workspace_path))
            _flush_text()

            if not done_flag["done"]:
                raise RuntimeError(
                    "Agent stopped before calling submit_pull_request; no PR or follow-up artifact was created."
                )

            if done_flag["status"] == "blocked":
                reason = str(
                    done_flag.get("reason") or "Agent marked the task as blocked."
                )
                raise RuntimeError(f"Agent marked the task as blocked: {reason}")

            final_output = "".join(collected_output)[:8000]
            span.set_attribute("output.value", final_output)
            span.set_attribute("gen_ai.completion", final_output)
            emitter.emit_summary({"agent_run_id": agent_run_id, "output": final_output})
            span.set_attribute("status", "completed")

        except asyncio.TimeoutError:
            _flush_text()
            span.set_attribute("status", "timeout")
            emitter.emit_error(
                f"Agent exceeded wall-time limit of {settings.agent_max_wall_time_sec}s"
            )
            raise

        except RunawayAgentError as exc:
            _flush_text()
            span.set_attribute("status", "aborted")
            span.record_exception(exc)
            emitter.emit_error(str(exc))
            raise

        except Exception as exc:
            _flush_text()
            span.set_attribute("status", "error")
            span.record_exception(exc)
            emitter.emit_error(str(exc))
            raise
