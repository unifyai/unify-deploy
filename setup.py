"""Setuptools entry point with install-profile support for slim runtime images."""

from __future__ import annotations

import os

from setuptools import find_packages, setup

_PROFILE = os.environ.get("UNITY_DEPLOY_INSTALL_PROFILE", "full").strip().lower()

_CLIENT_SUBPACKAGES = (
    "unity_deploy.assistant_deployments.clients.client_alpha",
    "unity_deploy.assistant_deployments.clients.clientepsilon_homes",
    "unity_deploy.assistant_deployments.clients.clientzeta",
    "unity_deploy.assistant_deployments.clients.client_beta",
    "unity_deploy.assistant_deployments.clients.clientgamma",
    "unity_deploy.assistant_deployments.clients.unify_company",
)

_RUNTIME_EXCLUDES = [f"{name}*" for name in _CLIENT_SUBPACKAGES]

if _PROFILE == "runtime":
    packages = find_packages(
        include=["unity_deploy*", "communication*", "adapters*", "common*"],
        exclude=_RUNTIME_EXCLUDES,
    )
else:
    packages = find_packages(
        include=["unity_deploy*", "communication*", "adapters*", "common*"],
    )

setup(packages=packages)
