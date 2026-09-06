"""Reuse route-test DB fixtures so model tests share a single fixture source.

pytest collects conftest.py per directory, so fixtures defined in
tests/routes/conftest.py are not visible here. Re-export the needed
fixture functions (no copies, no second client).
"""

from tests.routes.conftest import (
    fixture_engine,
    fixture_repository,
    fixture_session,
    fixture_task,
    fixture_test_user,
)

__all__ = [
    "fixture_engine",
    "fixture_repository",
    "fixture_session",
    "fixture_task",
    "fixture_test_user",
]
