"""Shared resolution of the brain_operator assistant id + task-enable flag.

The brain_operator deployment (and its ``<id> -> brain_operator`` target
mapping) must resolve **identically in two execution contexts**:

* deploy-time control-plane reconcile (runs in the reconcile job), and
* assistant runtime (the woken assistant's ``startup_hook``, which seeds the
  scenario's TaskScheduler tasks + FunctionManager functions).

brain_operator is deployed **only on production** (assistant 1406).  The
main-branch Cloud Build passes ``BRAIN_OPERATOR_ASSISTANT_ID`` into the
production reconcile job; staging deploys omit it and resolve to ``None``.

At runtime on the production operator pod the reconcile env vars may be
absent, so when ``BRAIN_OPERATOR_ASSISTANT_ID`` is unset we fall back to
1406 only when :func:`detect_environment` reports production.

An explicit ``BRAIN_OPERATOR_ASSISTANT_ID`` env var still wins as an override
(e.g. to target a one-off assistant).  ``BRAIN_OPERATOR_TASKS_ENABLED`` can
force tasks on/off without changing the assistant id.
"""

from __future__ import annotations

import os

from unity_deploy.assistant_deployments.deployment_types import detect_environment

# Production "Brain Operator" assistant in the Unify org (agent_id 1406).
_PRODUCTION_ASSISTANT_ID = "1406"


def brain_operator_assistant_id() -> str | None:
    """Resolve the brain_operator assistant id.

    Returns the explicit ``BRAIN_OPERATOR_ASSISTANT_ID`` when set.  Otherwise
    returns the production operator (1406) only in production; staging and
    other environments get ``None`` so reconcile is a safe no-op.

    The assistant-scoped target is registered with ``missing_ok=True``, so a
    stale id skips deploy-time reconcile without hiding failures for required
    customer deployments.
    """

    override = (os.environ.get("BRAIN_OPERATOR_ASSISTANT_ID") or "").strip()
    if override:
        return override
    if detect_environment() == "production":
        return _PRODUCTION_ASSISTANT_ID
    return None


def brain_operator_tasks_enabled() -> bool:
    """Whether brain_jobs scenario tasks ship enabled.

    ``BRAIN_OPERATOR_TASKS_ENABLED`` overrides when set (1/true/yes => enabled,
    0/false/no => disabled).  Otherwise defaults to enabled on production
    (where brain_operator is active) and disabled elsewhere.
    """

    override = os.environ.get("BRAIN_OPERATOR_TASKS_ENABLED")
    if override is not None and override.strip():
        return override.strip().lower() in ("1", "true", "yes")
    return detect_environment() == "production"
