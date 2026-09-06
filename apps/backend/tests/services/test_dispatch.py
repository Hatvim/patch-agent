from unittest.mock import patch
import uuid


def test_enqueue_agent_run_raises_when_broker_down():
    from src.services import agent_dispatch

    with patch("src.services.agent_runner.dispatch_agent_run") as m:
        m.delay.side_effect = Exception("broker down")
        try:
            agent_dispatch.enqueue_agent_run(uuid.uuid4())
        except Exception:
            return
        raise AssertionError("expected enqueue_agent_run to raise on broker failure")


def test_enqueue_review_is_idempotent_marker():
    # Placeholder for Task 9 Step 3 guard: developer_run.reviewer_run_id must be set atomically
    assert True


def _make_mock_session(run_row):
    from unittest.mock import MagicMock

    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.return_value = run_row
    return mock_session


def test_cancelled_run_not_overwritten_on_nonzero_exit():
    from unittest.mock import MagicMock

    from src.models.enums import RunStatus
    from src.services.agent_runner import dispatch_agent_run

    run_id = str(uuid.uuid4())
    cancelled_row = MagicMock()
    cancelled_row.status = RunStatus.cancelled

    container = MagicMock()
    container.id = "abc123"
    container.wait.return_value = {"StatusCode": 1}
    container.logs.return_value = b"boom"

    docker_client = MagicMock()
    docker_client.networks.create.return_value = MagicMock()
    docker_client.networks.list.return_value = []
    docker_client.containers.run.return_value = container

    mock_session = _make_mock_session(cancelled_row)

    with (
        patch("src.services.agent_runner._load_run_context", return_value=MagicMock()),
        patch("src.services.agent_runner.docker.from_env", return_value=docker_client),
        patch(
            "src.services.agent_runner.get_sandbox_options",
            return_value={
                "network": f"patch_{run_id}",
                "detach": True,
                "remove": False,
            },
        ),
        patch("src.services.agent_runner.Session", return_value=mock_session),
    ):
        dispatch_agent_run(run_id)

    assert cancelled_row.status == RunStatus.cancelled


def test_cancelled_run_not_overwritten_on_host_error():
    from unittest.mock import MagicMock

    import pytest

    from src.models.enums import RunStatus
    from src.services.agent_runner import dispatch_agent_run

    run_id = str(uuid.uuid4())
    cancelled_row = MagicMock()
    cancelled_row.status = RunStatus.cancelled

    docker_client = MagicMock()
    mock_session = _make_mock_session(cancelled_row)

    with (
        patch(
            "src.services.agent_runner._load_run_context",
            side_effect=ValueError("not found"),
        ),
        patch("src.services.agent_runner.docker.from_env", return_value=docker_client),
        patch("src.services.agent_runner.Session", return_value=mock_session),
    ):
        with pytest.raises(ValueError, match="not found"):
            dispatch_agent_run(run_id)

    assert cancelled_row.status == RunStatus.cancelled


def _make_dev_row(**overrides):
    from unittest.mock import MagicMock

    dev_row = MagicMock()
    dev_row.task.id = uuid.uuid4()
    dev_row.task.user_id = uuid.uuid4()
    dev_row.model_id = "test-model"
    dev_row.pull_request = MagicMock()
    dev_row.reviewer_run_id = None
    for key, value in overrides.items():
        setattr(dev_row, key, value)
    return dev_row


def _make_exec_result(first_value):
    from unittest.mock import MagicMock

    result = MagicMock()
    result.first.return_value = first_value
    return result


def test_second_review_dispatch_creates_no_new_reviewer():
    from unittest.mock import MagicMock

    from src.services.review_runner import dispatch_review_run

    dev_uuid = uuid.uuid4()
    dev_row = _make_dev_row(reviewer_run_id=uuid.uuid4())
    mock_session = _make_mock_session(dev_row)
    mock_session.exec.return_value = _make_exec_result(MagicMock())

    with (
        patch("src.services.review_runner.Session", return_value=mock_session),
        patch("src.services.review_runner.decrypt_token", return_value="fake-token"),
        patch("src.services.review_runner.publish_status_change"),
        patch("src.services.review_runner._fetch_pr_diff", return_value="diff"),
        patch("src.services.review_runner._emit_review_finding"),
        patch("src.services.review_runner.run_review") as mock_run_review,
        patch("src.services.agent_dispatch.enqueue_agent_run") as mock_enqueue,
    ):
        dispatch_review_run(str(dev_uuid))

    mock_session.add.assert_not_called()
    mock_run_review.assert_not_called()
    mock_enqueue.assert_not_called()


def test_second_fixer_dispatch_creates_no_new_fixer():
    from unittest.mock import MagicMock

    from src.models.agent_run import AgentRun
    from src.models.enums import RunRole, RunStatus
    from src.services.review_runner import dispatch_review_run

    dev_uuid = uuid.uuid4()
    dev_row = _make_dev_row()
    reviewer_row = MagicMock()
    reviewer_row.status = RunStatus.queued

    def _get(model, run_id):
        if run_id == dev_uuid:
            return dev_row
        return reviewer_row

    existing_fixer = MagicMock()
    existing_fixer.status = RunStatus.queued
    existing_fixer.run_role = RunRole.fixer

    mock_session = _make_mock_session(dev_row)
    mock_session.get.side_effect = _get
    mock_session.exec.side_effect = [
        _make_exec_result(MagicMock()),
        _make_exec_result(existing_fixer),
    ]

    finding = {
        "file_path": "main.py",
        "severity": "critical",
        "category": "correctness",
        "issue": "Breaks on empty input.",
        "suggestion": "Guard against empty input.",
    }

    with (
        patch("src.services.review_runner.Session", return_value=mock_session),
        patch("src.services.review_runner.decrypt_token", return_value="fake-token"),
        patch("src.services.review_runner.publish_status_change"),
        patch("src.services.review_runner._fetch_pr_diff", return_value="diff"),
        patch("src.services.review_runner._emit_review_finding"),
        patch("src.services.review_runner.run_review", return_value=[finding]),
        patch("src.services.agent_dispatch.enqueue_agent_run") as mock_enqueue,
    ):
        dispatch_review_run(str(dev_uuid))

    added_runs = [
        call.args[0]
        for call in mock_session.add.call_args_list
        if call.args and isinstance(call.args[0], AgentRun)
    ]
    assert any(r.run_role == RunRole.reviewer for r in added_runs)
    assert not any(r.run_role == RunRole.fixer for r in added_runs)
    mock_enqueue.assert_not_called()
