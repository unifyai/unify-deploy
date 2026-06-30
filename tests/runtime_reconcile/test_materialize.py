from __future__ import annotations

from pathlib import Path
from types import ModuleType, SimpleNamespace

from unity_deploy.runtime_reconcile.context import RuntimeIdentity
from unity_deploy.runtime_reconcile import materialize


class _FakeFunctionManager:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def sync_custom(self, *, source_functions=None, source_venvs=None) -> bool:
        self.calls.append(
            {
                "source_functions": source_functions,
                "source_venvs": source_venvs,
            },
        )
        return True


def _install_materialize_fakes(
    monkeypatch,
    *,
    function_collector,
    venv_collector,
    function_manager: _FakeFunctionManager,
) -> None:
    custom_functions = ModuleType("unify.function_manager.custom_functions")
    custom_functions.collect_functions_from_directories = function_collector
    custom_functions.collect_venvs_from_directories = venv_collector
    monkeypatch.setitem(
        __import__("sys").modules,
        "unify.function_manager.custom_functions",
        custom_functions,
    )

    manager_registry = ModuleType("unify.manager_registry")

    class ManagerRegistry:
        @staticmethod
        def get_function_manager():
            return function_manager

    manager_registry.ManagerRegistry = ManagerRegistry
    monkeypatch.setitem(
        __import__("sys").modules,
        "unify.manager_registry",
        manager_registry,
    )

    seed_sync = ModuleType("unity_deploy.assistant_deployments.seed_sync")
    seed_sync.sync_all_seed_data = lambda _resolved: False
    monkeypatch.setitem(
        __import__("sys").modules,
        "unity_deploy.assistant_deployments.seed_sync",
        seed_sync,
    )


def test_materialize_syncs_authoritative_empty_custom_sources(monkeypatch):
    fake_fm = _FakeFunctionManager()

    _install_materialize_fakes(
        monkeypatch,
        function_collector=lambda _dirs: {},
        venv_collector=lambda _dirs: {},
        function_manager=fake_fm,
    )
    monkeypatch.setattr(
        materialize,
        "_enabled_integration_source_dirs",
        lambda: ([], []),
    )

    result = materialize.materialize_runtime_state(
        SimpleNamespace(function_dirs=[], venv_dirs=[]),
        RuntimeIdentity(assistant_id="382", user_id="user-1"),
        revision="test-revision",
    )

    assert result.custom_changed is True
    assert fake_fm.calls == [{"source_functions": {}, "source_venvs": {}}]


def test_materialize_includes_enabled_integration_dirs(monkeypatch, tmp_path):
    deployment_dir = tmp_path / "deployment-functions"
    integration_dir = tmp_path / "github-functions"
    deployment_dir.mkdir()
    integration_dir.mkdir()
    seen_dirs: list[Path] = []
    fake_fm = _FakeFunctionManager()

    def collect_functions(dirs):
        seen_dirs.extend(dirs)
        return {path.name: {"custom_hash": path.name} for path in dirs}

    _install_materialize_fakes(
        monkeypatch,
        function_collector=collect_functions,
        venv_collector=lambda _dirs: {},
        function_manager=fake_fm,
    )
    monkeypatch.setattr(
        materialize,
        "_enabled_integration_source_dirs",
        lambda: ([integration_dir], []),
    )

    result = materialize.materialize_runtime_state(
        SimpleNamespace(function_dirs=[deployment_dir], venv_dirs=[]),
        RuntimeIdentity(assistant_id="382", user_id="user-1"),
        revision="test-revision",
    )

    assert result.custom_changed is True
    assert seen_dirs == [deployment_dir, integration_dir]
    assert fake_fm.calls[0]["source_functions"] == {
        "deployment-functions": {"custom_hash": "deployment-functions"},
        "github-functions": {"custom_hash": "github-functions"},
    }
