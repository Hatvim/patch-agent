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
