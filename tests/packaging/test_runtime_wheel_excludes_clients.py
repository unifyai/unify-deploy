"""Tests that the runtime install profile excludes client packages."""

from __future__ import annotations

from pathlib import Path

from setuptools import find_packages


def test_runtime_profile_excludes_client_packages():
    repo_root = Path(__file__).resolve().parents[2]
    client_subpackages = (
        "unity_deploy.assistant_deployments.clients.client_alpha",
        "unity_deploy.assistant_deployments.clients.clientepsilon_homes",
        "unity_deploy.assistant_deployments.clients.clientzeta",
        "unity_deploy.assistant_deployments.clients.client_beta",
        "unity_deploy.assistant_deployments.clients.clientgamma",
        "unity_deploy.assistant_deployments.clients.unify_company",
    )
    runtime_excludes = [f"{name}*" for name in client_subpackages]
    pkgs = find_packages(
        where=str(repo_root),
        include=["unity_deploy*", "communication*", "adapters*", "common*"],
        exclude=runtime_excludes,
    )
    assert "unity_deploy.assistant_deployments.clients.client_alpha" not in pkgs
    assert "unity_deploy.assistant_deployments.clients.unify_company" not in pkgs
    assert any(p == "unity_deploy.assistant_deployments" for p in pkgs)
