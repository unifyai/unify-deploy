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
- Activated for the assistant id named by the
  `BRAIN_OPERATOR_ASSISTANT_ID` env var.  Without that env var, the
  brain_operator simply doesn't activate.
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
| `influencers.youtube.extract` | `influencers.run_youtube_browser_extraction` |
| `intel.hackernews.daily_digest` | `intel.run_hackernews_digest_to_whatsapp` |

## Scenario activation

`deployment.py` declares a `ScenarioActivation` pointing at
`brain_jobs/brain_jobs_v0` (an integration package that lives under
`unity_deploy/assistant_deployments/integrations/packages/brain_jobs/`
and ships a generated scenario YAML).  Activation behaviour:

| Env var | Effect |
|---|---|
| `BRAIN_OPERATOR_ASSISTANT_ID` (optional) | Stamped into every `tasks[*].target.assistant_id` at materialisation time.  When unset, no scenario is activated.  When set to a missing assistant id, deploy-time reconcile skips the optional target with a warning. |
| `BRAIN_OPERATOR_TASKS_ENABLED` (optional, default `false`) | When `true`, materialised rows ship enabled.  When `false`, rows ship disabled and the operator flips them after seeding. |

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
| `UNIFY_PROJECT` | Defaults to `Brain`. |
| `DROID_COMMS_URL` | Used by `brain.outbound.whatsapp.send`. |
| `FIREFLIES_API_KEY` | Used by `brain.sync.fireflies`. |
| `LEMLIST_API_KEY` | Used by the evergreen outbound wrappers. |
| `GOOGLE_SERVICE_ACCOUNT_KEY_FILE` | Used by `brain.sync.gmail`. |
| `BRAIN_WHATSAPP_DEFAULT_RECIPIENT` | Default operator recipient for digest jobs. |

## Adding a new function wrapper

```python
# brain_operator/functions/<area>.py
from droid.function_manager.custom import custom_function

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

## Activation checklist

1. Provision a new assistant in Orchestra for the brain operator
   inside the `unify_company` tenant.  Note the numeric id.
2. Add `BRAIN_OPERATOR_ASSISTANT_ID=<id>` to the production Cloud Run
   secret bundle.
3. Add `BRAIN_WHATSAPP_DEFAULT_RECIPIENT=<+447...>` for the operator's
   number.
4. Run `brain whatsapp register-recipient` so the gateway can route to
   that recipient.
5. Deploy.  Tasks rows ship disabled.
6. Flip `BRAIN_OPERATOR_TASKS_ENABLED=true` (or enable rows manually)
   to start firing.
