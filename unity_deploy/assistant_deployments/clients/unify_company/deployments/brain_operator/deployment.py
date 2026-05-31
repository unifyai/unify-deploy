"""
Brain operator deployment for the Unify company tenant.

The brain operator is a dedicated virtual colleague that owns every
recurring + trigger-based job declared in the ``brain.scheduled``
registry.  Its job is to fire those ticks deterministically through
Unity's ``TaskScheduler`` -> ``FunctionManager`` -> brain entrypoint
loop, with no laptop process required.

What lives here:

* ``functions/`` — ``@custom_function``-decorated wrappers that import
  brain entrypoints (``brain.crm.*``, ``brain.outbound.*``,
  ``brain.intel.*``, ``brain.influencers.*``) and adapt them to the
  FunctionManager isolation contract.  These are picked up by
  ``FunctionManager.sync_custom`` at assistant boot.
* ``scenarios/brain_jobs_v0.yaml`` — generated from the brain
  registry via ``brain scheduled export-scenario``.  Materialised
  into TaskScheduler rows by the deploy-reconcile control plane.

What does NOT live here:

* Job declarations.  Those live in brain itself
  (``brain/<area>/scheduled_jobs.py``) and are mirrored into the
  scenario YAML.  Editing the YAML by hand is forbidden — re-run
  the export instead.
* Per-customer guidance, per-team knowledge, voice configuration.
  The brain operator stays scoped to the Unify-internal company
  tenant; customer-facing colleagues are separate deployments.

See ``brain/docs/operations/scheduled-jobs.md`` for the end-to-end
pattern (architecture diagrams, decision matrix, recipe for adding a
new tick).
"""

from __future__ import annotations

from pathlib import Path

from unity.guidance_manager.types.guidance import Guidance
from unity.secret_manager.types import Secret

from unity_deploy.assistant_deployments.clients.unify_company._brain_operator import (
    brain_operator_assistant_id,
    brain_operator_tasks_enabled,
)
from unity_deploy.assistant_deployments.configs.types.actor_config import ActorConfig
from unity_deploy.assistant_deployments.deployment_types import DeploymentSpec
from unity_deploy.assistant_deployments.scenarios.types import ScenarioActivation


def _scenario_activations() -> list[ScenarioActivation]:
    """Return the brain_jobs scenario activations for this deployment.

    The assistant id is resolved from the current environment
    (:func:`brain_operator_assistant_id`, which keys off
    ``detect_environment()`` with a ``BRAIN_OPERATOR_ASSISTANT_ID``
    override).  Resolving this way — rather than from a reconcile-only
    env var — is what makes the scenario present at assistant *runtime*,
    so the woken assistant's ``startup_hook`` actually seeds the
    TaskScheduler rows.  When no id is mapped for the environment the
    activation list is empty and no rows are materialised (the
    unify_company catch-all still works).

    ``tasks_enabled`` ships true on staging so the brain_operator's
    recurring work fires; the control-plane reconcile defers any
    not-yet-seeded activation to the runtime plane, so shipping enabled
    on a brand-new assistant is safe.
    """

    assistant_id = brain_operator_assistant_id()
    if not assistant_id:
        return []
    return [
        ScenarioActivation(
            scenario_template="brain_jobs/brain_jobs_v0",
            assistant_id=assistant_id,
            scenario_id_override="unify_company_brain_jobs_v0",
            tasks_enabled=brain_operator_tasks_enabled(),
        ),
    ]


def get_deployment() -> DeploymentSpec:
    return DeploymentSpec(
        name="brain_operator",
        actor_config=ActorConfig(),
        scenarios=_scenario_activations(),
        guidance=[
            Guidance(
                title="Brain operator role definition",
                content=(
                    "You are the brain_operator virtual colleague.  Your job "
                    "is to execute the recurring and trigger-based functions "
                    "registered by the brain repo's brain.scheduled registry, "
                    "exactly as scheduled.  You do not improvise: every "
                    "function call must come from a TaskScheduler row that "
                    "names a registered FunctionManager function id.  When a "
                    "scheduled function fails, surface the failure verbatim — "
                    "do not retry blindly."
                ),
            ),
            Guidance(
                title="Brain function audit policy",
                content=(
                    "Every brain function that mutates state (CRM stage, "
                    "outbound message, drive content, calendar event) must "
                    "produce an audit row before doing so. CRM writes land in "
                    "Data/CRM/OutboundActions; outbound WhatsApp / email "
                    "sends produce OutboundActions rows with reason, "
                    "recipients, body, and link-tracking provenance. "
                    "Functions that cannot satisfy this contract are not "
                    "eligible to run as offline tasks."
                ),
            ),
            Guidance(
                title="Browser primitive use",
                content=(
                    "Functions that drive ComputerPrimitives (e.g. the "
                    "HackerNews summariser, the YouTube extractor) run inside "
                    "the deployed assistant pod alongside the agent-service. "
                    "Default to headless execution; bring the visible "
                    "noVNC desktop online only when an operator needs to "
                    "co-pilot a CAPTCHA or sign-in.  Browser sessions must "
                    "persist their state to data/ paths the brain functions "
                    "control — never to /tmp."
                ),
            ),
            Guidance(
                title="Link tracking invariant",
                content=(
                    "Outbound URLs in any message body — WhatsApp, email, "
                    "DMs, deck links — must route through the r.unify.ai "
                    "first-party link tracker.  The brain.outbound.whatsapp "
                    "and brain.outbound helpers auto-rewrite; functions that "
                    "construct messages without those helpers must call "
                    "brain.outbound.evergreen.link_tracking.rewrite_links_in_text "
                    "explicitly before sending."
                ),
            ),
            Guidance(
                title="Schedule edits go through brain",
                content=(
                    "Do not edit scenarios/brain_jobs_v0.yaml by hand.  Job "
                    "declarations live in brain.<area>.scheduled_jobs "
                    "modules; the YAML is regenerated by `brain scheduled "
                    "export-scenario`.  If a scheduled function needs to "
                    "change, edit its BrainScheduledJob declaration first."
                ),
            ),
        ],
        secrets=[
            Secret(
                name="UNIFY_KEY",
                value="${UNIFY_KEY}",
                description=(
                    "Unify API key the brain modules use to read/write "
                    "Orchestra contexts (CRM, Tasks, etc.)."
                ),
            ),
            Secret(
                name="UNIFY_PROJECT",
                value="${UNIFY_PROJECT:Brain}",
                description=(
                    "Orchestra project brain modules target.  Defaults to "
                    "'Brain'."
                ),
            ),
            Secret(
                name="UNITY_COMMS_URL",
                value="${UNITY_COMMS_URL}",
                description=(
                    "Base URL of the Unity gateway used by "
                    "brain.outbound.whatsapp for /whatsapp/send."
                ),
            ),
            Secret(
                name="FIREFLIES_API_KEY",
                value="${FIREFLIES_API_KEY}",
                description="Fireflies API key for transcript export and CRM sync.",
            ),
            Secret(
                name="LEMLIST_API_KEY",
                value="${LEMLIST_API_KEY}",
                description="Lemlist API key for evergreen outbound ticks.",
            ),
            Secret(
                name="GOOGLE_SERVICE_ACCOUNT_KEY_FILE",
                value="${GOOGLE_SERVICE_ACCOUNT_KEY_FILE}",
                description=(
                    "Service-account key path for Gmail / Drive delegation "
                    "used by brain.sync.gmail and brain.email."
                ),
            ),
            Secret(
                name="BRAIN_WHATSAPP_DEFAULT_RECIPIENT",
                value="${BRAIN_WHATSAPP_DEFAULT_RECIPIENT}",
                description=(
                    "Default operator WhatsApp number used by brain "
                    "scheduled jobs that emit notifications (e.g. the "
                    "HackerNews daily digest)."
                ),
            ),
        ],
        knowledge={
            "BrainOperator/JobOwnership": {
                "description": (
                    "Authoritative map of brain.scheduled job ids to the "
                    "FunctionManager wrapper that runs them."
                ),
                "seed_key": "job_id",
                "columns": {
                    "job_id": "str",
                    "wrapper": "str",
                    "module": "str",
                },
                "rows": [
                    {
                        "job_id": "intel.hackernews.daily_digest",
                        "wrapper": "run_hackernews_digest_to_whatsapp",
                        "module": "brain.intel.hackernews",
                    },
                    {
                        "job_id": "crm.gmail_sync",
                        "wrapper": "ingest_gmail_mailbox",
                        "module": "brain.sync.gmail",
                    },
                    {
                        "job_id": "crm.fireflies_sync",
                        "wrapper": "sync_fireflies_and_associate",
                        "module": "brain.sync.fireflies",
                    },
                    {
                        "job_id": "influencers.youtube.extract",
                        "wrapper": "run_youtube_browser_extraction",
                        "module": "brain.influencers.youtube.runner",
                    },
                ],
            },
        },
        function_dir=Path(__file__).parent / "functions",
    )
