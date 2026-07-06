"""Shared resolution of the brain_operator assistant id + task-enable flag.

The brain_operator deployment (and its ``<id> -> brain_operator`` target
mapping) must resolve **identically in two execution contexts**:

* deploy-time control-plane reconcile (runs in the reconcile job), and
* assistant runtime (the woken assistant's ``startup_hook``, which seeds the
  scenario's TaskScheduler tasks + FunctionManager functions).

Both read ``BRAIN_OPERATOR_ASSISTANT_ID`` and ``BRAIN_OPERATOR_TASKS_ENABLED``
from the environment. Cloud Build sets these per environment
(``deploy/cloudbuild-staging.yaml`` / ``deploy/cloudbuild.yaml`` substitutions
→ reconcile job + overlay image build args). When unset, fall back to the
environment default (**7367** staging, **1406** production).
"""

from __future__ import annotations

import os

from unity_deploy.assistant_deployments.deployment_types import detect_environment

_STAGING_ASSISTANT_ID = "7367"
_PRODUCTION_ASSISTANT_ID = "1406"


def brain_operator_assistant_id() -> str | None:
    """Resolve the brain_operator assistant id.

    Returns ``BRAIN_OPERATOR_ASSISTANT_ID`` when set. Otherwise falls back to
    **7367** on staging and **1406** on production. Unknown environments get
    ``None`` so reconcile and deployment routing stay safe no-ops.
    """

    override = (os.environ.get("BRAIN_OPERATOR_ASSISTANT_ID") or "").strip()
    if override:
        return override
    env = detect_environment()
    if env == "production":
        return _PRODUCTION_ASSISTANT_ID
    if env == "staging":
        return _STAGING_ASSISTANT_ID
    return None


def brain_operator_tasks_enabled() -> bool:
    """Whether brain_jobs scenario tasks ship enabled.

    ``BRAIN_OPERATOR_TASKS_ENABLED`` overrides when set (1/true/yes => enabled,
    0/false/no => disabled).  Otherwise defaults to **disabled** until
    operators explicitly arm tasks via Cloud Build or env.
    """

    override = os.environ.get("BRAIN_OPERATOR_TASKS_ENABLED")
    if override is not None and override.strip():
        return override.strip().lower() in ("1", "true", "yes")
    return False
