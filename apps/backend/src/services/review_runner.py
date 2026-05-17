"""
Host-side Celery task that runs the automated PR reviewer.

Unlike dispatch_agent_run, the reviewer does NOT need a Docker container — it is
a read-only LLM call (no file writes, no shell exec). It runs directly inside the
Celery worker process for speed.

Flow:
  1. Create an AgentRun row with run_role=reviewer
  2. Link it back to the developer run (developer_run.reviewer_run_id = reviewer_run.id)
  3. Fetch the PR diff from GitHub
  4. Call the reviewer LLM (single structured call, no ReAct loop)
  5. Persist each finding as a review_finding event (Postgres + Redis)
  6. If critical or high findings exist → create a fixer AgentRun + enqueue it
"""
import logging
import uuid
from datetime import datetime, timezone

import httpx
from sqlmodel import Session, select

from src.ai.reviewer import run_review
from src.celery_app import celery_app
from src.core.config import settings
from src.core.database import engine
from src.models.agent_run import AgentRun
from src.models.agent_run_event import AgentRunEvent
from src.models.enums import EventType, RunRole, RunStatus
from src.models.github_credential import GithubCredential
from src.services.credentials import decrypt_token
from src.services.events import publish_run_frame, publish_status_change

logger = logging.getLogger(__name__)

FIX_SEVERITIES = {"critical", "high"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _set_run_status(session: Session, run: AgentRun, status: RunStatus, **kwargs) -> None:
    run.status = status
    for k, v in kwargs.items():
        setattr(run, k, v)
    session.add(run)
    session.commit()


def _emit_review_finding(reviewer_run_id: uuid.UUID, finding: dict, seq: int) -> None:
    """Write a review_finding event to Postgres and push to Redis pub/sub."""
    try:
        with Session(engine) as session:
            session.add(
                AgentRunEvent(
                    agent_run_id=reviewer_run_id,
                    sequence=seq,
                    event_type=EventType.review_finding,
                    payload=finding,
                )
            )
            session.commit()
    except Exception:
        logger.exception("Failed to persist review_finding seq=%s", seq)
    publish_run_frame(str(reviewer_run_id), EventType.review_finding, finding, seq)


def _fetch_pr_diff(github_token: str, owner: str, repo: str, pr_number: int) -> str:
    """Fetch the full unified diff for a PR from GitHub. Returns empty string on failure."""
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files"
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    try:
        resp = httpx.get(url, headers=headers, timeout=15.0)
        resp.raise_for_status()
        files = resp.json()
        parts: list[str] = []
        for f in files:
            patch = f.get("patch")
            if patch:
                parts.append(f"--- a/{f['filename']}\n+++ b/{f['filename']}\n{patch}")
        return "\n\n".join(parts)
    except Exception:
        logger.exception("Failed to fetch PR diff for %s/%s#%s", owner, repo, pr_number)
        return ""


def _format_fix_instruction(findings: list[dict], pr_number: int) -> str:
    lines = [
        f"Fix the following issues found in the automated code review of PR #{pr_number}.",
        "",
        "Make ONLY the changes needed to address these issues. Do not refactor unrelated code.",
        "Run the relevant tests after fixing. Submit a PR update when done.",
        "",
        "Issues to fix:",
    ]
    for i, f in enumerate(findings, 1):
        lines.append(
            f"\n{i}. [{f['severity'].upper()}] {f['file_path']}\n"
            f"   Problem: {f['issue']}\n"
            f"   Fix: {f['suggestion']}"
        )
    return "\n".join(lines)


@celery_app.task(bind=True, name="dispatch_review_run")
def dispatch_review_run(self, developer_run_id: str) -> None:
    dev_uuid = uuid.UUID(developer_run_id)
    reviewer_run_id: uuid.UUID | None = None

    try:
        # --- Load developer run context ---
        with Session(engine) as session:
            dev_run = session.get(AgentRun, dev_uuid)
            if dev_run is None:
                logger.error("Developer run %s not found — skipping review.", developer_run_id)
                return

            task = dev_run.task
            repo = task.repository
            pr = dev_run.pull_request

            if pr is None:
                logger.info("No PR found for run %s — skipping review.", developer_run_id)
                return

            credential = session.exec(
                select(GithubCredential).where(
                    GithubCredential.user_id == task.user_id,
                    GithubCredential.revoked_at.is_(None),  # type: ignore[attr-defined]
                )
            ).first()

            if credential is None:
                logger.warning("No GitHub credential for user %s — skipping review.", task.user_id)
                return

            github_token = decrypt_token(credential)
            pr_number = pr.github_pr_number
            pr_title = pr.title
            owner = repo.github_owner
            repo_name = repo.github_repo
            task_id = task.id
            dev_model_id = dev_run.model_id

        # --- Create reviewer AgentRun ---
        reviewer_model = settings.llm_reviewer_model_id or settings.llm_model_id
        with Session(engine) as session:
            reviewer_run = AgentRun(
                task_id=task_id,
                status=RunStatus.queued,
                run_role=RunRole.reviewer,
                parent_run_id=dev_uuid,
                model_id=reviewer_model,
                prompt_version="reviewer-v1",
                max_turns=1,
            )
            session.add(reviewer_run)
            session.commit()
            session.refresh(reviewer_run)
            reviewer_run_id = reviewer_run.id

        # Link reviewer_run_id back onto the developer run
        with Session(engine) as session:
            dev_row = session.get(AgentRun, dev_uuid)
            if dev_row:
                dev_row.reviewer_run_id = reviewer_run_id
                session.add(dev_row)
                session.commit()

        # --- Set reviewer run to running ---
        with Session(engine) as session:
            reviewer_row = session.get(AgentRun, reviewer_run_id)
            _set_run_status(session, reviewer_row, RunStatus.running, started_at=_now())
        publish_status_change(str(reviewer_run_id), "running", sequence=-1)

        # --- Fetch diff from GitHub ---
        diff_text = _fetch_pr_diff(github_token, owner, repo_name, pr_number)
        if not diff_text:
            logger.warning("Empty diff for PR #%s — reviewer will get no signal.", pr_number)

        # --- Run the review LLM call ---
        findings = run_review(diff_text, pr_title)
        logger.info(
            "Review for run %s: %d finding(s) from PR #%s",
            developer_run_id, len(findings), pr_number,
        )

        # --- Persist findings as review_finding events ---
        for seq, finding in enumerate(findings):
            _emit_review_finding(reviewer_run_id, finding, seq)

        # --- Mark reviewer run succeeded ---
        with Session(engine) as session:
            reviewer_row = session.get(AgentRun, reviewer_run_id)
            _set_run_status(session, reviewer_row, RunStatus.succeeded, finished_at=_now())
        publish_status_change(str(reviewer_run_id), "succeeded", sequence=-2)

        # --- Auto-dispatch fixer if actionable findings exist ---
        actionable = [f for f in findings if f.get("severity") in FIX_SEVERITIES]
        if actionable:
            logger.info(
                "Auto-dispatching fixer for run %s with %d actionable finding(s).",
                developer_run_id, len(actionable),
            )
            fix_instruction = _format_fix_instruction(actionable, pr_number)
            with Session(engine) as session:
                fix_run = AgentRun(
                    task_id=task_id,
                    status=RunStatus.queued,
                    run_role=RunRole.fixer,
                    parent_run_id=dev_uuid,
                    follow_up_instruction=fix_instruction,
                    model_id=dev_model_id,
                    prompt_version="v1",
                    max_turns=15,
                )
                session.add(fix_run)
                session.commit()
                session.refresh(fix_run)
                fix_run_id = fix_run.id

            from src.services.agent_dispatch import enqueue_agent_run  # noqa: PLC0415
            enqueue_agent_run(fix_run_id)

    except Exception as exc:
        logger.exception("dispatch_review_run failed for developer run %s", developer_run_id)
        if reviewer_run_id is not None:
            try:
                with Session(engine) as session:
                    reviewer_row = session.get(AgentRun, reviewer_run_id)
                    if reviewer_row and reviewer_row.status in (RunStatus.queued, RunStatus.running):
                        _set_run_status(
                            session, reviewer_row, RunStatus.failed,
                            finished_at=_now(),
                            error_message=f"Review dispatch error: {exc}",
                        )
            except Exception:
                pass
