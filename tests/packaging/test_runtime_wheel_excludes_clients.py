"""Tests that the runtime install profile excludes client packages."""

from __future__ import annotations

from pathlib import Path

from setuptools import find_packages


def test_runtime_profile_excludes_client_packages():
    repo_root = Path(__file__).resolve().parents[2]
    client_subpackages = (
        "unify_deploy.assistant_deployments.clients.client_alpha",
        "unify_deploy.assistant_deployments.clients.clientepsilon_homes",
        "unify_deploy.assistant_deployments.clients.clientzeta",
        "unify_deploy.assistant_deployments.clients.client_beta",
        "unify_deploy.assistant_deployments.clients.unify_company",
    )
    runtime_excludes = [f"{name}*" for name in client_subpackages]
    pkgs = find_packages(
        where=str(repo_root),
        include=["unify_deploy*", "communication*", "adapters*", "common*"],
        exclude=runtime_excludes,
    )
    assert "unify_deploy.assistant_deployments.clients.client_alpha" not in pkgs
    assert "unify_deploy.assistant_deployments.clients.unify_company" not in pkgs
    assert any(p == "unify_deploy.assistant_deployments" for p in pkgs)
