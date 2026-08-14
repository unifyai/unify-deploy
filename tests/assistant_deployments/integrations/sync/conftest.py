"""Context setup for integration sync tests.

Unity-deploy intentionally keeps a lightweight test lifecycle. Unlike the
core Unity repo, it does not install a global per-test ContextRegistry hook,
so each test here gets its own base context named after its node id.

Where the rows underneath that context live depends on what the test is for.
A test gated on ``requires_orchestra`` wants a real backend and gets a live
project. Everything else is served by the in-memory store, so the sync logic
is asserted without a credential or a deployed Orchestra.
"""

import os
import re

os.environ.pop("SKIP_UNITY_TEST_INIT", None)

import pytest
import unisdk

from tests.fake_orchestra import FAKE_PROJECT


@pytest.fixture(autouse=True)
def _integration_sync_context(request):
    """Set a per-test base context over the backend the test asked for."""
    from unify.common.context_registry import ContextRegistry
    from unify.manager_registry import ManagerRegistry

    if request.node.get_closest_marker("requires_orchestra"):
        project = request.getfixturevalue("unify_project")
    else:
        request.getfixturevalue("fake_orchestra_store")
        project = FAKE_PROJECT

    nodeid = request.node.nodeid.replace("::", "/").replace("[", "/").replace("]", "")
    nodeid = re.sub(r"[^A-Za-z0-9_/-]+", "_", nodeid)
    nodeid = re.sub(r"/+", "/", nodeid).strip("/")
    ctx = f"{project}/{nodeid}/unassigned/unassigned"
    unisdk.set_context(ctx, relative=False, skip_create=True)
    ManagerRegistry.clear()
    ContextRegistry.clear()
    try:
        yield
    finally:
        ManagerRegistry.clear()
        ContextRegistry.clear()
        try:
            unisdk.unset_context()
        except Exception:
            pass
