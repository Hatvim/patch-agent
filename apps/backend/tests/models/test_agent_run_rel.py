import datetime
import uuid

from src.models.agent_run import AgentRun
from src.models.enums import RunStatus


def test_agent_run_without_pr_has_none_pull_request(session, task):
    run = AgentRun(
        id=uuid.uuid4(),
        task_id=task.id,
        status=RunStatus.queued,
        model_id="test-model",
        prompt_version="v1",
        max_turns=15,
        queued_at=datetime.datetime.now(datetime.timezone.utc),
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    assert run.pull_request is None
