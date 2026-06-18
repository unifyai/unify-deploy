# Trigger Bridge Notes

This directory holds the versioned bridge configs that move hosted Droid image
rollouts from the public `droid` repository into the private canonical build
path in `unity-deploy`.

## Current State

**Both staging and production are cut over.** Live Cloud Build triggers now
run the private bridge flow:

| Environment | Bridge trigger | Private base trigger | Overlay trigger |
|-------------|----------------|---------------------|-----------------|
| **staging** | `droid-staging` (inline, fires on `droid@staging` push) | `droid-base-staging-private` (reads `unity-deploy/base/cloudbuild-staging.yaml`) | `unity-deploy-staging` |
| **production** | `droid-production-private-bridge` (inline, fires on `droid@main` push) | `droid-base-production-private` (reads `unity-deploy/base/cloudbuild.yaml`) | `unity-deploy` |

## Comms App Bridge

The hosted comms Cloud Run service (`droid-comms-app{,-staging}`) bundles
`droid.gateway` -- it clones `droid` inside `Dockerfile-comms` and composes on
top of `droid.gateway.app.create_app()`. The base/overlay chain above only
rebuilds the assistant Job image, so a `droid` change to the gateway code would
otherwise not reach the comms service until the next `unity-deploy` push. A
second, path-scoped bridge closes that gap:

| Environment | Comms bridge trigger | Fires on | Path filter | Runs |
|-------------|----------------------|----------|-------------|------|
| **staging** | `droid-comms-bridge-staging` (inline, from `droid-comms-staging-bridge.yaml`) | `droid@staging` push | `droid/gateway/**`, `requirements-gateway.txt` | `droid-comms-app-staging-unity-deploy` |
| **production** | `droid-comms-bridge-production` (inline, from `droid-comms-production-bridge.yaml`) | `droid@main` push | `droid/gateway/**`, `requirements-gateway.txt` | `droid-comms-app-unity-deploy` |

The path filter (`includedFiles`) keeps the comms service from rebuilding on
every `droid` push -- only communication-relevant changes fan out. The comms
build self-resolves the latest `droid@{staging,main}` head via `git ls-remote`
at build start (the `DROID_SHA` cache-buster in `cloudbuild/droid-comms-app*.yaml`),
so the bridge only needs to *run* the comms trigger -- no SHA threading. The
adapters service (`droid-adapters{,-staging}`) does **not** bundle `droid` and
is deliberately excluded.

The original `droid` trigger (which used to build directly from
`droid/deploy/cloudbuild.yaml` on the public repo) has been retired -- its
branch filter was set to `^__disabled_cutover_20260416__$` in April 2026 so
that it could not fire on any real branch, and was deleted in May 2026.

The preview deployment workflow has been retired entirely: the
`droid-preview` trigger and `cloudbuild-preview.yaml` configs were
deleted along with the rest of the preview tooling.

## Production Cutover Validation (completed)

The production cutover was validated on 2026-05-25 via the Phase C.5
droid@main fast-forward:

| Step | Build ID | Trigger | Outcome |
|------|----------|---------|---------|
| 1 | `f9c41dd4-a213-...` | `droid-production-private-bridge` | SUCCESS (fired on droid@main `7885f958c`) |
| 2 | `974cf979-9ac2-...` | `droid-base-production-private` | SUCCESS (built `droid-base:7885f958c`) |
| 3 | `56d0d282-bd7d-...` | `unity-deploy` | SUCCESS (overlay built on top) |

End-to-end chain (push to running Droid Job image) verified clean.

## Important Invariants

- **The `droid-base-{staging,production}-private` triggers must keep cloning
  `droid` from the source-of-truth branch (`staging` or `main`) and clobbering
  `droid/deploy/` with `unity-deploy/base/` before building.** This is what
  makes `unity-deploy/base/` the canonical hosted deploy source.
- **`droid/deploy/cloudbuild{,-staging}.yaml` are no longer the canonical
  config for production / staging image builds.** They remain in the open-source
  `droid` repo as a reference implementation (OSS users self-deploying Droid
  can use them as a starting point), but the SaaS triggers ignore them.
