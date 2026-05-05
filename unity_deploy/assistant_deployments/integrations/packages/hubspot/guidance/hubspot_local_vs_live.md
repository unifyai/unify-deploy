# Local DataManager Query vs. Live HubSpot Call

This package mirrors HubSpot CRM data into the assistant's DataManager via
the sync orchestrator.  For most reads, prefer the local copy.

## Decision rule

| Question | Use |
| --- | --- |
| "Show me Sarah Chen's contact info" | `query_local_contacts` first |
| "What's the current state of deal 9001 RIGHT NOW?" | `get_deal(mock=False)` - bypass local |
| "Has Sarah replied since I last checked?" | live - the local copy is at most 5 min stale |
| "Find all contacts at Acme" | `query_local_contacts(name_query='Acme')` |
| "Update Sarah's phone number" | live `update_contact` (writes always go live; mirror updates DataManager same-tick) |

## Freshness signal

Every `query_local_*` function returns a `freshness` block:

```json
{"last_synced_at": "2026-04-26T15:00:00Z", "is_fresh": true, "threshold_seconds": 3600}
```

If `is_fresh` is False, the local copy is older than the freshness
threshold.  Tell the user the data may be slightly stale, and offer to
refresh by calling the live function.

## When local query returns nothing

Two possibilities:

1. **The record genuinely doesn't exist** - confirm with a live
   `search_*` to be sure.  Possible if the customer just created it and the
   sync hasn't caught up.
2. **The sync hasn't run yet for this object type** - check
   `get_sync_state` to see when the last successful tick was.  If never,
   tell the user the integration is still bootstrapping and offer the
   live path.

## Writes are always live

`create_*`, `update_*`, `delete_*`, mutation engagements, all
`send_*`/`publish_*`/`enroll_*` operations bypass the local copy and go
directly to HubSpot.  When `HUBSPOT_MIRROR_MUTATIONS_TO_DATAMANAGER=true`
(default), successful mutations also write back to DataManager so the
local copy stays consistent without waiting for the next sync tick.
