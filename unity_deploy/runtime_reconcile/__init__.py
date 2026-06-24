"""Assistant-scoped runtime reconciliation."""

from unity_deploy.runtime_reconcile.context import (
    RuntimeIdentity,
    activate_runtime_context,
    runtime_identity_from_session,
)
from unity_deploy.runtime_reconcile.materialize import (
    RuntimeStateResult,
    compute_runtime_state_fingerprint,
    materialize_runtime_state,
)
from unity_deploy.runtime_reconcile.runner import (
    RuntimeReconcileHandle,
    start_runtime_reconcile,
)
from unity_deploy.runtime_reconcile.status import (
    RuntimeReconcileStatus,
    RuntimeReconcileStatusHandle,
    runtime_reconcile_prompt_note,
)

__all__ = [
    "RuntimeIdentity",
    "RuntimeReconcileHandle",
    "RuntimeReconcileStatus",
    "RuntimeReconcileStatusHandle",
    "RuntimeStateResult",
    "activate_runtime_context",
    "compute_runtime_state_fingerprint",
    "materialize_runtime_state",
    "runtime_identity_from_session",
    "runtime_reconcile_prompt_note",
    "start_runtime_reconcile",
]
