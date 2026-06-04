"""Unify organization operating-memory assistant_deployments.

Two deployments are routed under this client:

* ``default`` — the original Unify company brain (CRM seed data,
  generic guidance, light function surface).  Scoped to the Unify
  organization in each environment.
* ``brain_operator`` — dedicated colleague that owns the recurring +
  trigger-based jobs declared in the brain repo's
  ``brain.scheduled`` registry.  Activated for the environment's
  brain_operator assistant id (resolved by :mod:`._brain_operator` from
  ``detect_environment()``, with a ``BRAIN_OPERATOR_ASSISTANT_ID`` env
  override).  Resolving from the environment — rather than a
  reconcile-only env var — is what makes the mapping present at
  assistant *runtime* too, so the scenario's tasks actually seed on wake.
"""

from __future__ import annotations

import os
from pathlib import Path

from unity_deploy.assistant_deployments.clients.unify_company._brain_operator import (
    brain_operator_assistant_id,
)
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
    out: list[DeploymentTarget] = []
    assistant_id = brain_operator_assistant_id()
    if assistant_id:
        out.append(
            DeploymentTarget(
                scope="assistant",
                scope_id=assistant_id,
                deployment="brain_operator",
                missing_ok=True,
            ),
        )
    org_id = unify_company_org_id()
    if org_id:
        out.append(DeploymentTarget(scope="org", scope_id=org_id, deployment="default"))
    return out


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
