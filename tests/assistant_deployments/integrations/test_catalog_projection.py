"""Tests for projecting native integration manifests into app catalog payloads."""

from __future__ import annotations

import sys
from types import ModuleType

from unify_deploy.assistant_deployments.integrations.activation import (
    expand_integrations,
)
from unify_deploy.assistant_deployments.integrations.catalog_projection import (
    NATIVE_INTEGRATION_BACKEND_ID,
    native_catalog_app_from_registry_row,
    native_catalog_apps_from_registry,
    sync_integrations,
)

from .test_activation import _empty_resolved


def test_native_registry_row_projects_to_app_only_catalog_payload() -> None:
    resolved = expand_integrations(_empty_resolved(integrations=["matterport"]))
    row = {item["slug"]: item for item in resolved.integration_registry}["matterport"]

    app = native_catalog_app_from_registry_row(row)

    assert app["provider_app_id"] == "matterport"
    assert app["canonical_app_slug"] == "matterport"
    assert app["source_type"] == "native"
    assert app["auth_modes"] == ["native"]
    assert "tools" not in app
    assert app["backend_id"] == NATIVE_INTEGRATION_BACKEND_ID
    assert app["raw_provider_metadata"]["native_metadata"]["tier"] == "api"
    assert "matterport_graphql_query" in app["function_names"]
    assert "Matterport Overview" in app["guidance_titles"]


def test_mock_packages_project_only_when_registry_row_is_explicitly_included() -> None:
    default_resolved = expand_integrations(
        _empty_resolved(integrations=["github"]),
    )
    assert "client_alpha_repairs_mock" not in {
        app["canonical_app_slug"]
        for app in native_catalog_apps_from_registry(
            default_resolved.integration_registry,
        )
    }

    explicit_resolved = expand_integrations(
        _empty_resolved(integrations=["client_alpha_repairs_mock"]),
        include_mock_packages=True,
    )
    apps = native_catalog_apps_from_registry(explicit_resolved.integration_registry)

    assert [app["canonical_app_slug"] for app in apps] == ["client_alpha_repairs_mock"]
    assert apps[0]["source_type"] == "native"


def test_sync_native_catalog_seeds_builtins_app_rows(monkeypatch) -> None:
    resolved = expand_integrations(_empty_resolved(integrations=["github"]))
    calls: list[dict] = []

    def fake_seed_builtin_integrations(**kwargs):
        calls.append(kwargs)
        return True

    unity_module = ModuleType("unify")
    integrations_module = ModuleType("unify.integrations")
    builtins_catalog_module = ModuleType("unify.integrations.builtins_catalog")
    builtins_catalog_module.seed_builtin_integrations = fake_seed_builtin_integrations
    integrations_module.builtins_catalog = builtins_catalog_module
    unity_module.integrations = integrations_module
    monkeypatch.setitem(sys.modules, "unify", unity_module)
    monkeypatch.setitem(sys.modules, "unify.integrations", integrations_module)
    monkeypatch.setitem(
        sys.modules,
        "unify.integrations.builtins_catalog",
        builtins_catalog_module,
    )

    result = sync_integrations(resolved.integration_registry)

    assert result == {
        "status": "synced",
        "backend_id": NATIVE_INTEGRATION_BACKEND_ID,
        "source_type": "native",
        "cache_version": "unity-deploy-native-v1",
        "apps_upserted": 1,
        "tools_upserted": 0,
    }
    assert calls[0]["apps"][0]["canonical_app_slug"] == "github"
    assert calls[0]["backend_id"] == NATIVE_INTEGRATION_BACKEND_ID
    assert calls[0]["app_slugs"] == ["github"]
    assert calls[0]["prune_unlisted_apps"] is True
