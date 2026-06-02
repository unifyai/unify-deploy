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
    activation list is empty and no rows are materialised.

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
        # Secrets are provisioned directly in the assistant's Unity
        # SecretManager (Orchestra ``Secrets`` context), NOT declared here.
        #
        # The previous ``Secret(name=X, value="${X}")`` entries were a
        # footgun: the ``${X}`` env-substitution syntax is never expanded by
        # the seed path, so each one stored the *literal* string ``${X}`` and
        # then clobbered the pod's real value via
        # ``SecretManager._sync_dotenv`` (which does ``os.environ[k]=v`` for
        # every stored secret).  In particular ``UNIFY_KEY="${UNIFY_KEY}"``
        # overwrote the pod's valid key, so every Orchestra call 401'd and the
        # assistant could neither sync_custom nor seed tasks.
        #
        # Correct model: the runtime gets ``UNIFY_KEY`` / ``UNITY_COMMS_URL`` /
        # ``ORCHESTRA_ADMIN_KEY`` from the pod environment, and brain-specific
        # credentials (``X_CLIENT_ID``, ``X_CLIENT_SECRET``,
        # ``X_OAUTH_TOKENS_<USER>``, ``FIREFLIES_API_KEY``, ``LEMLIST_API_KEY``,
        # ``GOOGLE_SERVICE_ACCOUNT_KEY_FILE``, ``BRAIN_WHATSAPP_DEFAULT_RECIPIENT``)
        # are written into the SecretManager out-of-band.  ``brain`` defaults
        # ``UNIFY_PROJECT`` to ``Brain`` via ``os.environ.setdefault``.
        secrets=[],
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
