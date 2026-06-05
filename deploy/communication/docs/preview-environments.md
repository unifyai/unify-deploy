# Preview Environments

Per-feature-branch deployment isolation that lets you exercise the
full Console → Orchestra → Comms App → Adapters → Unity-container path
without disturbing shared staging traffic.

A push to ``feature/<name>`` on any of the participating sibling
repositories (``communication``, ``orchestra``, ``unity``,
``unity-deploy``, ``console``) builds a slug-tagged image and deploys
it as a tagged Cloud Run revision against the corresponding ``-staging``
service with ``--no-traffic``.  The canonical staging URL keeps serving
the un-tagged revision for the rest of the team; only the tagged URL
``https://<slug>---<canonical-host>`` routes to the feature build.
Unity images are uploaded to a per-branch GCS blob
``image_hash_staging_<slug>.txt``, which a tagged Comms App revision
reads via ``Settings.image_hash_blob`` (it follows ``BRANCH_TAG``).
The tagged Comms App writes that resolved image URI into
``AssistantSession.spec.imageOverride``; the AssistantSession
controller spawns a fresh Job pinned to that image instead of claiming
a shared idle container, so the in-cluster runtime that serves a
preview session also runs the feature-branch Unity build.

## Mental model

Tagged routing covers **every code path whose entry point is a request
you originate against a tagged URL**.  Anything kicked off by:

- Cloud Scheduler crons,
- third-party webhooks (Twilio, Gmail, Microsoft, Teams),
- the AssistantSession controller running in GKE,
- VM startup scripts,
- Pub/Sub messages produced by the un-tagged staging revision,
- Cloud Tasks enqueued before the preview deploy,

runs the **un-tagged staging code** because those entry points target
the canonical hostnames hard-wired at provisioning time.  Preview
isolation does not attempt to fork those singletons.  In practice this
covers ~95% of feature work — anything you can drive by clicking around
in the console is end-to-end your branch.

## What gets isolated

| Tier | How | Repo |
|------|-----|------|
| Console (Next.js) | Tagged Cloud Run revision; peer URLs injected as runtime env vars | ``console`` |
| Orchestra API | Tagged Cloud Run revision; shares staging Cloud SQL | ``orchestra`` |
| Comms App | Tagged Cloud Run revision; reads per-branch ``image_hash_staging_<slug>.txt`` via ``BRANCH_TAG`` | ``communication`` |
| Adapters | Tagged Cloud Run revision; peer URLs pinned to tagged orchestra/comms | ``communication`` |
| Unity base image | ``unity-base-staging:preview-<slug>-<sha>`` | ``unity`` |
| Unity overlay image | ``unity-staging:preview-<slug>-<sha>`` plus ``image_hash_staging_<slug>.txt`` | ``unity-deploy`` |
| Unity container in GKE | ``AssistantSession.spec.imageOverride`` makes the controller spawn a fresh Job on the per-branch image, bypassing the shared idle pool | ``communication`` |

Schema migrations are not isolated.  Push migrations through the regular
``staging`` flow before doing preview testing.

## One-time setup

The Cloud Build triggers exist already (they were originally configured
to fire on a single shared ``preview`` branch); update them to fire on
``feature/.+`` instead.  Run once per project:

```bash
gcloud config set project gcp-project-runtime

gcloud builds triggers update unity-comms-app-preview \
  --branch-pattern='^feature/.+$' \
  --build-config=cloudbuild/unity-comms-app-preview.yaml
gcloud builds triggers update adapters-preview \
  --branch-pattern='^feature/.+$' \
  --build-config=cloudbuild/adapters-preview.yaml
gcloud builds triggers update unity-preview \
  --branch-pattern='^feature/.+$' \
  --build-config=deploy/cloudbuild-preview.yaml
gcloud builds triggers update unity-deploy-preview \
  --branch-pattern='^feature/.+$' \
  --build-config=deploy/cloudbuild-preview.yaml
```

```bash
gcloud config set project gcp-project-saas

gcloud builds triggers update unify-orchestra-preview \
  --branch-pattern='^feature/.+$' \
  --build-config=deploy/cloudbuild_preview.yaml \
  || gcloud builds triggers create github \
       --name=unify-orchestra-preview \
       --repo-name=orchestra --repo-owner=unifyai \
       --branch-pattern='^feature/.+$' \
       --build-config=deploy/cloudbuild_preview.yaml

gcloud builds triggers update unify-console-preview \
  --branch-pattern='^feature/.+$' \
  --build-config=cloudbuild_preview.yaml \
  || gcloud builds triggers create github \
       --name=unify-console-preview \
       --repo-name=console --repo-owner=unifyai \
       --branch-pattern='^feature/.+$' \
       --build-config=cloudbuild_preview.yaml
```

The Comms-App service account needs ``run.developer`` plus
``run.invoker`` on ``orchestra-staging`` and ``unity-adapters-staging``
to deploy tagged revisions; the staging service account already has it
since regular staging deploys exercise the same call.

## Daily workflow

End-to-end isolation requires a tagged revision in **every** repo
between the user's request and the spawned Unity container —
``console`` for the frontend, ``communication`` for the tagged Comms
App / Adapters that resolves the override image, and ``unity`` for
the per-branch Unity build.  The CLI publishes pass-through branches
for any of those three that doesn't already have one, so a single
real feature branch in one repo is enough to drive the full pipeline:

```bash
cd ~/Unify/unity                       # or whichever repo holds the change
git checkout -b feature/<name>
# ... edits ...
git push -u origin feature/<name>      # fires the unity preview build

cd ~/Unify/unity-deploy
./scripts/preview.py up                # creates pass-through feature/<name>
                                       # branches on origin in console and
                                       # unity-deploy so their preview
                                       # builds fire too — local working
                                       # trees and branches are untouched.
./scripts/preview.py status            # shows tagged-revision status
./scripts/preview.py open              # opens the tagged Console URL
```

``up`` skips repos whose ``feature/<name>`` branch already exists on
origin (typically the repo holding the real work), so it never
clobbers an in-flight branch.  Pass ``--force`` to re-base existing
pass-throughs against the latest ``origin/staging`` tip.  Pass
``--base-branch=<name>`` if the integration branch in your fork is not
``staging``.

The slug is derived from the feature branch name automatically when
exactly one sibling repo is on a ``feature/*`` branch; otherwise pass
``--slug=<value>`` explicitly.

When you're done with a slug, tear it back down:

```bash
./scripts/preview.py down              # deletes feature/<name> from origin
                                       # in console, communication, and any
                                       # other pass-through repos
./scripts/preview.py cleanup           # drops tagged Cloud Run revisions
                                       # and the Unity image-hash blob
```

## Cleanup

Tagged Cloud Run revisions are free unless invoked, but the traffic
table gets noisy after a busy week.  Run periodically:

```bash
./scripts/preview.py cleanup --age-days=14 --dry-run
./scripts/preview.py cleanup --age-days=14
```

This removes both the Cloud Run tags and the matching
``image_hash_staging_<slug>.txt`` blobs in GCS.  The
``preview.py down`` command above only deletes the remote
``feature/<slug>`` refs; ``cleanup`` is what frees the actual deployed
artifacts.

## Caveats

- **Cloud Scheduler cron flows are env-wide.**  ``infra-maintenance-staging``,
  ``email-watches-staging``, etc. fire against the canonical Adapters URL.
  To exercise a cron-driven flow on a feature branch, ``curl`` the tagged
  Adapters URL manually instead of waiting for the scheduler.
- **AssistantSession controller binary is a GKE singleton.**  Every
  preview Comms App creates AssistantSession CRDs that the same
  staging controller reconciles, and the Unity Jobs it spawns honour
  ``spec.imageOverride`` so each preview session runs the feature
  branch's Unity image end-to-end.  Changing the *controller logic*
  itself still requires a different strategy (own namespace, separate
  controller deployment, or push through staging) — only the Jobs it
  manages are isolated by the override.
- **Twilio/Gmail/Teams webhooks reach the un-tagged Adapters URL.**  Use
  test fixtures to drive webhook flows on a preview branch.
- **Schema migrations share the staging Cloud SQL instance.**  Push
  Alembic migrations to ``staging`` before running preview tests.
- **Service-to-service hops must use tagged peer URLs.**  The preview
  cloudbuilds set ``ORCHESTRA_URL`` / ``UNITY_COMMS_URL`` /
  ``UNITY_ADAPTERS_URL`` on each tagged revision so this is wired up
  automatically.  If you add new service-to-service traffic to a feature
  branch, route it through the existing ``Settings.*_url`` accessors so
  the env-var override path picks up the tagged peer.
