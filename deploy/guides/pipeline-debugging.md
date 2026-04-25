# Pipeline Debugging Runbook

Use this when parse or ingest appears stuck, Pub/Sub numbers do not match
local logs, or a worker pod is sitting in `ContainerStatusUnknown`.

## Backlog Metrics

Prefer absolute Cloud Monitoring metrics over HPA output:

```bash
gcloud pubsub subscriptions update unity-ingest-sub-staging --ack-deadline=120
gcloud pubsub subscriptions update unity-parse-sub-staging --ack-deadline=120
```

The staging subscriptions should use a 120s initial ack deadline. Long
messages are protected by the worker `LeaseController`, which extends by
300s every 120s. A dead pod should therefore sit in limbo for about two
minutes, not ten.

`deploy/scripts/dev/run_pipeline.sh` now writes absolute values to
`pubsub-backlog.log`:

```text
12:00:00  parse: undeliv=0 oldest=0s replicas=1/1  |  ingest: undeliv=25 oldest=6589s replicas=15/15
```

Older logs may show values such as `1667m`. That is the HPA external
metric `averageValue`, not total backlog: `1667m` means roughly 1.667
messages per replica. Multiply by current replicas to estimate the real
queue depth.

## Log Replay Duplicates

Old monitor runs used `kubectl logs -f --tail=50` in a reconnect loop.
Each reconnect replayed the last 50 lines, so `Completed job=` counts can
be inflated. Count unique jobs, not raw lines:

```bash
deploy/scripts/dev/analyze_ingest_log.sh logs/pipeline/<run-dir>
```

The analyzer reports raw completion lines, unique completed jobs, per-job
replay multiplicity, pods that started ingest, and pods that never emitted
`Completed`.

## ContainerStatusUnknown

For these workers, `ContainerStatusUnknown` should be treated as a pod
death first and an application error second. In the observed incident the
root cause was ephemeral storage eviction: live pods showed a 2Gi effective
limit even though the manifest asked for a 4Gi limit. GKE Autopilot lowered
the limit because the request was still 2Gi.

Verify both request and limit after rollout:

```bash
kubectl get deployment unity-ingest-worker -n staging -o yaml \
  | rg 'autopilot.gke.io/resource-adjustment|ephemeral-storage' -C 2
```

The effective pod spec must show:

```yaml
requests:
  ephemeral-storage: 4Gi
limits:
  ephemeral-storage: 4Gi
```

The failed-pod GC CronJob removes stale `Failed` and `Unknown` pipeline
worker pods every 10 minutes:

```bash
kubectl apply -f deploy/k8s/workers/failed-pod-gc-cronjob.yaml
```

## DLQ Triage

`run_pipeline.sh` writes `dlq.log` every 60s. Any growth during a run is
actionable.

Inspect without acking:

```bash
deploy/scripts/dev/drain_dlq.sh --limit 100
```

Drain after confirming messages are stale or already re-dispatched:

```bash
deploy/scripts/dev/drain_dlq.sh --ack --limit 100
```

Decoded payloads are written to `logs/dlq/<timestamp>.jsonl`. Attribute
`dispatch_id` identifies which dispatch produced the message.

## Large CSV Notes

Excel displays at most 1,048,576 rows in a sheet. A CSV that appears to
stop at 1.048M rows in Excel can still contain more rows. Trust parser
manifests and ingest checkpoints for real row counts.

`chunk_size` is configured as 1000 rows for the Client Alpha deployment.
If a run appears to ingest at only a few rows per second, inspect the new
`[ingest][progress]` and `[ingest][dm]` logs to separate checkpoint write
time, resume skip overhead, and DataManager insert throughput.

## Stuck Ingest Checklist

1. Check absolute backlog and oldest age in `pubsub-backlog.log`.
2. Confirm lease logs show regular `Lease extended` lines every ~120s.
3. Run `analyze_ingest_log.sh` to detect replayed completions and pod deaths.
4. Inspect `pods.log` for evictions or `ContainerStatusUnknown`.
5. Inspect `dlq.log`; if it grew, decode with `drain_dlq.sh` in dry-run mode.
6. Do not roll worker deployments while million-row ingests are in flight.
