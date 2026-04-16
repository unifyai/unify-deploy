# Trigger Bridge Notes

This directory holds the versioned bridge configs that move hosted Unity image
rollouts from the public `unity` repository into the private canonical build
path in `unity-deploy`.

## Current State

- `unity-staging` has already been cut over. The live Cloud Build trigger now
  runs the private bridge flow instead of building directly from
  `unity/deploy/cloudbuild-staging.yaml`.
- `unity-base-staging-private` exists as the private canonical staging base
  build trigger.
- `unity-production-private-bridge` and `unity-base-production-private` exist,
  but they are dormant/manual only. Production has not been cut over yet.
- The live production `unity` trigger still points at `deploy/cloudbuild.yaml`
  in the public `unity` repository.

## Important Invariant

Do not prune hosted deploy assets from `unity/main` until the live production
`unity` trigger has been switched to the private bridge path and validated.

Merging `staging -> main` without that trigger switch will probably not break
production immediately, because production can still build from the old public
path. The risk is more subtle:

- people may assume production is already using `unity-deploy` as the canonical
  hosted source of truth when it is not
- later hosted-only edits in `unity-deploy/main` may not actually control
  production yet
- a later cleanup of hosted files from `unity/main` could then break production
  builds unexpectedly

## Production Cutover Checklist

When you are ready to promote the hosted split to production:

1. Merge `unity/staging -> main`.
2. Merge `unity-deploy/staging -> main`.
3. Update the live `unity` production trigger to use the private bridge config
   instead of `deploy/cloudbuild.yaml` from the public repo.
4. Run one proof cycle on the current `unity/main` SHA.
5. Confirm the chain:
   - `unity` trigger runs the private bridge
   - `unity-base-production-private` runs with the exact `unity` SHA
   - downstream `unity-deploy` production build succeeds
   - idle container refresh completes
6. Only after that, prune hosted-only deploy assets from `unity/main`.

## Staging Reference

The staging cutover that has already been validated uses this shape:

- live trigger: `unity-staging`
- private base trigger: `unity-base-staging-private`
- downstream overlay trigger: `unity-deploy-staging`

The production cutover should mirror that same pattern:

- live trigger: `unity`
- private base trigger: `unity-base-production-private`
- downstream overlay trigger: `unity-deploy`
