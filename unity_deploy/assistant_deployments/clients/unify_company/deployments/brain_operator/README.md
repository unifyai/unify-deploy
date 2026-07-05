# `brain_operator` deployment

Dedicated AI colleague under the `unify_company` tenant that owns every
recurring + trigger-based job declared in the brain repo's
`brain.scheduled` registry.

For the end-to-end operating pattern see the brain wiki page:
[`brain/docs/operations/scheduled-jobs.md`](https://github.com/unifyai/brain/blob/main/docs/operations/scheduled-jobs.md).

## What this deployment is

- A separate **deployment** under the existing `unify_company` client,
  not a new client.  Shares the tenant's Orchestra project, secrets
  bundle, and gateway routing.
- Activated for the **production** brain operator assistant (**1406**)
  on main-branch deploys only.  Staging deploys skip brain_operator.
  ``BRAIN_OPERATOR_ASSISTANT_ID`` overrides the id when set explicitly.
- Carries no per-customer state.  Customer-facing colleagues are
  separate deployments under separate clients.

## Structure

```
brain_operator/
├── README.md           # this file
├── __init__.py
├── deployment.py       # get_deployment() returns the DeploymentSpec
└── functions/          # @custom_function wrappers — FunctionManager.sync_custom picks these up
    ├── __init__.py
    ├── crm.py          # CRM Gmail / Fireflies / digest / hygiene / pipeline-pack ticks
    ├── outbound.py     # one wrapper per evergreen tick name
    ├── influencers.py  # YouTube browser-extraction trigger
    ├── intel.py        # HackerNews-to-WhatsApp daily digest (canonical example)
    ├── social.py       # IG/TikTok auto-publish (exported, disabled until armed)
    ├── helpers.py      # reserved (FunctionManager structural check)
    └── metrics.py      # reserved (FunctionManager structural check)
```

The recurring **schedule** lives in `brain.scheduled` (the brain
repo).  This package only owns the FunctionManager wrappers and the
`ScenarioActivation` that wires the brain-emitted template YAML to
this deployment's assistant id.

## Functions / job map

| Job id (`brain.scheduled`) | Wrapper here |
|---|---|
| `crm.gmail_sync` | `crm.ingest_gmail_mailbox` |
| `crm.fireflies_sync` | `crm.sync_fireflies_and_associate` |
| `crm.account_digest_refresh` | `crm.refresh_account_digests` (stub) |
| `crm.hygiene_review` | `crm.run_crm_hygiene_review` |
| `crm.pipeline_review_pack` | `crm.build_pipeline_review_pack` |
| `outbound.evergreen.<slug>.<tick>` | `outbound.run_evergreen_tick__<tick>` |
| `outbound.smartlead.reply_processor` | `outbound.run_smartlead_reply_processor` |
| `influencers.youtube.extract` | `influencers.run_youtube_browser_extraction` |
| `intel.hackernews.daily_digest` | `intel.run_hackernews_digest_to_whatsapp` |
| `intel.droid_outreach.hackernews_daily` | `intel.run_droid_outreach_hackernews_daily` |
| `intel.droid_outreach.reddit_daily` | `intel.run_droid_outreach_reddit_daily` |
| `intel.droid_outreach.discord_daily_summary` | `intel.run_droid_outreach_discord_daily_summary` |
| `intel.social_post.x_discover_draft` | `intel.run_social_post_discover_draft` |
| `intel.social_post.x_post_approved` | `intel.run_social_post_post_approved` |
| `intel.social_post.x_post_now` | `intel.run_social_post_now` |
| `social.ideate_and_generate` | `social.run_social_ideate_and_generate` (**disabled**) |
| `social.poll_reviews` | `social.run_social_poll_reviews` (**disabled**) |
| `social.render_storyboards` | `social.run_social_render_storyboards` (**disabled**) |
| `social.publish_approved` | `social.run_social_publish_approved` (**disabled**) |

## Scenario activation

`deployment.py` declares a `ScenarioActivation` pointing at
`brain_jobs/brain_jobs_v0` (an integration package that lives under
`unity_deploy/assistant_deployments/integrations/packages/brain_jobs/`
and ships a generated scenario YAML).  Activation behaviour:

| Env var | Effect |
|---|---|
| `BRAIN_OPERATOR_ASSISTANT_ID` (optional) | Defaults to **1406** on production; unset on staging (no activation). Stamped into every `tasks[*].target.assistant_id` at materialisation time. |
| `BRAIN_OPERATOR_TASKS_ENABLED` (optional) | Defaults to `true` on production, `false` on staging. When `true`, materialised rows ship enabled. Per-job `enabled: false` in scenario YAML still respected (e.g. `social.*`). |

Production/main Cloud Build passes both vars into the production reconcile job
(`deploy/cloudbuild.yaml`).  Staging Cloud Build omits them so brain_operator
is not synced on staging pushes.

The deploy-reconcile control plane materialises rows on every push (no
laptop step required).  Brain's `brain scheduled install --execute`
remains available for fast laptop iteration; both paths produce the
same `Tasks` rows.

## Operational env vars

These pass through `DeploymentSpec.secrets` to the assistant
runtime's `SecretManager`:

| Var | Purpose |
|---|---|
| `UNIFY_KEY` | Used by brain entrypoints to read/write Orchestra. |
| `UNISDK_PROJECT` | Defaults to `Brain`. |
| `UNITY_COMMS_URL` | Used by `brain.outbound.whatsapp.send`. |
| `FIREFLIES_API_KEY` | Used by `brain.sync.fireflies`. |
| `LEMLIST_API_KEY` | Used by the evergreen outbound wrappers. |
| `GOOGLE_SERVICE_ACCOUNT_KEY_FILE` | Used by `brain.sync.gmail`. |
| `BRAIN_WHATSAPP_DEFAULT_RECIPIENT` | Default operator recipient for digest jobs. |

## Adding a new function wrapper

```python
# brain_operator/functions/<area>.py
from unity.function_manager.custom import custom_function

@custom_function()
async def run_my_brain_thing(*, foo: str = "bar") -> dict:
    """Daily my-brain-thing tick."""
    from brain.<area>.<source> import do_the_thing
    return await do_the_thing(foo=foo)
```

Then:

1. Register the corresponding `BrainScheduledJob` in brain
   (`entrypoint_function="run_my_brain_thing"`).
2. Regenerate the scenario YAML: `brain scheduled export-scenario`.
3. Commit and deploy.

The wrapper MUST be `async`, MUST import brain inside the function
body (never at module scope — FunctionManager's isolation rule), and
MUST return a JSON-serialisable dict.

## Brain installation

The deploy image installs brain via `deploy/Dockerfile` at a build-arg
`BRAIN_REF` (defaults to `main`; production triggers pin a SHA).  See
`unity-deploy/deploy/Dockerfile` and the `_BRAIN_REF` substitution on
`deploy/cloudbuild.yaml`.

## Activation checklist (production)

brain_operator runs on production assistant **1406** (main branch only).

1. Connect integrations (Instagram, TikTok, Gmail, etc.) on assistant 1406
   in **production** Console.
2. Add brain-specific secrets to ``unify-deploy/.secrets.json`` under
   ``assistant.1406`` (see social README) and seed on deploy.
3. Add ``BRAIN_WHATSAPP_DEFAULT_RECIPIENT=<+447...>`` for digest jobs.
4. Run ``brain whatsapp register-recipient`` against production.
5. Deploy unify-deploy to **main** (production pipeline syncs 1406).
6. Disable individual jobs via per-task ``enabled: false`` in scenario YAML
   (``social.*`` ships disabled) rather than turning off the whole scenario.
