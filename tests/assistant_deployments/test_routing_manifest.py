"""Tests for routing_manifest.yaml resolution."""

from __future__ import annotations

import pytest

from unity_deploy.assistant_deployments.routing_manifest import (
    resolve_client_bundle_target,
    resolve_manifest_layers,
)


@pytest.fixture(autouse=True)
def _staging_env(monkeypatch):
    monkeypatch.setenv(
        "ORCHESTRA_URL",
        "https://internal.example.com/v0",
    )


def test_resolve_unify_company_org_staging():
    target = resolve_client_bundle_target(org_id=5)
    assert target is not None
    assert target.client_name == "unify_company"
    assert target.deployment == "default"
    assert target.bundle_key == "unify_company"


def test_resolve_client_alpha_assistant_staging():
    target = resolve_client_bundle_target(assistant_id=2242)
    assert target is not None
    assert target.client_name == "client_alpha"
    assert target.deployment == "v2"


def test_resolve_manifest_layers_client_alpha():
    layers = resolve_manifest_layers("client_alpha", assistant_id=2242)
    assert len(layers) == 1
    assert layers[0].integrations == ("client_alpha_repairs_mock",)
