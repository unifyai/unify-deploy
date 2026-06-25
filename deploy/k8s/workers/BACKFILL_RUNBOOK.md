# Ingest pipeline — backfill mode & durability runbook

Operational guide for large one-off backfills (e.g. the client_beta v1 load) and for
the durability levers that keep the ingest pipeline self-healing. Prefer the
documented steps below over ad-hoc `kubectl patch` so the cluster always returns
to a known-good steady state.

## Steady-state configuration (committed in manifests)

| Setting | Production | Staging | Where |
| --- | --- | --- | --- |
| HPA `minReplicas`/`maxReplicas` | `1` / `20` | `1` / `15` | `ingest-worker-deployment*.yaml` |
| `UNITY_INGEST_ATTEMPT_LEASE_TTL` | `480`s | `480`s | `ingest-worker-deployment*.yaml` |
| `UNITY_INGEST_LEASE_MAX_LIFETIME_S` (code default) | `1800`s | `1800`s | `entrypoint_ingest._lease_lifetime_cap` |
| `UNITY_QUEUED_STALE_AGE_SECONDS` (code default) | `900`s | `900`s | `pipeline_observability.queued_stale_age_seconds` |

The code default for `UNITY_INGEST_ATTEMPT_LEASE_TTL` is `480` and matches the
manifests; the env var is the single source of truth in the cluster.

## Backfill mode (pin the fleet)

During a large backfill the HPA can scale the fleet up and down aggressively.
Scale-down terminates pods mid-chunk; Phase 1 releases the GCS attempt-lease on
SIGTERM so a survivor resumes immediately, but pinning replicas still gives
steadier throughput and avoids churn.

1. Pin the HPA to a fixed size (`N` = desired steady worker count, e.g. `8`):

   ```bash
   kubectl -n production patch hpa unity-ingest-worker-hpa \
     --type merge -p '{"spec":{"minReplicas":8,"maxReplicas":8}}'
   ```

2. (Optional) If chunks are large and lease churn is observed, raise the attempt
   lease TTL for the duration of the backfill (still well under Pub/Sub's 600s
   modify-ack-deadline cap):

   ```bash
   kubectl -n production set env deploy/unity-ingest-worker \
     UNITY_INGEST_ATTEMPT_LEASE_TTL=480
   ```

3. Monitor throughput from durable checkpoints (immune to worker log spam):

   ```bash
   uv run python -m unity_deploy.infra.cli.pipeline_control throughput \
     --dispatch-id <DISPATCH_ID> --interval 60
   ```

## Restore after the backfill drains

When the dispatch is complete (`throughput` reports "All jobs terminal", or
`verify` passes), restore autoscaling. **Production must return to `min 1 /
max 20`; staging to `min 1 / max 15`.**

```bash
# Production
kubectl -n production patch hpa unity-ingest-worker-hpa \
  --type merge -p '{"spec":{"minReplicas":1,"maxReplicas":20}}'

# Staging
kubectl -n staging patch hpa unity-ingest-worker-hpa \
  --type merge -p '{"spec":{"minReplicas":1,"maxReplicas":15}}'
```

Equivalent (and preferred for drift-free state) is re-applying the manifests:

```bash
kubectl apply -f deploy/k8s/workers/ingest-worker-deployment.yaml
kubectl apply -f deploy/k8s/workers/ingest-worker-deployment_staging.yaml
```

> Reminder: an ad-hoc pin (`min=max=8`) left in place after a backfill silently
> caps throughput for all subsequent ingests. Always restore.

## Verify completeness

After any backfill or recovery, confirm declared rows landed in durable
checkpoints:

```bash
uv run python -m unity_deploy.infra.cli.pipeline_control verify \
  --dispatch-id <DISPATCH_ID>
```

A non-zero exit means at least one table's checkpoint is short of its declared
`row_count` — investigate before declaring the backfill done.

## Self-healing (no manual intervention needed)

- **Orphaned attempt-lease** (pod killed mid-chunk): released on SIGTERM /
  lease-lifetime-cap surrender; the message nacks and a survivor resumes from
  the durable checkpoint (Phase 1).
- **running-stale / queued-stale "limbo"**: the `stale-reconciler` CronJob
  (`reconcile-stale --execute`, every 15m) republishes from the parse outbox /
  checkpoints. `queued-stale` only triggers after
  `UNITY_QUEUED_STALE_AGE_SECONDS` so a freshly-dispatched job whose message is
  still in flight is never prematurely recovered.
- **Duplicate live attempt**: deferred until just after the holder's lease is
  stealable (expiry + steal-grace + buffer), bounded by
  `UNITY_DUPLICATE_DEFER_MAX_ATTEMPTS`.

### Do not run `retry` and `recover-stale` concurrently

Both publish ingest messages. Running them at the same time on the same job
creates the duplicate-message lease/checkpoint race that can silently
under-ingest. The in-flight publish guard (`UNITY_INFLIGHT_GUARD_SECONDS`) skips
a second publish within the window unless `--force` is passed — do not override
it without confirming no message is in flight.
