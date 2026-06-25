# Linking Matterport Models to External Records

A Matterport model corresponds to a physical space — but Matterport's
`model_id` and `address` are independent of whatever the customer's
authoritative record system uses (a unit id in a property-management
system, a project code in a construction system, a SKU/location code in
retail, etc.).  The package stores the link in DataManager at
`Matterport/Links/ModelUnit` with columns
`(model_id, unit_id, source, confidence, linked_at)`.

`unit_id` here is generic — whatever join key the customer's record
system exposes.  v1 stores it as a **free string** so the link can be
populated now and tightened to a typed FK later when the upstream
system is integrated.

## Three population paths

In order of preference:

### 1. Internal-label convention (preferred — automatic)

If the Matterport model's `internal_label` matches the regex
`^unit:(?P<unit_id>[A-Za-z0-9-_]+)$`, sync auto-creates a link with
`source="internal_label"` and `confidence=1.0`.  Regex overridable via
`MATTERPORT_INTERNAL_LABEL_UNIT_REGEX`.

Customer instruction:

> Open the model in Matterport, click the gear icon -> Details, set
> "Internal Label" to `unit:<your-id>` (whichever record id this scan
> represents) and save.

### 2. Manual via chat

The user tells the assistant: "Link Matterport model `mdl-...` to record
`<unit-id>`."  The assistant calls
`link_matterport_model_to_unit(model_id, unit_id, source="manual")`.

### 3. Address heuristic (deferred)

When the upstream record system is integrated, sync can compare each
Matterport model's address to the upstream `address` field, score the
match, and auto-link above a high threshold.  Below the threshold,
surface the candidate to the user for confirmation.  The function
signature accepts `source="address_match"` and `confidence` already.

## Looking up the link

`lookup_matterport_model_for_unit(unit_id)` returns the linked model(s).
A single record can have multiple linked models (e.g. before/after
renovation scans) — the function returns all of them so the assistant
can pick the most recent.

## When the link is missing

If asked "show me the 3D tour for record X" and no link exists:

1. Try `query_local_matterport_models(unit_id="X")` — returns nothing.
2. Fall back to a fuzzy address lookup via `matterport_graphql_query` —
   query `models(filter: { address: $query })` (verify the field name
   against the live schema first; Matterport's filter args change) and
   surface any high-score match: "Looks like model Y.  Link them?" and
   on confirmation call `link_matterport_model_to_unit(...)`.
3. If no good match, tell the user to either set the Matterport
   internal label to `unit:X` or paste the model id directly.
