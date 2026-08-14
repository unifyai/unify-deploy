"""
tests/conftest.py
=================

Lightweight pytest configuration for unity-deploy.

Unity-deploy tests are mostly offline unit tests.  The heavy session lifecycle
(project create/delete, per-test context isolation, cost tracking, stub
patching) from the unity repo is NOT replicated here — it's unnecessary.

Integration tests that need a live Unify project use ``@_handle_project``
from ``tests.helpers`` and are gated behind ``@pytest.mark.requires_orchestra``.
When the API is unreachable they skip cleanly; when running via
``parallel_run.sh`` the project is prepared externally.
"""

from __future__ import annotations

import logging
import os
import random

import httpx
import pytest
import unisdk

from tests.settings import SETTINGS
from unify.manager_registry import ManagerRegistry

_logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Orchestra availability (cached)                                              #
# --------------------------------------------------------------------------- #
def _check_orchestra_available() -> bool:
    if hasattr(_check_orchestra_available, "_cached"):
        return _check_orchestra_available._cached

    orchestra_url = os.environ.get("ORCHESTRA_URL", "http://localhost:8000").rstrip("/")
    if orchestra_url.endswith("/v0"):
        health_url = f"{orchestra_url}/projects"
    else:
        health_url = f"{orchestra_url}/v0/projects"
    try:
        with httpx.Client(timeout=5.0) as client:
            # Authenticated, and only 200 counts. These tests create projects
            # and write rows; a server that answers 401 can host none of that,
            # so treating "something is listening" as available just moves the
            # failure from a clean skip to an auth error mid-suite.
            resp = client.get(
                health_url,
                headers={"Authorization": f"Bearer {os.environ.get('UNIFY_KEY', '')}"},
            )
            _check_orchestra_available._cached = resp.status_code == 200
    except Exception:
        _check_orchestra_available._cached = False

    return _check_orchestra_available._cached


# --------------------------------------------------------------------------- #
# Hooks                                                                        #
# --------------------------------------------------------------------------- #
def pytest_configure(config):
    try:
        from unify_deploy.utils.load_repo_env import load_repo_dotenv

        load_repo_dotenv(override=False)
    except Exception:
        try:
            from dotenv import load_dotenv
            from pathlib import Path

            env_path = Path(__file__).resolve().parent.parent / ".env"
            if env_path.is_file():
                load_dotenv(str(env_path), override=False)
        except Exception:
            pass

    config.addinivalue_line(
        "markers",
        "requires_orchestra: mark test as needing a live Unify/Orchestra API",
    )
    config.addinivalue_line(
        "markers",
        "enable_eventbus: enable EventBus publishing for this test",
    )


def pytest_runtest_setup(item):
    if item.get_closest_marker("requires_orchestra"):
        if not _check_orchestra_available():
            pytest.skip("Orchestra server not available")


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def unify_project():
    """Activate a Unify project once per session (single API call)."""
    project = SETTINGS.test_project_name
    unisdk.activate(project)
    unisdk.set_context(project, relative=False, skip_create=True)
    return project


@pytest.fixture(autouse=True)
def _clear_singletons():
    """Prevent singleton state from leaking between tests.

    Only clears ManagerRegistry (cached singleton instances).
    ContextRegistry is NOT cleared here — re-creating remote contexts
    on every test is prohibitively expensive.  Tests that need registry
    isolation (test_registration, test_resolve) manage their own cleanup.
    """
    yield
    ManagerRegistry.clear()


@pytest.fixture(autouse=True)
def _set_random_seed():
    random.seed(42)
