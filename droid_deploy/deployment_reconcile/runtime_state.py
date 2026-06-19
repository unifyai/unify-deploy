"""Compatibility imports for runtime reconciliation.

Runtime state materialization now belongs to
``droid_deploy.runtime_reconcile`` because it runs in the woken assistant's
own identity/context.  This module remains as a thin import shim for older
deployment-reconcile call sites and tests while Cloud Build is narrowed back
to deploy-time control-plane work.
"""

from droid_deploy.runtime_reconcile.context import (
    RuntimeIdentity,
    activate_runtime_context,
)
from droid_deploy.runtime_reconcile.materialize import (
    RuntimeStateResult,
    compute_runtime_state_fingerprint,
    materialize_runtime_state,
)

__all__ = [
    "RuntimeIdentity",
    "RuntimeStateResult",
    "activate_runtime_context",
    "compute_runtime_state_fingerprint",
    "materialize_runtime_state",
]
