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


class _FakeSecretManager:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def sync_custom(self, *, source_secrets=None) -> bool:
        self.calls.append({"source_secrets": source_secrets})
        return True


class _FakeKnowledgeManager:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def sync_custom(self, *, source_tables=None) -> bool:
        self.calls.append({"source_tables": source_tables})
        return True


class _FakeDataManager:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def sync_custom(self, *, source_tables=None) -> bool:
        self.calls.append({"source_tables": source_tables})
        return True


class _FakeDashboardManager:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def sync_custom(self, *, source_entities=None) -> bool:
        self.calls.append({"source_entities": source_entities})
        return True


class _FakeIntegrationRegistrySync:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, *, source_registry=None) -> bool:
        self.calls.append({"source_registry": source_registry})
        return False


def _install_materialize_fakes(
    monkeypatch,
    *,
    function_collector,
    venv_collector,
    guidance_collector,
    contacts_collector,
    secrets_collector,
    secrets_model_collector,
    knowledge_collector,
    data_collector,
    dashboards_collector,
    integration_registry_collector,
    integration_registry_sync,
    blacklist_collector,
    function_manager: _FakeFunctionManager,
    guidance_manager: _FakeGuidanceManager,
    contact_manager: _FakeContactManager,
    secret_manager: _FakeSecretManager,
    knowledge_manager: _FakeKnowledgeManager,
    data_manager: _FakeDataManager,
    dashboard_manager: _FakeDashboardManager,
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

    custom_secrets = ModuleType("unify.secret_manager.custom_secrets")
    custom_secrets.collect_secrets_from_directories = secrets_collector
    custom_secrets.collect_secrets_from_secret_models = secrets_model_collector
    monkeypatch.setitem(
        __import__("sys").modules,
        "unify.secret_manager.custom_secrets",
        custom_secrets,
    )

    custom_knowledge = ModuleType("unify.knowledge_manager.custom_knowledge")
    custom_knowledge.collect_knowledge_from_directories = knowledge_collector
    monkeypatch.setitem(
        __import__("sys").modules,
        "unify.knowledge_manager.custom_knowledge",
        custom_knowledge,
    )

    custom_data = ModuleType("unify.data_manager.custom_data")
    custom_data.collect_data_from_directories = data_collector
    monkeypatch.setitem(
        __import__("sys").modules,
        "unify.data_manager.custom_data",
        custom_data,
    )

    custom_dashboards = ModuleType("unify.dashboard_manager.custom_dashboards")
    custom_dashboards.collect_dashboards_from_directories = dashboards_collector
    monkeypatch.setitem(
        __import__("sys").modules,
        "unify.dashboard_manager.custom_dashboards",
        custom_dashboards,
    )

    integration_registry = ModuleType("unify.integration_registry")
    integration_registry.collect_integration_registry_from_rows = (
        integration_registry_collector
    )
    integration_registry.sync_custom_integration_registry = integration_registry_sync
    monkeypatch.setitem(
        __import__("sys").modules,
        "unify.integration_registry",
        integration_registry,
    )

    catalog_projection = ModuleType(
        "unity_deploy.assistant_deployments.integrations.catalog_projection",
    )
    catalog_projection.sync_integrations = lambda _rows: None
    monkeypatch.setitem(
        __import__("sys").modules,
        "unity_deploy.assistant_deployments.integrations.catalog_projection",
        catalog_projection,
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
        def get_secret_manager():
            return secret_manager

        @staticmethod
        def get_knowledge_manager():
            return knowledge_manager

        @staticmethod
        def get_data_manager():
            return data_manager

        @staticmethod
        def get_dashboard_manager():
            return dashboard_manager

        @staticmethod
        def get_blacklist_manager():
            return blacklist_manager

    manager_registry.ManagerRegistry = ManagerRegistry
    monkeypatch.setitem(
        __import__("sys").modules,
        "unify.manager_registry",
        manager_registry,
    )


def test_materialize_syncs_authoritative_empty_custom_sources(monkeypatch):
    fake_fm = _FakeFunctionManager()
    fake_gm = _FakeGuidanceManager()
    fake_cm = _FakeContactManager()
    fake_sm = _FakeSecretManager()
    fake_km = _FakeKnowledgeManager()
    fake_dm = _FakeDataManager()
    fake_dash = _FakeDashboardManager()
    fake_registry_sync = _FakeIntegrationRegistrySync()
    fake_bm = _FakeBlacklistManager()

    _install_materialize_fakes(
        monkeypatch,
        function_collector=lambda _dirs: {},
        venv_collector=lambda _dirs: {},
        guidance_collector=lambda _dirs: {},
        contacts_collector=lambda _dirs: {},
        secrets_collector=lambda _dirs: {},
        secrets_model_collector=lambda _secrets: {},
        knowledge_collector=lambda _dirs: {},
        data_collector=lambda _dirs: {},
        dashboards_collector=lambda _dirs: {"tiles": {}, "layouts": {}},
        integration_registry_collector=lambda _rows: {},
        integration_registry_sync=fake_registry_sync,
        blacklist_collector=lambda _dirs: {},
        function_manager=fake_fm,
        guidance_manager=fake_gm,
        contact_manager=fake_cm,
        secret_manager=fake_sm,
        knowledge_manager=fake_km,
        data_manager=fake_dm,
        dashboard_manager=fake_dash,
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
            knowledge_dirs=[],
            custom_data_dirs=[],
            dashboards_dirs=[],
            integration_registry=[],
            secrets_dirs=[],
            secrets=[],
            blacklist_dirs=[],
        ),
        RuntimeIdentity(assistant_id="382", user_id="user-1"),
        revision="test-revision",
    )

    assert result.custom_changed is True
    assert result.guidance_changed is True
    assert result.contacts_changed is True
    assert result.knowledge_changed is True
    assert result.custom_data_changed is True
    assert result.dashboards_changed is True
    assert result.secrets_changed is True
    assert result.blacklist_changed is True
    assert fake_fm.calls == [{"source_functions": {}, "source_venvs": {}}]
    assert fake_gm.calls == [{"source_guidance": {}, "function_name_to_id": {}}]
    assert fake_cm.calls == [{"source_contacts": {}}]
    assert fake_km.calls == [{"source_tables": {}}]
    assert fake_dm.calls == [{"source_tables": {}}]
    assert fake_dash.calls == [{"source_entities": {"tiles": {}, "layouts": {}}}]
    assert fake_registry_sync.calls == [{"source_registry": {}}]
    assert fake_sm.calls == [{"source_secrets": {}}]
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
    fake_sm = _FakeSecretManager()
    fake_km = _FakeKnowledgeManager()
    fake_dm = _FakeDataManager()
    fake_dash = _FakeDashboardManager()
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
        secrets_collector=lambda _dirs: {},
        secrets_model_collector=lambda _secrets: {},
        knowledge_collector=lambda _dirs: {},
        data_collector=lambda _dirs: {},
        dashboards_collector=lambda _dirs: {"tiles": {}, "layouts": {}},
        integration_registry_collector=lambda _rows: {},
        integration_registry_sync=_FakeIntegrationRegistrySync(),
        blacklist_collector=lambda _dirs: {},
        function_manager=fake_fm,
        guidance_manager=fake_gm,
        contact_manager=fake_cm,
        secret_manager=fake_sm,
        knowledge_manager=fake_km,
        data_manager=fake_dm,
        dashboard_manager=fake_dash,
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
            knowledge_dirs=[],
            custom_data_dirs=[],
            dashboards_dirs=[],
            integration_registry=[],
            secrets_dirs=[],
            secrets=[],
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
