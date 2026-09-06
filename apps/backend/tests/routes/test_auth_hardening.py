def test_task_quota_enforced(client, repository, session, test_user):
    from datetime import date
    from src.models.usage_record import UsageRecord
    import uuid

    rec = UsageRecord(
        id=uuid.uuid4(), user_id=test_user.id, date=date.today(), run_count=15
    )
    session.add(rec)
    session.commit()
    resp = client.post(
        "/tasks/",
        json={
            "repository_id": str(repository.id),
            "instruction": "x",
            "target_branch": "main",
        },
    )
    assert resp.status_code == 429


def test_logout_bumps_session_version(client, session, test_user):
    assert test_user.session_version == 0
    resp = client.post("/auth/logout")
    assert resp.status_code == 204
    session.refresh(test_user)
    assert test_user.session_version == 1
    set_cookie = resp.headers.get("set-cookie", "")
    assert "patch_session" in set_cookie
    assert "Max-Age=0" in set_cookie


def test_stale_session_version_rejected(session, test_user):
    import pytest
    from fastapi import HTTPException

    from src.core.auth import _resolve_user
    from src.core.security import create_session_token

    token = create_session_token(test_user.id, session_version=0)
    test_user.session_version = 1
    session.add(test_user)
    session.commit()
    with pytest.raises(HTTPException) as exc_info:
        _resolve_user(session, token)
    assert exc_info.value.status_code == 401
