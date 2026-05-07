# Linking Matterport Models to RealPage Units

A Matterport model corresponds to a physical unit, but Matterport's
`address` and RealPage's `unit_id` are independent identifiers.  The
package stores the link in DataManager at
`Matterport/Links/ModelUnit` with columns
`(model_id, unit_id, source, confidence, linked_at)`.

## v1 caveat

The RealPage table doesn't exist yet — RealPage is on a separate
RPX-cert track.  v1 stores `unit_id` as a **free string** so the link
can be populated now and tightened to a typed FK later.  When RealPage
lands, a one-shot backfill resolves the free strings against the live
`units` table and quarantines any unresolved rows.

## Three population paths

In order of preference:

### 1. Internal-label convention (preferred — automatic)

If the Matterport model's `internal_label` matches the regex
`^unit:(?P<unit_id>[A-Za-z0-9-_]+)$`, sync auto-creates a link with
`source="internal_label"` and `confidence=1.0`.

The regex is overridable via `MATTERPORT_INTERNAL_LABEL_UNIT_REGEX`.

Tell the customer to set the internal label inside Matterport when they
scan a unit:

> Open the model in Matterport, click the gear icon → Details, set
> "Internal Label" to `unit:4B` (or whichever RealPage unit id this
> tour represents), and save.

### 2. Manual via chat

The user tells the assistant: "Link Matterport model `mdl-...` to unit
`4B`."  The assistant calls
`link_matterport_model_to_unit(model_id, unit_id, source="manual")`.

### 3. Address heuristic (deferred)

When the RealPage scenario lands, sync can compare each Matterport
model's address to the RealPage `units.address`, score the match, and
auto-link above a high threshold.  Below the threshold, surface the
candidate to the user for confirmation.  v1 stubs this out — the
function signature accepts `source="address_match"` and `confidence`
already.

## Looking up the link

`lookup_matterport_model_for_unit(unit_id)` returns the linked
model(s).  A unit can have multiple linked models (e.g. before/after
renovation scans) — the function returns all of them so the assistant
can pick the most recent.

## When the link is missing

If the assistant is asked "show me the 3D tour for unit 4B" and no
link exists:

1. Try `query_local_matterport_models(unit_id="4B")` — returns nothing.
2. Try `search_matterport_models_by_address(<unit's address>)` — if a
   high-score match comes back, surface it to the user with "Looks
   like model X.  Link them?" and on confirmation call
   `link_matterport_model_to_unit(...)`.
3. If no good match, tell the user to either set the Matterport
   internal label to `unit:4B` or paste the model id directly.
