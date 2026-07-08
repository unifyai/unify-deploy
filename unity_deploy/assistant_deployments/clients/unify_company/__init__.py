"""Unify organization assistant deployment.

All Unify-internal assistants in the unify org resolve to the ``default``
deployment, which carries CRM seed data, guidance, and the brain scheduled-job
function surface.
"""

from __future__ import annotations

import os
from pathlib import Path

from unity_deploy.assistant_deployments.deployment_types import (
    DeploymentMapping,
    DeploymentTarget,
    detect_environment,
    register_client,
)

_DEPLOYMENTS_DIR = Path(__file__).parent / "deployments"
_ORG_IDS: dict[str, str] = {
    "production": "1",
    "staging": "5",
}


def unify_company_org_id() -> str | None:
    """Resolve the Unify organization id for the current environment."""

    override = (os.environ.get("UNIFY_COMPANY_ORG_ID") or "").strip()
    if override:
        return override
    return _ORG_IDS.get(detect_environment())


def _targets() -> list[DeploymentTarget]:
    org_id = unify_company_org_id()
    if not org_id:
        return []
    return [DeploymentTarget(scope="org", scope_id=org_id, deployment="default")]


_MAPPING = DeploymentMapping(targets=_targets())

_ENV = detect_environment()

if _MAPPING.targets:
    _loaded_deployments = register_client(
        "unify_company",
        _MAPPING,
        _DEPLOYMENTS_DIR,
        environment=_ENV,
    )
else:
    _loaded_deployments = {}
