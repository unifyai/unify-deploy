# Matterport Sync Runbook

`run_matterport_sync_tick` is the entrypoint the scenario runtime calls
on its interval.  It dispatches per-object sync functions, gates by
per-object cadence, aggregates returned tables, and emits an audit row.

## Cadence

- **Scheduler tick**: 60s (set in scenario YAML).
- **Per-object minimum gap**: `MATTERPORT_SYNC_OBJECT_INTERVALS`
  (default `models:86400,view_stats:3600`).
- **Floor across all objects**: `MATTERPORT_SYNC_MIN_INTERVAL_SECONDS`
  (default 300).

So the scheduler can fire every minute, but each object only syncs when
its per-object gap has elapsed.

## Object selection

`MATTERPORT_SYNC_OBJECTS` (default `models,view_stats`).  Drop one to
disable.  Add `view_events` to enable granular per-session events
(high volume — only when the lead-correlation join is in scope).

## Idempotency

| Table | Unique key |
|---|---|
| `models` | `model_id` |
| `model_unit_links` | `[model_id, unit_id]` |
| `view_stats_daily` | `[model_id, day]` |
| `view_events` | `event_id` |
| `sync_state` | `object_type` |

Watermarks are per object_type so partial failures emit
`metadata.partial=True` and the next tick re-runs the failed slice.
View stats are bucketed daily so re-pulling overlapping windows is safe.

## Operations

`get_matterport_sync_state` returns current per-object watermarks plus
the most recent `sync_runs` row.  `probe_matterport_tier(force=True)`
re-runs the capability sweep.

`run_matterport_sync_tick(full=true, mock=false)` ignores watermarks and
pulls everything — use after first connect, after a long outage, or
after changing `MATTERPORT_SYNC_OBJECTS`.

Per the scenario convention, ships with `tasks[*].enabled: false`.
Operators flip to `true` after the control-plane has TaskScheduler +
FunctionManager ids seeded for the client.

## Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `view_stats` rows empty for some models | Plan tier doesn't include analytics | `probe_matterport_tier` — if 403 on view_stats, document as known-gated |
| `models` returns 0 rows in production | Sandbox token in use | Re-issue token after upgrading to paid plan with Developer Tools |
| Sync runs every tick but no data changes | Cadence working as designed | Inspect `MATTERPORT_SYNC_OBJECT_INTERVALS` |
| `429` errors | Sync too aggressive | Increase `MATTERPORT_RATE_LIMIT_BACKOFF_FACTOR` or raise per-object intervals |
