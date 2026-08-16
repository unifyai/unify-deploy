"""Setuptools entry point with install-profile support for slim runtime images."""

from __future__ import annotations

import os

from setuptools import find_packages, setup

_PROFILE = os.environ.get("UNIFY_DEPLOY_INSTALL_PROFILE", "full").strip().lower()

_CLIENT_SUBPACKAGES = (
    "unify_deploy.assistant_deployments.clients.client_alpha",
    "unify_deploy.assistant_deployments.clients.clientepsilon_homes",
    "unify_deploy.assistant_deployments.clients.clientzeta",
    "unify_deploy.assistant_deployments.clients.client_beta",
    "unify_deploy.assistant_deployments.clients.unify_company",
)

_RUNTIME_EXCLUDES = [f"{name}*" for name in _CLIENT_SUBPACKAGES]

if _PROFILE == "runtime":
    packages = find_packages(
        include=["unify_deploy*", "communication*", "adapters*", "common*"],
        exclude=_RUNTIME_EXCLUDES,
    )
else:
    packages = find_packages(
        include=["unify_deploy*", "communication*", "adapters*", "common*"],
    )

setup(packages=packages)
