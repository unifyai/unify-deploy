"""
tests/conftest.py
=================

Lightweight pytest configuration for unity-deploy.

Unity-deploy tests are mostly offline unit tests.  The heavy session lifecycle
(project create/delete, per-test context isolation, cost tracking, stub
patching) from the unity repo is NOT replicated here — it's unnecessary.

Integration tests that need a live Unify project use ``@_handle_project``
from ``tests.helpers`` and are gated behind ``@pytest.mark.requires_orchestra``.
They run against a local Orchestra, skip cleanly when none is reachable, and
refuse the hosted backend unless ``UNIFY_TESTS_ALLOW_PROD`` says otherwise;
when running via ``parallel_run.sh`` the project is prepared externally.
"""

from __future__ import annotations

import logging
import os
import random
from urllib.parse import urlparse

# Resolve the backend before anything imports unisdk.  ``unisdk.BASE_URL`` is
# bound from the environment at import time, so a value arriving any later —
# from the repo ``.env``, or from ``pytest_configure`` — reaches the
# availability probe but not the client the tests actually call.  These suites
# target a local Orchestra the way every other repo's do; without this default
# the client falls back to the hosted URL, and a local run reads and writes
# production while the probe reports on localhost.
from unify_deploy.utils.load_repo_env import load_repo_dotenv

load_repo_dotenv(override=False)
os.environ.setdefault("ORCHESTRA_URL", "http://localhost:8000/v0")

import httpx
import pytest
import unisdk

from tests.settings import SETTINGS
from unify.manager_registry import ManagerRegistry

_logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Orchestra availability (cached)                                              #
# --------------------------------------------------------------------------- #
def _orchestra_url() -> str:
    """Return the backend the tests themselves call.

    Read from ``unisdk`` rather than from the environment.  ``BASE_URL`` is
    already bound by the time any of this runs, so the two can disagree, and a
    probe that checks a different server than the suite uses answers the wrong
    question.
    """

    return unisdk.BASE_URL.rstrip("/")


def _targets_production() -> bool:
    return urlparse(_orchestra_url()).hostname == "api.unify.ai"


def _check_orchestra_available() -> bool:
    if hasattr(_check_orchestra_available, "_cached"):
        return _check_orchestra_available._cached

    orchestra_url = _orchestra_url()
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
        # These tests create projects and register functions with
        # ``overwrite=True``.  Pointed at the hosted backend they do that to
        # real rows, so production is opt-in rather than a thing a missing
        # environment variable can arrive at.
        if _targets_production() and not SETTINGS.UNIFY_TESTS_ALLOW_PROD:
            pytest.skip(
                "Refusing to run against production Orchestra "
                f"({_orchestra_url()}); point ORCHESTRA_URL at a local or "
                "staging backend, or set UNIFY_TESTS_ALLOW_PROD=1",
            )
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
