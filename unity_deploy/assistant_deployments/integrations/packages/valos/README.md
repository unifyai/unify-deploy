# Valos — UK Property Data

UK property valuation data layer. Replaces the original placeholder
pass-through to `api.valos.ai` with a composition of paid + free
upstreams:

- **Ordnance Survey Data Hub** — OS Maps API (raster WMTS tiles,
  required scope) plus optional OS Names / OS Places (Premium add-ons
  for building-level geocoding with UPRN). Single key
  (`OS_MAPS_API_KEY`).
- **PropertyData** — `/api/freeholds` (postcode → registered titles +
  INSPIRE polygons), `/api/title-information` (full boundary GeoJSON,
  ownership, tenure). Single key (`PROPERTYDATA_API_KEY`).
- **postcodes.io** — UK postcode → WGS84 + LSOA/MSOA/local authority.
  Unauthenticated. Used as the primary path for `valos_geocode`
  postcode queries, the LSOA/MSOA bridge for
  `valos_postcode_demographics`, and the bulk geocoder +
  reverse-geocoder behind `valos_find_care_homes`.
- **Nominatim (OpenStreetMap)** — free-text address → WGS84.
  Unauthenticated, ≤1 req/s by upstream policy. Used as the
  free-tier fallback for `valos_geocode` when the OS plan does not
  cover OS Names / OS Places.
- **CQC Syndication** — care-home name/location register (coordinates,
  rating, bed count). Keyless (a `partnerCode` query param unlocks the
  2000 req/min tier). Backs `valos_find_care_homes`, which resolves
  competitor care homes authoritatively — market lists such as
  Carterwood name competitors but carry no address, and a bare name is
  unsafe to geocode through a general geocoder. Open Government Licence
  v3.0 (attribution required on deliverables).

`valos_geocode` composes a four-step fallback chain
(`postcodes.io → OS Places → OS Names → Nominatim`) so a Standard-tier
OS Data Hub project that only has OS Maps still resolves both
postcodes and free-text addresses without any extra wiring.
Demographics enrichment also uses the ONS open APIs — no key required.

> **Two activation paths.** The package is opt-in either way:
>
> 1. **Deploy-time wiring** — add `"valos"` to a deployment's
>    `integrations=[...]` argument on `BASE_SPEC.derive(...)`. Both
>    keys are seeded into the assistant's `/Secrets` from the
>    deployment env, typically using Unify-owned credentials shared
>    across all assistants in that deployment.
> 2. **Per-assistant Console paste** — leave the deployment alone and
>    paste both keys into the assistant's Integrations tab via the
>    Console card (`INTEGRATION_PROVIDERS["valos"]`). Useful for
>    one-off testing on a single assistant, or for clients who hold
>    their own OS Data Hub / PropertyData subscriptions. Requires an
>    assistant-session restart for the runtime
>    `register_available_integrations` pass to pick up the new keys
>    and register the package's tools.
>
> Either way, the secret names (`OS_MAPS_API_KEY`,
> `PROPERTYDATA_API_KEY`) are the only contract between Console and
> this manifest — keep them in sync if either side renames.

## Surface

Eight typed primitives the actor can call directly:

| Function | Purpose |
| --- | --- |
| `valos_geocode(query, max_results)` | Address/postcode → coordinates (+ UPRN where the OS plan supports it). Four-step chain: postcodes.io → OS Places → OS Names → Nominatim. |
| `valos_lookup_freeholds(postcode \| lat+lon)` | Postcode/coords → freehold title numbers + INSPIRE ids + tenure. |
| `valos_get_title_polygon(title_number \| inspire_id)` | Title id → GeoJSON boundary + plot size + ownership. |
| `valos_postcode_demographics(postcode, radius_km)` | Postcode → LSOA/MSOA + local authority + (later) census/IMD. |
| `valos_find_care_homes(lat, lon, radius_km, names)` | CQC care homes inside the catchment → authoritative coords + rating + beds; optional fuzzy-match of a competitor-name list. |
| `valos_render_os_map(geometry, output_path, ...)` | OS Maps WMTS layer → PNG, with optional polygon overlay. |
| `valos_render_location_plan(polygon, output_path, ...)` | Land-Registry-style plan with thick boundary + north arrow + scale bar. |
| `valos_render_competitor_map(centre, radius_km, pins, output_path, ...)` | Outdoor map + numbered competitor pins. |
| `valos_render_catchment_map(centre, radius_km, output_path, ...)` | Outdoor map + a single labelled catchment circle + subject marker. |

All renderers persist PNGs at the supplied filesystem path so
`ImageManager` resolves them later via `filter_images(filter="filepath
== '...'")` — same cross-manager image convention used throughout the
assistant.

## Authentication

Two required secrets, populated by whichever activation path is in use
(see the callout above):

- `OS_MAPS_API_KEY` — OS Data Hub project key. **Required scope is
  just OS Maps** (raster tiles for the rendering primitives). OS
  Names and OS Places are optional Premium add-ons; when they're
  not on the project, `valos_geocode` transparently routes around
  them (postcodes.io for postcodes, Nominatim for free-text
  addresses). The simple `?key=...` query-param flow is used
  everywhere; the OS "Project API Secret" (OAuth client-credentials
  path) is unused by this package.
- `PROPERTYDATA_API_KEY` — PropertyData subscription with the Land
  Registry endpoints enabled (`/api/freeholds`,
  `/api/title-information`).

Both keys read from `os.environ` at call time, so the runtime is
indifferent to which activation path seeded them.

### Geocoder selection

`valos_geocode`'s four-step chain is automatic and self-healing:

1. **postcodes.io** — taken when the input is a bare UK postcode.
2. **OS Places** — tried for free-text addresses. Returns 401
   `Invalid ApiKey for given resource` on Standard plans; the chain
   detects this and falls through.
3. **OS Names** — gazetteer fallback after OS Places. Same Premium
   gating; falls through on Standard.
4. **Nominatim (OpenStreetMap)** — final unauthenticated fallback for
   free-text addresses.

Set `VALOS_GEOCODE_SKIP_OS=true` to skip steps 2 and 3 entirely —
useful when you know the OS key has no Names/Places access and want
to save the two metered failed calls per address-level lookup.

### Competitor resolution

Do **not** geocode competitor care-home *names* through `valos_geocode`
— market lists (e.g. Carterwood) carry the name only, and a bare name
either fails to resolve or matches an unrelated building of the same
name elsewhere in the country. Use `valos_find_care_homes(lat, lon,
radius_km, names=[...])` instead: it lists the CQC care homes that
actually sit inside the catchment and matches the names against only
that radius-bounded set, so a wrong-entity match is geometrically
impossible. It also returns each home's CQC rating and bed count, which
populate the rating column and cross-check a market-data bed list.
Match threshold is tunable via `VALOS_CQC_MATCH_THRESHOLD` (default
0.6).

### Licensing

- **OS Maps API**: distributing OS rasters in customer-deliverable Word
  reports is **redistribution** and may require a separate OS Partner /
  OS Reseller agreement on top of the Premium plan. Confirm commercial
  terms with Ordnance Survey before producing client-deliverable maps.
- **PropertyData**: ToS includes reseller / multi-tenant clauses.
  Confirm reseller / multi-tenant terms with PropertyData before
  opting more than one client deployment into this package.
- **HM Land Registry Polygon Plus** (£1.50/polygon): not used by this
  package directly. Reserved for legal-grade title plans on a follow-on
  pass; INSPIRE polygons returned by PropertyData are *indicative
  extents*, not legal certainty.
- **CQC Syndication**: Open Government Licence v3.0. Deliverables that
  surface CQC data must carry the attribution "Contains public sector
  information licensed under the Open Government Licence v3.0" — the
  `valos_find_care_homes` payload returns this string in its
  `attribution` field.

## Metering and caps

`_metering.py` keeps per-day, per-process call counters across five
upstream channels:

| Provider | Default cap (env override) |
| --- | --- |
| `os_tiles` | 5,000/day (`VALOS_OS_TILES_DAILY_LIMIT`) |
| `os_names_places` | 1,000/day (`VALOS_OS_NAMES_PLACES_DAILY_LIMIT`) |
| `propertydata` | 500/day (`VALOS_PROPERTYDATA_DAILY_LIMIT`) |
| `nominatim` | 2,000/day (`VALOS_NOMINATIM_DAILY_LIMIT`) |
| `cqc` | 10,000/day (`VALOS_CQC_DAILY_LIMIT`) |

postcodes.io is intentionally unmetered — it's a UK-government
open-data service with no published rate limit and serves the
postcode-centroid path plus the bulk geocode / reverse-geocode behind
`valos_find_care_homes`. Nominatim's ceiling is a politeness signal to
the OSM operations team, not an upstream-billed quota. CQC's ceiling is
generous because a single catchment resolution fans out across
authority listings + per-location detail calls; it caps a runaway loop,
not normal usage.

When a counter trips its ceiling, the upstream client returns a
structured "quota exceeded" envelope and skips the HTTP call. The cap
exists to stop a runaway loop, not to police normal usage. Counters
reset at the next call after midnight UTC. Cross-process aggregation
(e.g. when more than one client deployment opts in) is a follow-on.

## Conventions

- 429 honours `Retry-After` with exponential backoff
  (`OS_RATE_LIMIT_MAX_RETRIES` / `OS_RATE_LIMIT_BACKOFF_FACTOR`,
  `PROPERTYDATA_RATE_LIMIT_*`).
- 401 / 403 surface a hint in the error envelope rather than retrying.
- All renderers reproject WGS84 input to the active tile-matrix CRS via
  `pyproj` (BNG `EPSG:27700` by default; Web Mercator `EPSG:3857` is
  available via the `projection=` argument).

For comparable reference implementations, see `hubspot/_client.py`
(API-key) and `salto_ks/_client.py` (OAuth ROPC).
