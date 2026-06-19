# Local DataManager Query vs. Live HubSpot Call

For most reads, prefer `query_local_*` over the live API.  Reach for live
when freshness matters or when the local copy hasn't synced yet.

| Question | Use |
| --- | --- |
| "Show me Sarah Chen's contact info" | `query_local_contacts` first |
| "What's the current state of deal 9001 RIGHT NOW?" | `get_deal(mock=False)` — bypass local |
| "Has Sarah replied since I last checked?" | live — local copy is up to ~5 min stale |
| "Find all contacts at Acme" | `query_local_contacts(name_query='Acme')` |
| "Update Sarah's phone number" | live `update_contact` (writes always go live) |

`query_local_*` returns a `freshness` block; if `is_fresh=False`, tell the
user the data may be stale and offer to refresh via the live function.

If a local query returns nothing, check `get_sync_state` — the sync may
not have run for that object type yet.
