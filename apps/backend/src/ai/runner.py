"""
Entrypoint for the P.A.T.C.H. agent Docker container.
Reads env vars, clones the repo, runs the agent loop, persists events and tool calls to Postgres,
publishes every event to Redis, and exits with code 0 on success or 1 on failure.
"""
import asyncio
import logging
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone

from sqlmodel import Session

from src.ai.agent import run_agent
from src.core.config import settings
from src.core.database import engine
from src.core.redaction import redact_text
from src.models.agent_run import AgentRun
from src.models.enums import RunStatus
from src.services.code_indexer import index_repository
from src.services.events import RunEmitter, publish_error, publish_status_change

logger = logging.getLogger(__name__)

WORKSPACE = "/workspace"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _set_run_status(agent_run_id: uuid.UUID, status: RunStatus, **kwargs) -> None:
    with Session(engine) as session:
        run = session.get(AgentRun, agent_run_id)
        if run is None:
            return
        run.status = status
        for key, value in kwargs.items():
            setattr(run, key, value)
        session.add(run)
        session.commit()


def _https_clone_url(repo_clone_url: str) -> str:
    if repo_clone_url.startswith("https://"):
        return repo_clone_url
    path = repo_clone_url.split(":")[-1]
    return f"https://github.com/{path}"


def _git_base_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get(
            "PATH",
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        ),
        "HOME": "/tmp",
        "USER": os.environ.get("USER", "patch"),
        "LOGNAME": os.environ.get("LOGNAME", "patch"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
    }


def _git_auth_env(github_token: str) -> tuple[dict[str, str], str]:
    askpass = tempfile.NamedTemporaryFile("w", delete=False)
    askpass.write(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  *Username*) printf '%s\\n' x-access-token ;;\n"
        "  *) printf '%s\\n' \"$GITHUB_TOKEN\" ;;\n"
        "esac\n"
    )
    askpass.close()
    os.chmod(askpass.name, 0o700)
    env = _git_base_env()
    env["GIT_ASKPASS"] = askpass.name
    env["GITHUB_TOKEN"] = github_token
    return env, askpass.name


def _clone_repo(repo_clone_url: str, github_token: str) -> None:
    clone_url = _https_clone_url(repo_clone_url)
    env, askpass_path = _git_auth_env(github_token)
    try:
        subprocess.run(
            ["git", "clone", clone_url, WORKSPACE],
            check=True,
            timeout=300,
            capture_output=True,
            text=True,
            env=env,
        )
        subprocess.run(
            ["git", "remote", "set-url", "origin", clone_url],
            cwd=WORKSPACE,
            check=True,
            timeout=30,
            capture_output=True,
            text=True,
            env=_git_base_env(),
        )
    finally:
        try:
            os.unlink(askpass_path)
        except OSError:
            pass


def _configure_git_identity() -> None:
    subprocess.run(
        ["git", "config", "user.email", "patch@patch.ai"],
        cwd=WORKSPACE,
        check=True,
        capture_output=True,
        env=_git_base_env(),
    )
    subprocess.run(
        ["git", "config", "user.name", "P.A.T.C.H."],
        cwd=WORKSPACE,
        check=True,
        capture_output=True,
        env=_git_base_env(),
    )


def _checkout_branch(branch: str) -> None:
    subprocess.run(
        ["git", "checkout", branch],
        cwd=WORKSPACE,
        check=True,
        timeout=30,
        capture_output=True,
        text=True,
        env=_git_base_env(),
    )



def _detect_stack(workspace: str) -> str:
    parts = []
    if os.path.exists(os.path.join(workspace, "package.json")):
        parts.append("- Node.js project detected (package.json found). Run `npm test` or similar verification commands if applicable.")
    if os.path.exists(os.path.join(workspace, "pyproject.toml")) or os.path.exists(os.path.join(workspace, "setup.py")):
        if os.path.exists(os.path.join(workspace, "uv.lock")):
            parts.append("- Python project detected with uv.lock. Prefer `uv run pytest ...` for verification; if verification tooling is unavailable, note that in the PR body and continue if the change is otherwise valid.")
        else:
            parts.append("- Python project detected. Suggest using `python -m pytest ...` or `pytest ...` for verification; if verification tooling is unavailable, note that in the PR body and continue if the change is otherwise valid.")
    if os.path.exists(os.path.join(workspace, "go.mod")):
        parts.append("- Go project detected. Verification with `go test ./...` is recommended.")
    if os.path.exists(os.path.join(workspace, "Cargo.toml")):
        parts.append("- Rust project detected. Verification with `cargo test` is recommended.")
    
    if parts:
        return "Stack detection results:\n" + "\n".join(parts)
    return "No known package manager or stack detected. Proceed gracefully."

def _get_commit_sha() -> str | None:
    """Get the current HEAD commit SHA from the workspace."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=WORKSPACE,
            capture_output=True,
            text=True,
            timeout=10,
            env=_git_base_env(),
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


async def main() -> None:
    agent_run_id_str = os.environ["AGENT_RUN_ID"]
    instruction = os.environ["INSTRUCTION"]
    repo_clone_url = os.environ["REPO_CLONE_URL"]
    base_branch = os.environ["BASE_BRANCH"]
    github_token = os.environ["GITHUB_TOKEN"]
    repository_id = os.environ["REPOSITORY_ID"]
    head_branch = os.environ.get("HEAD_BRANCH")
    parent_run_id = os.environ.get("PARENT_RUN_ID")
    follow_up_instruction = os.environ.get("FOLLOW_UP_INSTRUCTION")

    agent_run_id = uuid.UUID(agent_run_id_str)

    follow_up_context: str | None = None
    if parent_run_id and follow_up_instruction:
        with Session(engine) as session:
            parent_run = session.get(AgentRun, uuid.UUID(parent_run_id))
            parent_summary = parent_run.error_message or "(no summary available)" if parent_run else "(parent run not found)"
        follow_up_context = (
            f"You are resuming work on an existing Pull Request.\n\n"
            f"Previous run summary:\n{parent_summary}\n\n"
            f"New follow-up instruction:\n{follow_up_instruction}"
        )
        effective_instruction = follow_up_instruction
    else:
        effective_instruction = instruction

    # Live-only status frames use negative sequences so they don't collide
    # with the RunEmitter's per-event counter.
    live_seq = -1

    def _publish_status(new_status: str) -> None:
        nonlocal live_seq
        publish_status_change(agent_run_id_str, new_status, live_seq)
        live_seq -= 1

    def _publish_error_live(message: str) -> None:
        nonlocal live_seq
        publish_error(agent_run_id_str, message, live_seq)
        live_seq -= 1

    _set_run_status(agent_run_id, RunStatus.running, started_at=_now())
    _publish_status("running")

    emitter = RunEmitter(
        agent_run_id,
        max_steps=settings.agent_max_steps,
        duplicate_streak_limit=settings.agent_duplicate_streak_limit,
    )

    try:
        _clone_repo(repo_clone_url, github_token)
        _configure_git_identity()

        branch_to_checkout = head_branch if head_branch else base_branch
        _checkout_branch(branch_to_checkout)

        stack_context = _detect_stack(WORKSPACE)
        if follow_up_context:
            follow_up_context += "\n\n" + stack_context
        else:
            follow_up_context = stack_context

        # Index repository into vector DB for semantic code search.
        # Non-fatal: if indexing fails, agent still runs with normal tools.
        commit_sha = _get_commit_sha()
        try:
            index_result = await index_repository(
                repository_id=repository_id,
                workspace_path=WORKSPACE,
                commit_sha=commit_sha,
            )
            if index_result.get("ok"):
                logger.info("Repository indexed: %s", index_result)
            else:
                logger.warning("Repository indexing returned error: %s", index_result.get("error"))
        except Exception as exc:
            logger.warning("Repository indexing failed (non-fatal): %s", exc)

        await run_agent(

            emitter=emitter,
            instruction=effective_instruction,
            workspace_path=WORKSPACE,
            repository_id=repository_id,
            branch=branch_to_checkout,
            agent_run_id=agent_run_id_str,
            follow_up_context=follow_up_context,
        )

        _set_run_status(
            agent_run_id,
            RunStatus.succeeded,
            finished_at=_now(),
            total_tool_calls=emitter.total_tool_calls,
        )
        _publish_status("succeeded")

    except subprocess.CalledProcessError as e:
        error_msg = redact_text(f"Git command failed: {e.stderr or str(e)}")
        _set_run_status(agent_run_id, RunStatus.failed, finished_at=_now(), error_message=error_msg)
        _publish_error_live(error_msg)
        _publish_status("failed")
        sys.exit(1)

    except Exception as e:
        error_msg = redact_text(str(e))
        _set_run_status(agent_run_id, RunStatus.failed, finished_at=_now(), error_message=error_msg)
        _publish_error_live(error_msg)
        _publish_status("failed")
        sys.exit(1)

    finally:
        emitter.close()


if __name__ == "__main__":
    asyncio.run(main())
