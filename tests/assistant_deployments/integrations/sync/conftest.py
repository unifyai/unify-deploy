"""Context setup for integration sync tests.

Unity-deploy intentionally keeps a lightweight test lifecycle. Unlike the
core Unity repo, it does not install a global per-test ContextRegistry hook.
Tests that need live managers use the session-scoped ``unify_project`` fixture
from ``tests/conftest.py`` and set an explicit, isolated base context here.
This mirrors existing deploy-side FunctionManager sync tests.
"""

import os
import re

os.environ.pop("SKIP_UNITY_TEST_INIT", None)

import pytest
import unify


@pytest.fixture(autouse=True)
def _integration_sync_context(unify_project, request):
    """Set a per-test base context for real manager sync tests."""
    from unity.common.context_registry import ContextRegistry
    from unity.manager_registry import ManagerRegistry

    nodeid = request.node.nodeid.replace("::", "/").replace("[", "/").replace("]", "")
    nodeid = re.sub(r"[^A-Za-z0-9_/-]+", "_", nodeid)
    nodeid = re.sub(r"/+", "/", nodeid).strip("/")
    ctx = f"{unify_project}/{nodeid}/unassigned/unassigned"
    unify.set_context(ctx, relative=False, skip_create=True)
    ManagerRegistry.clear()
    ContextRegistry.clear()
    try:
        yield
    finally:
        ManagerRegistry.clear()
        ContextRegistry.clear()
        try:
            unify.unset_context()
        except Exception:
            pass
