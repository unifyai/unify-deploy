"""Unify company-wide operating-memory customization."""

from __future__ import annotations

from pathlib import Path

from unity_deploy.customization.deployment_types import (
    DeploymentMapping,
    DeploymentTarget,
    register_client,
)

_DEPLOYMENTS_DIR = Path(__file__).parent / "deployments"

_MAPPING = DeploymentMapping(
    targets=[
        DeploymentTarget(scope="default", deployment="default"),
    ],
)

_loaded_deployments = register_client(
    "unify_company",
    _MAPPING,
    _DEPLOYMENTS_DIR,
)
