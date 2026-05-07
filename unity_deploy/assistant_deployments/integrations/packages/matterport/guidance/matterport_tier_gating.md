# Matterport Tier Gating

Some Matterport surfaces are only accessible on paid plans with the
Developer Tools add-on.  403 responses come back as structured
envelopes (never raised).

## Two tiers that matter

| Tier | What it sees |
|---|---|
| **Sandbox** (free) | Matterport's demo models only — not the customer's real tours.  All read-only. |
| **Production** (paid plan + Developer Tools add-on) | The customer's own models with full analytics. |

A customer who pastes credentials from a free Matterport account is in
sandbox tier; the package will appear to work but every list returns
demo data, not theirs.

## Detection

`probe_matterport_tier(force=True, mock=False)` runs a cheap GraphQL
sweep across the package's main capabilities (`models`, `view_stats`,
`mosaics`, `tags`) and returns a per-capability availability map.
Cached for 24h in `Matterport/Meta/Capabilities`.

## Customer-facing message when gated

If `probe_matterport_tier` shows view_stats as gated:

> Your Matterport plan doesn't include the Developer Tools add-on
> needed for analytics.  You can still browse 3D tours in the
> assistant, but per-tour view counts are unavailable.  Contact
> Matterport to enable Developer Tools, then run
> `probe_matterport_tier(force=True)` to refresh.

## Free annual renewal

At time of writing, Matterport offers a free annual renewal of
Developer Tools licenses for a limited window.  Worth re-checking at
contract time — pricing may shift.
