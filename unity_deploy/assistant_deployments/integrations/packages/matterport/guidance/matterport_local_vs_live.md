# Local DataManager vs Live API

After `run_matterport_sync_tick` has materialised data into
`Matterport/...` contexts, prefer `query_local_*` for analytics.  Live
calls are right for fresh single-record fetches and post-write
confirmations.

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

`query_local_*` returns a `freshness` block; default threshold is 2× the
per-object sync interval, overridable via
`MATTERPORT_LOCAL_FRESHNESS_THRESHOLD_SECONDS`.  When `is_fresh=False`,
say "as of HH:MM" or fall through to live — don't silently return stale
data.

## Two contexts to know

- `Matterport/Models` — dimension table, one row per scanned space.
- `Matterport/ViewStats/Daily` — daily aggregated time series, unique
  per `(model_id, day)`.

## Cross-joins through DataManager

Both `query_local_matterport_models` and
`query_local_matterport_view_stats` accept a join key (e.g. `unit_id`)
to roll up via `Matterport/Links/ModelUnit`.  See
`matterport_unit_linking.md`.

## When to skip the cache

- The user explicitly asks for live data.
- A model id was created in the last sync interval (local copy doesn't
  know it yet).
- After a write — confirm by reading live.
