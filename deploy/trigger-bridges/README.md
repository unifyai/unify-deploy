# Trigger Bridge Notes

This directory holds the versioned bridge configs that move hosted Unity image
rollouts from the public `unity` repository into the private canonical build
path in `unity-deploy`.

## Current State

**Both staging and production are cut over.** Live Cloud Build triggers now
run the private bridge flow:

| Environment | Bridge trigger | Private base trigger | Overlay trigger |
|-------------|----------------|---------------------|-----------------|
| **staging** | `unity-staging` (inline, fires on `unity@staging` push) | `unity-base-staging-private` (reads `unity-deploy/base/cloudbuild-staging.yaml`) | `unity-deploy-staging` |
| **production** | `unity-production-private-bridge` (inline, fires on `unity@main` push) | `unity-base-production-private` (reads `unity-deploy/base/cloudbuild.yaml`) | `unity-deploy` |

## Consolidated deploy orchestrator (comms + adapters folded in)

The hosted comms Cloud Run service (`unity-comms-app{,-staging}`) bundles
`unity.gateway` -- it clones `unity` inside `Dockerfile-comms` and composes on
top of `unity.gateway.app.create_app()`.

The overlay build is now a single **orchestrator** per environment
(`deploy/cloudbuild-staging.yaml` / `deploy/cloudbuild.yaml`, triggers
`unity-deploy-staging` / `unity-deploy`) that:

- runs a concurrency guard first (`deploy/scripts/cloudbuild/concurrency_guard.sh`)
  to cancel duplicate-webhook and superseded builds of the same trigger;
- builds overlay + comms + adapters + session-controller in parallel;
- deploys only services whose image digest changed (comms/adapters gate via
  `cloud_run_deploy_needed.sh`; the overlay always deploys because its image
  tracks the whole `unity-deploy` source);
- runs exactly one idle-pool refresh (`refresh_idle_pool.sh`) after the comms
  revision takes traffic and the GCS image hash is updated.

Because the orchestrator always rebuilds comms, a `unity` gateway change reaches
comms via the `unity-staging`/`unity-*-private-bridge` -> base -> overlay chain
with no separate comms bridge. The former per-service triggers
(`unity-comms-app{,-staging}-unity-deploy`, `unity-adapters{,-staging}-unity-deploy`,
`unity-comms-bridge-{staging,production}`) and their standalone configs
(`cloudbuild/unity-comms-app{,-staging}.yaml`, `cloudbuild/adapters{,-staging}.yaml`,
`unity-comms-*-bridge.yaml`) are retired. The adapters service does **not**
bundle `unity`.

The overlay image identity (`_UNITY_SHA`) is the Unity brain SHA: passed by the
base-build chain on a `unity` push, or resolved on a direct `unity-deploy` push
from `gs://bucket/unity_base_sha{,_staging}.txt` (published by the base
build). The orchestrator is the sole writer of `gs://bucket/image_hash{,_staging}.txt`.

The original `unity` trigger (which used to build directly from
`unity/deploy/cloudbuild.yaml` on the public repo) has been retired -- its
branch filter was set to `^__disabled_cutover_20260416__$` in April 2026 so
that it could not fire on any real branch, and was deleted in May 2026.

The preview deployment workflow has been retired entirely: the
`unity-preview` trigger and `cloudbuild-preview.yaml` configs were
deleted along with the rest of the preview tooling.

## Production Cutover Validation (completed)

The production cutover was validated on 2026-05-25 via the Phase C.5
unity@main fast-forward:

| Step | Build ID | Trigger | Outcome |
|------|----------|---------|---------|
| 1 | `f9c41dd4-a213-...` | `unity-production-private-bridge` | SUCCESS (fired on unity@main `7885f958c`) |
| 2 | `974cf979-9ac2-...` | `unity-base-production-private` | SUCCESS (built `unity-base:7885f958c`) |
| 3 | `56d0d282-bd7d-...` | `unity-deploy` | SUCCESS (overlay built on top) |

End-to-end chain (push to running Unity Job image) verified clean.

## Important Invariants

- **The `unity-base-{staging,production}-private` triggers must keep cloning
  `unity` from the source-of-truth branch (`staging` or `main`) and clobbering
  `unity/deploy/` with `unity-deploy/base/` before building.** This is what
  makes `unity-deploy/base/` the canonical hosted deploy source.
- **`unity/deploy/cloudbuild{,-staging}.yaml` are no longer the canonical
  config for production / staging image builds.** They remain in the open-source
  `unity` repo as a reference implementation (OSS users self-deploying Unity
  can use them as a starting point), but the SaaS triggers ignore them.
