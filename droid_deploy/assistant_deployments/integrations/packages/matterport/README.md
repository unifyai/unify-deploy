# Matterport Integration

Generic, reusable Matterport 3D virtual-tour analytics connector. Lives
under `integrations/packages/` so any client can opt in via
`integrations: ["matterport"]` on their deployment spec.

## What this package provides

* **On-demand functions** — get/list/search across models, mosaics,
  Mattertags, and view stats. Each function defaults to `mock=True` for
  safe testing; pass `mock=False` to hit live Matterport.
* **Sync functions** — pull deltas into the assistant's DataManager.
  Returns the canonical `{schema_version, tables, metadata}` envelope.
* **Local-query functions** — read the synced DataManager copy with a
  freshness signal. Preferred over live calls when freshness allows.
* **Embed-URL builder** — `generate_matterport_embed_url` constructs
  Showcase URLs for embedding tours in portals (no API call needed).
* **Cross-joins** — link Matterport models to RealPage units and to
  HubSpot leads (via `utm_email` query-string instrumentation).
* **Sync orchestrator** — `run_matterport_sync_tick` aggregates enabled
  object types into one envelope and gates per-object cadence by config.
* **Tier detection** — `probe_matterport_tier` records which Matterport
  endpoints the active credentials cover. Tier-gated functions return
  graceful 403 envelopes rather than raising.
* **Guidance** — markdown files cover every domain plus cross-cutting
  concerns (tier gating, local vs live, sync runbook, lead correlation).

## Configuration

The customer generates an API Token at Matterport account Settings →
Account → API Access, then pastes the Token ID and Token Secret into the
assistant's Secret Manager as `MATTERPORT_TOKEN_ID` and
`MATTERPORT_TOKEN_SECRET`. No OAuth callback — paste-and-go.

`MATTERPORT_ORG_ID` is an optional pin when the credentials see more
than one Matterport organisation. `MATTERPORT_BASE_URL` is an optional
host override (defaults to `https://api.matterport.com`).

| Env var | Default | Purpose |
| --- | --- | --- |
| `MATTERPORT_SYNC_MIN_INTERVAL_SECONDS` | `300` | Floor between sync ticks |
| `MATTERPORT_SYNC_OBJECT_INTERVALS` | (per-object defaults) | Per-object cadence overrides (`models:86400,view_stats:3600`) |
| `MATTERPORT_SYNC_OBJECTS` | `models,view_stats` | Which object types to sync |
| `MATTERPORT_VIEW_STATS_LOOKBACK_DAYS` | `7` | Window for view-stat sync |
| `MATTERPORT_SYNC_VIEW_EVENTS` | `false` | Opt-in granular view-event sync (high volume) |
| `MATTERPORT_API_PAGE_SIZE` | `25` | Page size for list queries |
| `MATTERPORT_REQUEST_TIMEOUT_SECONDS` | `30` | Per-request HTTP timeout |
| `MATTERPORT_RATE_LIMIT_MAX_RETRIES` | `3` | 429 retry attempts |
| `MATTERPORT_RATE_LIMIT_BACKOFF_FACTOR` | `1.5` | Exponential backoff base |
| `MATTERPORT_TIER_PROBE_TTL_SECONDS` | `86400` | Cache TTL for `probe_matterport_tier` |
| `MATTERPORT_LOCAL_FRESHNESS_THRESHOLD_SECONDS` | (per-object 2× interval) | When to flag a local query as stale |
| `MATTERPORT_CONFIG_JSON` | unset | One-shot JSON override for any of the above |

## Adopting the package

In a client's deployment:

```python
return BASE_SPEC.derive(
    name="default",
    integrations=["matterport"],
)
```

To also schedule the incremental sync, add a per-client sync package
that defines the scenario YAML with the client's assistant id pinned in
`tasks[*].target.assistant_id`, or activate the bundled
`matterport_listing_analytics_v0` template via `ScenarioActivation`.

## Tier limitations

Matterport's free sandbox tier returns only Matterport's demo models —
not the customer's actual tours. Production API access against a
customer's models requires the **Developer Tools add-on** on their
Matterport plan. `probe_matterport_tier(force=True)` records which
endpoints the active credentials cover; tier-gated capabilities return
graceful 403 envelopes the assistant relays to the user.

## Cross-join setup

The HubSpot lead-correlation join requires the customer to instrument
their HubSpot landing pages with a `utm_email` query parameter on the
Showcase URL — for example `?utm_email={{ contact.email }}`. Without it
the join is a no-op. See the docstring on
`correlate_matterport_views_to_hubspot_leads` in `functions/linking.py`.

The RealPage unit-linking join works in three modes: an internal-label
convention on the Matterport model (preferred), manual link via chat,
and an address heuristic. See `guidance/matterport_unit_linking.md`.
