# Embed URL vs Analytics

Two distinct things the assistant might be asked for.  Pick the right
one based on intent.

## Embed URL

Use `generate_matterport_embed_url(model_id, options=...)` when the
user wants to:

- See the tour themselves
- Share a tour link with a prospect or colleague
- Embed an iframe in a portal or document

The function constructs a Showcase URL with sensible defaults
(`qs=1` quickstart, `play=1` auto-rotate, `brand=0` hide branding).
No API call — pure URL assembly.  Works without API credentials.

`options` overrides the defaults.  Common keys (see Matterport's
Showcase URL Parameters reference for the complete list):

| Key | Purpose |
|---|---|
| `qs` | Quickstart (skip splash) |
| `play` | Auto-rotate on load |
| `brand` | Show/hide Matterport branding |
| `mt` | Mattertags toggle |
| `dh` | Display hover hotspots |
| `hr` | High-res rendering |
| `help` | Show in-tour help |

## Analytics

Use the view-stats functions when the user wants to know:

- How many people viewed a listing
- Who specifically viewed it (lead correlation)
- Trend over time
- Top referring channels

Default to `query_local_matterport_view_stats` for any question that
spans multiple records or rolls up to a unit/address — local
DataManager is faster and cheaper.  Use the live functions when
freshness matters or for a specific session lookup.

## Don't conflate

If someone says "show me the Westwood listing tour," they want the
embed URL — not the engagement stats.  If they say "how is the
Westwood listing performing," they want the stats.  Ask for
clarification only when the intent is genuinely ambiguous.
