"""Unify company-wide operating-memory assistant_deployments.

Two deployments are routed under this client:

* ``default`` — the original Unify company brain (CRM seed data,
  generic guidance, light function surface).  Catch-all for every
  assistant in the tenant.
* ``brain_operator`` — dedicated colleague that owns the recurring +
  trigger-based jobs declared in the brain repo's
  ``brain.scheduled`` registry.  Activated for the assistant id named
  by the ``BRAIN_OPERATOR_ASSISTANT_ID`` environment variable so the
  numeric id stays out of source while the deployment package stays
  importable in every environment.
"""

from __future__ import annotations

import os
from pathlib import Path

from unity_deploy.assistant_deployments.deployment_types import (
    DeploymentMapping,
    DeploymentTarget,
    register_client,
)

_DEPLOYMENTS_DIR = Path(__file__).parent / "deployments"

# Optional override.  When set to a numeric assistant id, the
# brain_operator deployment activates for that assistant; otherwise
# only the default mapping is in play.  Set this in the production
# Cloud Run env bundle once the brain_operator assistant has been
# provisioned in Orchestra.
BRAIN_OPERATOR_ASSISTANT_ID = os.environ.get("BRAIN_OPERATOR_ASSISTANT_ID")


def _targets() -> list[DeploymentTarget]:
    out: list[DeploymentTarget] = []
    if BRAIN_OPERATOR_ASSISTANT_ID:
        out.append(
            DeploymentTarget(
                scope="assistant",
                scope_id=BRAIN_OPERATOR_ASSISTANT_ID,
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
