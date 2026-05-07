# Local DataManager vs Live API

After `run_matterport_sync_tick` has materialised data into
`Matterport/...` contexts, prefer `query_local_*` over live calls
for analytics.  Live calls remain the right call for fresh single-record
fetches and post-write confirmations.

## Decision rule

```
if asking about many records or rolling up:
    use query_local_*
elif asking "right now" or "since last second":
    use live get_*/list_*
elif post-write confirmation:
    use live get_*
else:
    use query_local_* if its freshness block says is_fresh=true,
    fall through to live otherwise
```

## Freshness block

Every `query_local_*` function returns:

```python
{
  "rows": [...],
  "count": N,
  "freshness": {
    "last_synced_at": "2026-04-30T09:00:00Z",
    "age_seconds": 1830,
    "threshold_seconds": 7200,
    "is_fresh": True
  }
}
```

`is_fresh` is true when `age_seconds < threshold_seconds`.  Default
threshold is 2× the per-object sync interval, overridable via
`MATTERPORT_LOCAL_FRESHNESS_THRESHOLD_SECONDS`.

When `is_fresh=False`, decide whether the analytical use case can
tolerate the staleness.  If not, fall through to the live API.  Don't
silently return stale data without telling the user — say "as of
HH:MM" or similar.

## Two contexts to know

- `Matterport/Models` — the dimension table (one row per scanned
  property).
- `Matterport/ViewStats/Daily` — daily aggregated time series; unique
  per `(model_id, day)`.

## Cross-joins through DataManager

Both `query_local_matterport_models` and
`query_local_matterport_view_stats` accept `unit_id` to roll up via
`Matterport/Links/ModelUnit`.  This is how you answer "what's the
total view count for unit 4B" without ever calling the live API.

## When to skip the cache

- The user explicitly asks for live data ("right now", "current",
  "real-time").
- A model id was created in the last sync interval (the local copy
  doesn't know it yet).
- After a write — confirm by reading live, then mirror back.
