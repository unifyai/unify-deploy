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

    def list_functions(self):
        return {}


class _FakeGuidanceManager:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def sync_custom(self, *, source_guidance=None, function_name_to_id=None) -> bool:
        self.calls.append(
            {
                "source_guidance": source_guidance,
                "function_name_to_id": function_name_to_id,
            },
        )
        return True


class _FakeBlacklistManager:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def sync_custom(self, *, source_blacklist=None) -> bool:
        self.calls.append({"source_blacklist": source_blacklist})
        return True


class _FakeContactManager:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def sync_custom(self, *, source_contacts=None) -> bool:
        self.calls.append({"source_contacts": source_contacts})
        return True


def _install_materialize_fakes(
    monkeypatch,
    *,
    function_collector,
    venv_collector,
    guidance_collector,
    contacts_collector,
    blacklist_collector,
    function_manager: _FakeFunctionManager,
    guidance_manager: _FakeGuidanceManager,
    contact_manager: _FakeContactManager,
    blacklist_manager: _FakeBlacklistManager,
) -> None:
    custom_functions = ModuleType("unify.function_manager.custom_functions")
    custom_functions.collect_functions_from_directories = function_collector
    custom_functions.collect_venvs_from_directories = venv_collector
    monkeypatch.setitem(
        __import__("sys").modules,
        "unify.function_manager.custom_functions",
        custom_functions,
    )

    custom_guidance = ModuleType("unify.guidance_manager.custom_guidance")
    custom_guidance.collect_guidance_from_directories = guidance_collector
    monkeypatch.setitem(
        __import__("sys").modules,
        "unify.guidance_manager.custom_guidance",
        custom_guidance,
    )

    custom_blacklist = ModuleType("unify.blacklist_manager.custom_blacklist")
    custom_blacklist.collect_blacklist_from_directories = blacklist_collector
    monkeypatch.setitem(
        __import__("sys").modules,
        "unify.blacklist_manager.custom_blacklist",
        custom_blacklist,
    )

    custom_contacts = ModuleType("unify.contact_manager.custom_contacts")
    custom_contacts.collect_contacts_from_directories = contacts_collector
    monkeypatch.setitem(
        __import__("sys").modules,
        "unify.contact_manager.custom_contacts",
        custom_contacts,
    )

    manager_registry = ModuleType("unify.manager_registry")

    class ManagerRegistry:
        @staticmethod
        def get_function_manager():
            return function_manager

        @staticmethod
        def get_guidance_manager():
            return guidance_manager

        @staticmethod
        def get_contact_manager():
            return contact_manager

        @staticmethod
        def get_blacklist_manager():
            return blacklist_manager

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
    fake_gm = _FakeGuidanceManager()
    fake_cm = _FakeContactManager()
    fake_bm = _FakeBlacklistManager()

    _install_materialize_fakes(
        monkeypatch,
        function_collector=lambda _dirs: {},
        venv_collector=lambda _dirs: {},
        guidance_collector=lambda _dirs: {},
        contacts_collector=lambda _dirs: {},
        blacklist_collector=lambda _dirs: {},
        function_manager=fake_fm,
        guidance_manager=fake_gm,
        contact_manager=fake_cm,
        blacklist_manager=fake_bm,
    )
    monkeypatch.setattr(
        materialize,
        "_enabled_integration_source_dirs",
        lambda: ([], [], []),
    )

    result = materialize.materialize_runtime_state(
        SimpleNamespace(
            function_dirs=[],
            venv_dirs=[],
            guidance_dirs=[],
            contacts_dirs=[],
            blacklist_dirs=[],
        ),
        RuntimeIdentity(assistant_id="382", user_id="user-1"),
        revision="test-revision",
    )

    assert result.custom_changed is True
    assert result.guidance_changed is True
    assert result.contacts_changed is True
    assert result.blacklist_changed is True
    assert fake_fm.calls == [{"source_functions": {}, "source_venvs": {}}]
    assert fake_gm.calls == [{"source_guidance": {}, "function_name_to_id": {}}]
    assert fake_cm.calls == [{"source_contacts": {}}]
    assert fake_bm.calls == [{"source_blacklist": {}}]


def test_materialize_includes_enabled_integration_dirs(monkeypatch, tmp_path):
    deployment_dir = tmp_path / "deployment-functions"
    integration_dir = tmp_path / "github-functions"
    deployment_dir.mkdir()
    integration_dir.mkdir()
    seen_dirs: list[Path] = []
    fake_fm = _FakeFunctionManager()
    fake_gm = _FakeGuidanceManager()
    fake_cm = _FakeContactManager()
    fake_bm = _FakeBlacklistManager()

    def collect_functions(dirs):
        seen_dirs.extend(dirs)
        return {path.name: {"custom_hash": path.name} for path in dirs}

    _install_materialize_fakes(
        monkeypatch,
        function_collector=collect_functions,
        venv_collector=lambda _dirs: {},
        guidance_collector=lambda _dirs: {},
        contacts_collector=lambda _dirs: {},
        blacklist_collector=lambda _dirs: {},
        function_manager=fake_fm,
        guidance_manager=fake_gm,
        contact_manager=fake_cm,
        blacklist_manager=fake_bm,
    )
    monkeypatch.setattr(
        materialize,
        "_enabled_integration_source_dirs",
        lambda: ([integration_dir], [], []),
    )

    result = materialize.materialize_runtime_state(
        SimpleNamespace(
            function_dirs=[deployment_dir],
            venv_dirs=[],
            guidance_dirs=[],
            contacts_dirs=[],
            blacklist_dirs=[],
        ),
        RuntimeIdentity(assistant_id="382", user_id="user-1"),
        revision="test-revision",
    )

    assert result.custom_changed is True
    assert seen_dirs == [deployment_dir, integration_dir]
    assert fake_fm.calls[0]["source_functions"] == {
        "deployment-functions": {"custom_hash": "deployment-functions"},
        "github-functions": {"custom_hash": "github-functions"},
    }
