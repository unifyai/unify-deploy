"""Unit tests for worker bootstrap helpers in ``worker_utils``."""

from __future__ import annotations

import logging
import sys
import types

import pytest

from droid_deploy.infra.workers import worker_utils
from droid_deploy.infra.workers.worker_utils import (
    activate_unify_context,
    initialize_worker_environment,
)
from droid_deploy.utils.load_repo_env import droid_deploy_repo_root


@pytest.fixture
def _restore_root_logger():
    """Restore root logger state after bootstrap tests mutate it."""
    root = logging.getLogger()
    original_level = root.level
    original_handlers = list(root.handlers)
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    for handler in original_handlers:
        root.addHandler(handler)
    root.setLevel(original_level)


def test_initialize_worker_environment_skips_offline_ingest_initializer(
    monkeypatch,
    _restore_root_logger,
) -> None:
    """Worker bootstrap must not import the standalone script helper."""

    def _boom(**_kwargs):
        raise AssertionError("offline ingest bootstrap must not be used here")

    fake_module = types.SimpleNamespace(initialize_environment=_boom)
    monkeypatch.setitem(
        sys.modules,
        "droid_deploy.assistant_deployments.scripts.ingest_utils",
        fake_module,
    )

    project_root = initialize_worker_environment(debug=True)

    assert project_root == droid_deploy_repo_root()


def test_worker_bootstrap_imports_storage_client_symbol() -> None:
    """Guard worker image startup against missing google.cloud.storage import."""
    assert worker_utils.storage.Client is not None


def test_activate_unify_context_uses_explicit_identity(monkeypatch) -> None:
    """Explicit args win; env fallbacks are not consulted."""

    class _FakeUnify:
        def __init__(self) -> None:
            self.activated_projects: list[str] = []
            self.contexts: list[tuple[str, bool]] = []
            self._active_project = ""

        def active_project(self) -> str:
            return self._active_project

        def activate(self, project_name: str) -> None:
            self._active_project = project_name
            self.activated_projects.append(project_name)

        def unset_context(self) -> None:
            pass

        def set_context(self, ctx: str, skip_create: bool = False) -> None:
            self.contexts.append((ctx, skip_create))

    fake_unify = _FakeUnify()
    monkeypatch.setenv("UNIFY_KEY", "message-key")
    monkeypatch.setenv("UNIFY_PROJECT_NAME", "ProjectFromEnv")
    monkeypatch.setenv("USER_ID", "wrong-user")
    monkeypatch.setenv("ASSISTANT_ID", "wrong-assistant")
    monkeypatch.setitem(sys.modules, "unify", fake_unify)

    activate_unify_context(user_id="alice", assistant_id="42")

    assert fake_unify.activated_projects == ["ProjectFromEnv"]
    assert fake_unify.contexts == [("alice/42", False)]


def test_activate_unify_context_requires_installed_unify_key(monkeypatch) -> None:
    monkeypatch.delenv("UNIFY_KEY", raising=False)

    with pytest.raises(EnvironmentError, match="per-message resolver installed it"):
        activate_unify_context(user_id="alice", assistant_id="42")
