"""Shared resolution of the brain_operator assistant id + task-enable flag.

The brain_operator deployment (and its ``<id> -> brain_operator`` target
mapping) must resolve **identically in two execution contexts**:

* deploy-time control-plane reconcile (runs in the reconcile job), and
* assistant runtime (the woken assistant's ``startup_hook``, which seeds the
  scenario's TaskScheduler tasks + FunctionManager functions).

Keying off :func:`detect_environment` (which reads ``ORCHESTRA_URL``) makes the
mapping present in both places with **no env-var plumbing into the assistant
pods** — the same approach the clientzeta client uses for assistant 630.  An
explicit ``BRAIN_OPERATOR_ASSISTANT_ID`` env var still wins as an override
(e.g. to target a one-off assistant, or a not-yet-mapped environment).

The previous design read ``BRAIN_OPERATOR_ASSISTANT_ID`` from ``os.environ`` at
import time.  That var was only wired into the reconcile job, so at assistant
runtime it was unset and ``get_deployment()`` returned no scenarios — the
brain_jobs tasks never seeded.  Resolving from the environment fixes that.
"""

from __future__ import annotations

import os

from unity_deploy.assistant_deployments.deployment_types import detect_environment

# Per-environment "Brain Operator" assistant id.  Production stays unset
# until the prod colleague is provisioned (add it here, mirroring the
# clientzeta id constants).
#
# Staging is temporarily unset pending confirmation of the live assistant
# id — the previous id (2098) no longer exists in the staging Orchestra
# DB, which 404'd the control-plane reconcile.  With no id mapped,
# brain_operator_assistant_id() returns None and the deployment ships no
# scenario activation (the documented safe no-op).  Restore the id here
# AND in cloudbuild-staging.yaml (_BRAIN_OPERATOR_ASSISTANT_ID) together.
_ASSISTANT_IDS: dict[str, str] = {
    # "staging": "2098",
}


def brain_operator_assistant_id() -> str | None:
    """Resolve the brain_operator assistant id for the current environment."""

    override = (os.environ.get("BRAIN_OPERATOR_ASSISTANT_ID") or "").strip()
    if override:
        return override
    return _ASSISTANT_IDS.get(detect_environment())


def brain_operator_tasks_enabled() -> bool:
    """Whether brain_jobs scenario tasks ship enabled.

    On staging the brain_operator owns the recurring jobs and ships them
    enabled so the trickle of work actually fires.  An explicit
    ``BRAIN_OPERATOR_TASKS_ENABLED`` env var overrides (1/true/yes => enabled).
    """

    override = os.environ.get("BRAIN_OPERATOR_TASKS_ENABLED")
    if override is not None:
        return override.strip().lower() in ("1", "true", "yes")
    return detect_environment() == "staging"
