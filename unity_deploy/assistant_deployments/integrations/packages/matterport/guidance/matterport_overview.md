# Matterport Integration — Overview

3D virtual-tour analytics connector for Matterport-scanned listings.
The assistant uses this when:

- Listing or fetching 3D models for the customer's properties.
- Reading per-model engagement (view counts, unique visitors,
  time-on-tour, top referrers).
- Generating a Showcase URL the user can embed or share.
- Linking a model to a RealPage unit, or correlating tour views to
  HubSpot leads.

## Authentication

HTTP Basic with an API token pair.  The customer generates an API Token
at Matterport account Settings → Account → API Access and pastes the
**Token ID** and **Token Secret** into Console → Integrations →
Matterport.  No OAuth callback — paste-and-go.

If either secret is unset, all live functions return a structured
"not connected" envelope listing what's missing.

## Two read paths

| Read path | When to use |
|---|---|
| `query_local_*` (DataManager) | Analytics, cross-record joins, rollups across many models |
| `get_*` / `list_*` / `search_*` (live API) | Fresh single-record fetches, "right now" queries, post-write confirmations |

See `matterport_local_vs_live.md` for the decision rule.

## Embed vs analytics

If the question is "show me the tour," use `generate_matterport_embed_url`.
If the question is "how engaged are leads," use the view-stat functions.
Default Showcase URLs are public/unlisted — no signed-token API needed
for either share or embed.  See `matterport_embed_vs_analytics.md`.

## Sync mechanics

A scenario per client (`matterport_listing_analytics_v0.yaml`) drives
`run_matterport_sync_tick` on a 60-second scheduler tick.  The
orchestrator gates per-object cadence by env var so freshness is tunable
without redeploying the YAML.  Models drift slowly (daily); view stats
need to be hourly to be useful for sales hand-offs.  See
`matterport_sync_runbook.md`.

## Tier gating

Sandbox tokens only see Matterport's demo models.  Production access
against a customer's real models requires the **Developer Tools add-on**
on their Matterport plan.  Tier-gated capabilities return graceful 403
envelopes.  See `matterport_tier_gating.md`.
