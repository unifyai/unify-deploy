<!--
    GENERATED FILE - DO NOT EDIT DIRECTLY.

    Regenerate with:  python3 .agents/global-rules/build_agents_md.py

    Edit the sources instead:
      .agents/repo.md              this repo's overview and always-on guidance
      .agents/rules/*.md           this repo's own rules
      .agents/shared.txt           which shared rules this repo includes
      .agents/global-rules/rules/  rules shared across all unifyai repos
                                   (submodule: unifyai/global-agent-rules)
-->

# Self-Host & Local-Stack Ownership

The open-source repo set is `unify`, `unisdk`, and `unillm`. `orchestra` and `console` are private/hosted. The open-source `unify` public path runs the agent runtime locally against the **hosted** Orchestra backend (`ORCHESTRA_URL`, default `https://api.unify.ai/v0`) with **no** Console.

The "all-repo fully local" self-host stack (local Orchestra + Console + Coordinator + gateway, via Docker Compose or sibling source checkouts) now lives in the private **`unify-deploy`** repo (https://github.com/unifyai/unify-deploy), under:

- `unify-deploy/selfhost/` — orchestration scripts: `stack.sh`, `setup.sh`, `service.sh`, `install-compose.sh`, `compose-cli.sh`, `self_host_env.sh`, `self_host_runtime.sh`, `self_host_desktop.sh`, `reset_db.sh`, `ensure_prereqs.sh`, `fresh_install_smoke.sh`, `install_progress.sh`, and the full BYOK wizard (`prompt_byok_keys.sh`).
- `unify-deploy/deploy/selfhost/` — the Docker Compose bundle (compose file, Caddyfile, entrypoints, env example, LiveKit config, integration-bootstrap manifest, `fetch_assistant_*.py`, and the managed-desktop helpers `ensure_self_host_desktop_ssh_key.py` / `publish_self_host_desktop_ready.py`).
- `unify-deploy/.github/workflows/ghcr-selfhost.yml` — self-host image publishing.

These scripts resolve the sibling `unify`/`console`/`orchestra` checkouts under `UNIFY_STACK_ROOT` (defaults to the parent of `unify-deploy`).

## What stays in open-source `unify`

- `scripts/local.sh` — runs the `ConversationManager` + `unify.gateway` against `ORCHESTRA_URL` (the headless public run path; `unify serve`).
- `scripts/install.sh` — the minimal public installer (clone `unify`, `uv sync`, write `.env`, install the `unify` CLI shim). No Console/Orchestra clone, no Compose.
- `scripts/prompt_byok_keys.sh` — trimmed BYOK wizard (no Composio / workspace-OAuth prompts).
- `--live-voice` sandbox support — uses configured LiveKit Cloud credentials; do not reintroduce local media-server bootstrapping.
- `scripts/seed_builtins_catalog.py` — ships **inside** the unify application image (`/app/scripts/seed_builtins_catalog.py`) and is invoked there by both the hosted K8s seed job (`unify-deploy/deploy/scripts/run_seed_builtins_artifacts_job.sh`) and the self-host compose; it is also covered by a unify test. It must stay in `unify`.
- The `unify` package runtime (onboarding handlers, `console_ui.py`, screen-share, Composio runtime, demo mode) stays in place; it is simply dormant without the hosted features.

## Rules

- Never add open-source `unify` code, scripts, or docs that clone, boot, or wire a local `console`/`orchestra`. If a change needs the full local stack, it belongs in `unify-deploy/selfhost/`.
- If a plan or doc references `unify/scripts/stack.sh`, `unify/scripts/setup.sh`, or `unify/deploy/selfhost/`, treat it as stale and translate it to the corresponding `unify-deploy` path.
- Integration tests that drive `stack.sh` resolve it at `unify-deploy/selfhost/stack.sh` (via the sibling `unify-deploy` checkout).
- Use `unify-deploy/selfhost/stack.sh reset` (or `unify-deploy/selfhost/reset_db.sh`) to purge local self-host onboarding/chat history while preserving the manually-created local owner when one exists; otherwise it returns to the pre-signup state with no users or assistants. Do not add reset/purge scripts for the full local stack in `unify`, `console`, or `orchestra`.

---

# Hosted Communication Ownership

The `communication` repo (https://github.com/unifyai/communication) is archived. Do not edit it unless the user explicitly asks for historical archaeology in the archived repo.

Hosted communication runtime, adapters, deployment infrastructure, and hosted comms tests now live in the `unify-deploy` repo (https://github.com/unifyai/unify-deploy).

## Components

### Hosted Adapters
Active unauthenticated webhook handlers live under `unify-deploy/adapters/`:
- `twilio_call_webhook`: Inbound voice calls (TwiML conference setup + Pub/Sub dispatch)
- `twilio_msg_webhook`: Inbound SMS messages
- Gmail watch renewal and Pub/Sub notification processing

### Hosted Communication API
Active JSON endpoints protected by admin API key live under `unify-deploy/communication/`:
- **Phone** (`/phone`): Call control, SMS sending, number provisioning, agent dispatch
- **Gmail** (`/gmail`): Email sending, watch management
- **Outlook** (`/outlook`): Email sending, subscription management
- **Social** (`/social`): Verification code generation over SMS

### Tests
Hosted comms tests live under `unify-deploy/tests/communication/`.

## Position in the System

When Unify's `ConversationManager` needs hosted phone, SMS, email, WhatsApp, or deployment-backed communication, the active implementation is in `unify-deploy`. The open/local gateway boundary remains in the `unify` repo (https://github.com/unifyai/unify) under `unify.gateway`.

If a plan or old document references the archived `communication` repo, `communication/adapters/...`, or `communication/tests/...`, treat it as stale and translate the change to the corresponding path in `unify-deploy` unless the user explicitly says otherwise.

## Related Repositories

- **unify**: Local/open gateway and assistant runtime
- **unify-deploy**: Hosted communication, adapters, infra, deployment overlays, and hosted tests
- **orchestra**: Authenticates admin operations, stores assistant phone numbers
- **console**: Can trigger communications through Orchestra-hosted routes
- **unisdk**: May be used for logging communication events
- **unillm**: May be used for any LLM-powered communication features

## Mirrored Artifacts

- **OAuth Scopes** (`common/scopes.py`): Mirrored in Orchestra's `web/api/assistant/scopes.py`. Any change MUST be applied in both repos in the same changeset. See `scopes-mirror.md`.

## Shipping assistant-pod changes: the image leads, always

The pod manifest is in this repo; the code its containers run is in `unify`.
They ship on different pipelines, and **a push to `unify` does not reach pods
on its own** — the base image build is a *manual* trigger
(`unity-base-{staging,production}-private`). A `unify` build can report
SUCCESS while pods keep running the old code indefinitely.

So when a change spans both repos, promote `unify` first, run the base build,
**verify the built image actually contains the change**, and only then promote
this repo. Reversed, production spawns pods whose manifest references code the
image lacks — and since these changes usually also remove what the old shape
relied on (a provider key, an env var), there is no fallback and
`restartPolicy: Never` means every new pod stays dead.

Verifying against a local checkout is **not** verifying what ships. Run the
image. Nothing in CI enforces this ordering; it is checked by reading
`gs://bucket/{unity_base_sha,image_hash}[_staging].txt`.

Full pipeline, commands, and the 2026-08-11 near-miss that produced this rule:
README §8.

---

# Repository rules

# Authoring Workflow Bundles

Curated workflow bundles live under
`unify_deploy/assistant_deployments/workflows/<slug>/` — one directory per
workflow, `manifest.yaml` + content dirs (`guidance/`, `knowledge/`,
`tasks/`, `functions/`), following the integration-package layout. unify's
loader (`unify/workflow_manager/catalog.py`) is strict; the slug must equal
the directory name (it is the identity stamped on every planted row).

Everything below exists because a bundle is **read before it is run**: the
seed publishes the manifest and every artifact's substance verbatim to the
public-read Builtins project (`Workflows/Catalog` + `Workflows/Content`),
where Console renders it to a user deciding whether to install. Write every
field for that reader.

## Copy standards

- **`description` is the card**: one sentence, no more. **`about` is the
  page**: multi-paragraph markdown covering what the workflow does, when it
  runs, what arrives, and how its settings shape it. A new user must
  understand the workflow from `about` alone — no phrase salad, no bullet
  fragments standing in for sentences.
- **Params must explain themselves.** Every `params_schema` entry carries a
  `label` a human would say and a `help` that gives an example and states
  what happens when the field is left empty. A param whose meaning needs
  the manifest comments to decode is not done.
- **Artifact prose is user-facing.** Guidance/knowledge `title` and
  `content`, task `name` and `description`, and function docstrings are all
  published to the shelf and rendered in Console previews. Write them as
  finished documents, not internal notes.
- Vocabulary is unify's product-vocabulary rule: a **workflow** is the
  installable package; what it plants are **procedures**, **claims**,
  **tasks** and **functions**. Never "recurring workflow".

## Requirement slugs: validate before you ship

A requirement's `slug` is a **provider app slug** — the id space Console's
integrations gallery, `app_slug` in the integrations primitives, and native
package manifests share (`gmail`, `hubspot`, `notion`). It is NOT the OAuth
alias space in `runtime_oauth` (`google`/`google_workspace`), which the
gallery cannot see — a wrong slug renders a grey chip with no logo and no
connect action, and the requirement can never read as connected.

Validate every slug against the seeded Builtins integrations catalogue
before shipping the bundle (any valid user key works — the project is
public-read; apps rows key on `canonical_app_slug`, normalized
`[^a-z0-9]+ → _`):

```bash
curl -s --get "$ORCHESTRA_URL/logs" \
  --data-urlencode "project_name=Builtins" \
  --data-urlencode "context=Integrations/Apps" \
  --data-urlencode 'filter=canonical_app_slug == "<slug>"' \
  -H "Authorization: Bearer $UNIFY_KEY"
```

A slug is valid when that returns a row **or** the app is a BYOD OAuth
provider with no gallery row and no native package — then, and only then,
declare `required_secrets` (e.g. `GOOGLE_REFRESH_TOKEN` for Workspace) so
the resolver has a signal. For gallery apps and native packages, omit
`required_secrets`: their own authority already defines what connecting
means, and restating secrets here is two places to update.

Note the environment: a fresh local stack has an empty
`Integrations/Apps` (the provider bootstrap is a separate, expensive step),
so run the check against staging, or accept the local emptiness as "cannot
validate here", never as "the slug is wrong".

## Version discipline

- New bundles start **below 1.0.0** (`0.0.1`, …) and stay there while the
  workflow is being iterated on.
- Promotion to `1.0.0` is one thing only: the staging acceptance walk
  proving the workflow's job actually runs end to end (for
  `daily_briefing`: the briefing composes and delivers at 08:30 against a
  connected mailbox).
- Any content or manifest change bumps the version — the catalogue hash
  re-seeds Builtins and the boot reconcile carries the bump to existing
  installations.

## Tables and canvases

A bundle may ship **table schemas** under `data/<Path>/To/Table/meta.json`
and **canvas source** under `canvas/<view>/{view.tsx,view.json}`.

- **Tables are schemas only.** No `rows.jsonl`. A bundle is published
  verbatim and installed identically by everyone, so rows in it are one
  author's data handed to every installer; the table is the contract and
  the workflow's own job fills it. The loader refuses a bundle that ships
  rows.
- **Table contexts are normalised into the Data namespace.** Write
  `data/Finance/Invoices/`; it installs as `Data/Finance/Invoices`. A
  context outside that namespace cannot be installed to a team and cannot
  be read by a canvas.
- **Canvases ship source, never a built artifact.** The install compiles
  each `view.tsx` against the canvas kit the deployment actually has. A
  pre-built `bundle.js` would pin a host runtime and break at *view* time,
  for the user, with nothing failing at plant time — so the loader refuses
  one. `view.json` carries the title, description, bindings, actions and
  visibility the view publishes under; its bindings are validated at
  curation time, so a plausible-looking but wrong shape fails the PR rather
  than someone's install.
- A canvas that binds to a table the same bundle declares is the intended
  pairing: the recurring job keeps the table current and the view reads it
  live, so nothing has to be regenerated per day.

## Tasks are complete definitions

A `tasks.jsonl` row is the whole scheduled contract, using the Task model's
own fields rather than prose standing in for them:

- **Timing is structured.** Cadence lives in `repeat`, a one-off start in
  `schedule`, an event in `trigger` — never described only in the
  `description`. A repeat rule alone is a fully scheduled task; it needs no
  `schedule` anchor.
- **Delivery lives in `response_policy`.** How the run answers the user —
  one chat message, drafts only, post to the configured channel — is the
  `response_policy` field, which the run request renders as its own
  section. The `description` stays procedural: what to read, which stored
  procedures to follow, which of the bundle's functions to use.
- **State non-default fields explicitly** (`priority`, `offline`,
  `max_runtime_seconds`) when the job needs them; omit them when the
  default is the intent.

## No platform tool names in bundle prose

Task descriptions and guidance never name platform tools, primitives, or
call syntax (`WorkflowManager_*`, `primitives.*`, anything ending in
parentheses). The actor reads that prose while writing code, and a
tool-shaped reference gets pasted into a plan where the name does not
exist — two staging runs died on exactly that NameError. What a run needs
is delivered to it instead: the scheduler resolves the workflow's recorded
installation settings onto every run request, so prose says "the
installation settings included with this run", never how to fetch them.
Referencing the bundle's **own** functions and procedures by name is
correct and required — that is the linkage `function_names` records.

## What never goes in a bundle

Secrets or OAuth values (requirements are declared, never carried), contact
rows or transcripts (runtime-populated by the workflow's own functions),
seeded data rows (schemas only — see above), and pre-built canvas
bundles.
