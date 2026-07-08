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

from unity_deploy.assistant_deployments.clients.unify_company._brain_operator import (
    brain_operator_assistant_id,
    brain_operator_tasks_enabled,
)
from unity_deploy.assistant_deployments.configs.types.actor_config import ActorConfig
from unity_deploy.assistant_deployments.deployment_types import DeploymentSpec
from unity_deploy.assistant_deployments.scenarios.types import ScenarioActivation

_DIR = Path(__file__).resolve().parent


def _scenario_activations() -> list[ScenarioActivation]:
    """Return the brain_jobs scenario activations for this deployment.

    The assistant id comes from ``BRAIN_OPERATOR_ASSISTANT_ID`` (Cloud Build
    substitution → reconcile job + overlay image ENV).  Resolving from env —
    rather than hardcoding per environment — keeps staging and production
    deploys aligned with the same code path.

    ``tasks_enabled`` defaults true; per-job ``enabled: false`` in the
    scenario YAML still gates individual jobs (e.g. ``social.*``).
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
        guidance_dir=_DIR / "guidance",
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
        # ``UNISDK_PROJECT`` to ``Brain`` via ``os.environ.setdefault``.
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
                    {
                        "job_id": "intel.teammate_outreach.hackernews_daily",
                        "wrapper": "run_teammate_outreach_hackernews_daily",
                        "module": "brain.intel.teammate_outreach",
                    },
                    {
                        "job_id": "intel.teammate_outreach.reddit_daily",
                        "wrapper": "run_teammate_outreach_reddit_daily",
                        "module": "brain.intel.teammate_outreach",
                    },
                    {
                        "job_id": "intel.teammate_outreach.discord_daily_summary",
                        "wrapper": "run_teammate_outreach_discord_daily_summary",
                        "module": "brain.intel.teammate_outreach",
                    },
                    {
                        "job_id": "social.x_discover_draft",
                        "wrapper": "run_social_post_discover_draft",
                        "module": "brain.social.curate",
                    },
                    {
                        "job_id": "social.x_post_approved",
                        "wrapper": "run_social_post_post_approved",
                        "module": "brain.social.curate",
                    },
                    {
                        "job_id": "social.x_post_now",
                        "wrapper": "run_social_post_now",
                        "module": "brain.social.curate",
                    },
                    {
                        "job_id": "social.linkedin_discover_draft",
                        "wrapper": "run_social_linkedin_discover_draft",
                        "module": "brain.social.curate",
                    },
                    {
                        "job_id": "social.linkedin_login",
                        "wrapper": "run_social_linkedin_login",
                        "module": "brain.social.discovery.sources",
                    },
                    {
                        "job_id": "social.linkedin_post_approved",
                        "wrapper": "run_social_linkedin_post_approved",
                        "module": "brain.social.curate",
                    },
                    {
                        "job_id": "social.ideate_and_generate",
                        "wrapper": "run_social_ideate_and_generate",
                        "module": "brain.social.create.pipeline",
                    },
                    {
                        "job_id": "social.poll_reviews",
                        "wrapper": "run_social_poll_reviews",
                        "module": "brain.social.create.pipeline",
                    },
                    {
                        "job_id": "social.render_storyboards",
                        "wrapper": "run_social_render_storyboards",
                        "module": "brain.social.create.pipeline",
                    },
                    {
                        "job_id": "social.publish_approved",
                        "wrapper": "run_social_publish_approved",
                        "module": "brain.social.create.pipeline",
                    },
                    {
                        "job_id": "gtm.stargazer.poll",
                        "wrapper": "run_gtm_stargazer_poll_tick",
                        "module": "brain.gtm",
                    },
                    {
                        "job_id": "gtm.stargazer.enrich",
                        "wrapper": "run_gtm_stargazer_enrich_tick",
                        "module": "brain.gtm",
                    },
                    {
                        "job_id": "gtm.smartlead.reconcile",
                        "wrapper": "run_gtm_smartlead_reconcile_tick",
                        "module": "brain.gtm",
                    },
                    {
                        "job_id": "gtm.outbound.replenish",
                        "wrapper": "run_gtm_outbound_replenish_inventory",
                        "module": "brain.gtm",
                    },
                ],
            },
        },
        function_dir=_DIR / "functions",
        data_dir=_DIR / "data",
    )
