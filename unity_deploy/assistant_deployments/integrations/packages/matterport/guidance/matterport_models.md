# Matterport Models

The `model` is the canonical entity — one per scanned property.  Each
model has:

- `model_id` (opaque short string, immutable)
- `name` (free-text label set by the property manager)
- `internal_label` (free-text — preferred convention is `unit:<unit_id>`
  to make RealPage cross-linking automatic; see `matterport_unit_linking.md`)
- `address` (denormalised: line 1/2, city, state, postal_code, country)
- `share_url` (public/unlisted Showcase URL)
- `visibility` (`public` / `unlisted` / `private`)
- `status` (`processing` / `processed` / `archived`)
- `sqft` (when set in Matterport)
- `last_modified` (ISO timestamp)

## Reading models

| Goal | Function |
|---|---|
| Browse all models | `list_matterport_models(after, limit)` (cursor-paginated) |
| Get one model by id | `get_matterport_model(model_id)` |
| Find a model for an address | `search_matterport_models_by_address(address_query)` |
| Local query | `query_local_matterport_models(...)` (preferred for analytics) |

## Search semantics

`search_matterport_models_by_address` is a fuzzy lookup that paginates
and filters client-side when the GraphQL filter doesn't match exactly.
Returns a `match_score` 0.0-1.0 — exact substring matches score 1.0,
token-overlap matches score 0.5.  Treat anything below 0.5 as a
candidate to confirm with the user, not auto-link.

## Mosaics and Mattertags

- Mosaics are multi-model collections (e.g. an entire building's units
  grouped).  Read via `list_matterport_mosaics`.
- Mattertags are pin-style annotations placed inside the 3D space.
  Read via `list_matterport_tags(model_id)`.  Informational only —
  not synced into DataManager.

## Visibility and sharing

A `share_url` works for `public` and `unlisted` models without any
signed token.  `private` models require a signed-token API that
Matterport gates behind partner-tier — out of scope for v1.  If the
assistant needs to share a private tour, redirect the user to share via
Matterport's UI directly.
