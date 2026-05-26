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

The original `unity` trigger (which used to build directly from
`unity/deploy/cloudbuild.yaml` on the public repo) has been retired -- its
branch filter was set to `^__disabled_cutover_20260416__$` in April 2026 so
that it could not fire on any real branch, and was deleted in May 2026.

**Preview is intentionally not bridged.** The `unity-preview` trigger still
fires on `unity` repo `^feature/.+$` push events and runs
`unity/deploy/cloudbuild-preview.yaml` directly. Preview environments share
the staging image repo (`unity-base-staging:preview-<slug>-<sha>` tag), reuse
staging's job-watcher, and skip the customer-overlay control-plane reconcile,
so they don't need the bridge round-trip. If preview ever grows to need a
customer overlay or its own base build, mirror the staging/production pattern.

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
- **`unity/deploy/cloudbuild-preview.yaml` is still live** for the
  `unity-preview` trigger. Don't touch it without coordinating the preview
  flow.

## When to extend the bridge pattern to preview

Add a `unity-preview-private-bridge` + `unity-base-preview-private` only if:

- Preview environments need access to the customer-overlay seeds /
  integrations from `unity-deploy/unity_deploy/customization/`, or
- Preview needs to diverge from staging's job-watcher / pipeline workers, or
- You want preview base builds to read `unity-deploy/base/` rather than
  `unity/deploy/`.

Today none of these apply -- preview is deliberately a "staging base image
with a different tag" workflow.
