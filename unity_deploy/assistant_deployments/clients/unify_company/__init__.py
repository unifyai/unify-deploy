"""Unify company-wide operating-memory assistant_deployments.

Two deployments are routed under this client:

* ``default`` — the original Unify company brain (CRM seed data,
  generic guidance, light function surface).  Catch-all for every
  assistant in the tenant.
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

from pathlib import Path

from unity_deploy.assistant_deployments.clients.unify_company._brain_operator import (
    brain_operator_assistant_id,
)
from unity_deploy.assistant_deployments.deployment_types import (
    DeploymentMapping,
    DeploymentTarget,
    register_client,
)

_DEPLOYMENTS_DIR = Path(__file__).parent / "deployments"


def _targets() -> list[DeploymentTarget]:
    out: list[DeploymentTarget] = []
    assistant_id = brain_operator_assistant_id()
    if assistant_id:
        out.append(
            DeploymentTarget(
                scope="assistant",
                scope_id=assistant_id,
                deployment="brain_operator",
            ),
        )
    out.append(DeploymentTarget(scope="default", deployment="default"))
    return out


_MAPPING = DeploymentMapping(targets=_targets())

_loaded_deployments = register_client(
    "unify_company",
    _MAPPING,
    _DEPLOYMENTS_DIR,
)
