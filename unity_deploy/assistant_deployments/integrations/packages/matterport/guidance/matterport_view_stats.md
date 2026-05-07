# Matterport View Statistics

Per-model engagement: how many people viewed a tour, who they were
(via referrer / `utm_email`), and how long they stayed.

## What's available

| Function | Returns | Best for |
|---|---|---|
| `get_matterport_view_stats(model_id, since, until, granularity)` | Aggregate totals + daily breakdown | "How popular is this listing?" |
| `list_matterport_view_events(model_id, since, until)` | Per-session events with referrer + `utm_email` | Lead correlation against HubSpot |
| `query_local_matterport_view_stats(...)` | Local DataManager copy | Analytics, rollups across multiple models or rolled up to a unit |

## Lookback window

Sync defaults to a 7-day rolling window (`MATTERPORT_VIEW_STATS_LOOKBACK_DAYS`).
Daily granularity in DataManager — `view_stats_daily` is unique-keyed by
`(model_id, day)` so re-pulling overlapping windows is safe.

For longer history, raise the lookback or query Matterport live for the
specific window.

## Distinguishing the metrics

- `total_views` — count of view sessions (one user can produce many).
- `unique_visitors` — Matterport's de-dup of the above.
- `avg_seconds_on_tour` — how engaged sessions are; a useful proxy for
  listing quality vs. churn signal.
- `top_referrers` — which marketing channel drove the views (HubSpot
  landing page, Zillow listing, direct, etc.).

## When to use which

- **Aggregate question** ("how many people saw the Westwood listing
  this month?") → `get_matterport_view_stats` with a wide window, or
  the local equivalent if data is fresh enough.
- **Per-session question** ("who specifically viewed the listing on
  the morning of the 29th?") → `list_matterport_view_events` then
  `correlate_matterport_views_to_hubspot_leads`.
- **Trend question** ("is engagement growing on this property?") →
  `query_local_matterport_view_stats` with `since` set, then chart the
  daily breakdown.

## Granular events are opt-in

`view_events` is high-volume.  Sync is gated by
`MATTERPORT_SYNC_VIEW_EVENTS=true` (default false).  Turn this on
*only* when the lead-correlation cross-join is in scope for the
client — it's the only thing that reads from `view_events` in v1.
