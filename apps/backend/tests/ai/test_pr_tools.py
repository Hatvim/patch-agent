from types import SimpleNamespace

import pytest

from src.ai.tools.pr_tools import (
    _check_has_changes,
    _check_worktree_has_changes,
    _ensure_remote_branch_matches_head,
)


def test_check_has_changes_detects_untracked_files(monkeypatch):
    calls: list[list[str]] = []

    def fake_run_git(args, check=True, github_token=None):
        _ = (check, github_token)
        calls.append(args)
        if args == ["status", "--porcelain"]:
            return SimpleNamespace(returncode=0, stdout="?? tests/new_test.py\n", stderr="")
        raise AssertionError(f"unexpected git command: {args}")

    monkeypatch.setattr("src.ai.tools.pr_tools._run_git", fake_run_git)

    _check_has_changes()

    assert calls == [["status", "--porcelain"]]


def test_check_has_changes_raises_when_status_clean_and_no_ahead_commit(monkeypatch):
    def fake_run_git(args, check=True, github_token=None):
        _ = (check, github_token)
        if args == ["status", "--porcelain"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args == ["log", "--oneline", "--branches", "--not", "--remotes"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected git command: {args}")

    monkeypatch.setattr("src.ai.tools.pr_tools._run_git", fake_run_git)

    with pytest.raises(RuntimeError, match="No changes detected"):
        _check_has_changes()


def test_check_has_changes_detects_unpushed_local_commit(monkeypatch):
    calls: list[list[str]] = []

    def fake_run_git(args, check=True, github_token=None):
        _ = (check, github_token)
        calls.append(args)
        if args == ["status", "--porcelain"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args == ["log", "--oneline", "--branches", "--not", "--remotes"]:
            return SimpleNamespace(returncode=0, stdout="abc123 local commit\n", stderr="")
        raise AssertionError(f"unexpected git command: {args}")

    monkeypatch.setattr("src.ai.tools.pr_tools._run_git", fake_run_git)

    _check_has_changes()

    assert calls == [
        ["status", "--porcelain"],
        ["log", "--oneline", "--branches", "--not", "--remotes"],
    ]


def test_check_worktree_has_changes_detects_unpushed_local_commit(monkeypatch):
    def fake_run_git(args, check=True, github_token=None):
        _ = (check, github_token)
        if args == ["status", "--porcelain"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args == ["log", "--oneline", "--branches", "--not", "--remotes"]:
            return SimpleNamespace(returncode=0, stdout="abc123 local commit\n", stderr="")
        raise AssertionError(f"unexpected git command: {args}")

    monkeypatch.setattr("src.ai.tools.pr_tools._run_git", fake_run_git)

    _check_worktree_has_changes()


def test_ensure_remote_branch_allows_local_branch_ahead_of_remote(monkeypatch):
    calls: list[list[str]] = []

    def fake_run_git(args, check=True, github_token=None):
        _ = (check, github_token)
        calls.append(args)
        if args == ["fetch", "origin", "patch/task-1"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args == ["rev-parse", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout="local\n", stderr="")
        if args == ["rev-parse", "FETCH_HEAD"]:
            return SimpleNamespace(returncode=0, stdout="remote\n", stderr="")
        if args == ["merge-base", "--is-ancestor", "FETCH_HEAD", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected git command: {args}")

    monkeypatch.setattr("src.ai.tools.pr_tools._run_git", fake_run_git)

    _ensure_remote_branch_matches_head("patch/task-1", "token")

    assert calls == [
        ["fetch", "origin", "patch/task-1"],
        ["rev-parse", "HEAD"],
        ["rev-parse", "FETCH_HEAD"],
        ["merge-base", "--is-ancestor", "FETCH_HEAD", "HEAD"],
    ]
