# Matterport Tier Gating

Some Matterport surfaces are only accessible on paid plans with the
Developer Tools add-on.  The package detects this gracefully — 403
responses come back as structured envelopes rather than exceptions.

## Two tiers that matter

| Tier | What it sees |
|---|---|
| **Sandbox** (free) | Matterport's demo models only — not the customer's real tours.  All read-only. |
| **Production** (paid plan + Developer Tools add-on) | The customer's own models with full analytics. |

A customer who pastes credentials from a free Matterport account is in
sandbox tier; the package will appear to work but every list will
return Matterport's demo data, not theirs.

## Detection

`probe_matterport_tier(force=True, mock=False)` runs a cheap GraphQL
sweep across the package's main capabilities (`models`, `view_stats`,
`mosaics`, `tags`) and returns a per-capability availability map.
Cached for 24 h in `Matterport/Meta/Capabilities`.

Result shape:

```python
{
  "probed_at": "...",
  "ttl_seconds": 86400,
  "capabilities": {
    "models": {"available": True, "status_code": 200},
    "view_stats": {"available": False, "status_code": 403,
                    "hint": "..."},
    ...
  }
}
```

## Handling 403 in functions

Tier-gated functions return:

```python
{
  "error": "Matterport ... returned 403",
  "status_code": 403,
  "hint": "...customer message about plan upgrade..."
}
```

The assistant should relay the hint to the user, not retry.  If a
sync function returns a tier-gated 403, the orchestrator records it as
`skipped` (not `errored`) so subsequent ticks keep working for the
unaffected objects.

## Customer-facing messages

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
