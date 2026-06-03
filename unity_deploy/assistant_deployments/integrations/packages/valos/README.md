# Valos — UK Property Data

UK property valuation data layer. Replaces the original placeholder
pass-through to `api.valos.ai` with two upstream services that Unify
holds accounts for:

- **Ordnance Survey Data Hub** — OS Maps API (raster WMTS tiles), OS
  Names API (gazetteer geocoding), OS Places API (address lookup with
  UPRN). Single key (`OS_MAPS_API_KEY`).
- **PropertyData** — `/api/freeholds` (postcode → registered titles +
  INSPIRE polygons), `/api/title-information` (full boundary GeoJSON,
  ownership, tenure). Single key (`PROPERTYDATA_API_KEY`).

Demographics enrichment uses `postcodes.io` and the ONS open APIs — no
key required.

> **Opt-in only, no Console card.** This package has no entry in the
> generic Console `INTEGRATION_PROVIDERS` registry. Any client
> deployment that needs UK property/valuation data opts in via the
> `integrations=[...]` argument on `BASE_SPEC.derive(...)`. Customers
> never see, configure, or hold the upstream credentials; both keys
> are Unify-owned and populated in the deployment env.

## Surface

Five typed primitives the actor can call directly:

| Function | Purpose |
| --- | --- |
| `valos_geocode(query, max_results)` | Address/postcode → coordinates + UPRN. OS Places first, falls through to OS Names on 403. |
| `valos_lookup_freeholds(postcode \| lat+lon)` | Postcode/coords → freehold title numbers + INSPIRE ids + tenure. |
| `valos_get_title_polygon(title_number \| inspire_id)` | Title id → GeoJSON boundary + plot size + ownership. |
| `valos_postcode_demographics(postcode, radius_km)` | Postcode → LSOA/MSOA + local authority + (later) census/IMD. |
| `valos_render_os_map(geometry, output_path, ...)` | OS Maps WMTS layer → PNG, with optional polygon overlay. |
| `valos_render_location_plan(polygon, output_path, ...)` | Land-Registry-style plan with thick boundary + north arrow + scale bar. |
| `valos_render_competitor_map(centre, radius_km, pins, output_path, ...)` | Outdoor map + numbered competitor pins. |

All renderers persist PNGs at the supplied filesystem path so
`ImageManager` resolves them later via `filter_images(filter="filepath
== '...'")` — same cross-manager image convention used throughout the
assistant.

## Authentication

Two required, Unify-owned secrets populated in the deployment env:

- `OS_MAPS_API_KEY` — OS Data Hub Premium plan, covers OS Maps + Names
  + Places.
- `PROPERTYDATA_API_KEY` — PropertyData subscription with the Land
  Registry endpoints enabled.

Neither key is customer-supplied. Both are seeded by the deployment env
pipeline; the customer-facing Console has no card for this package.

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

## Metering and caps

`_metering.py` keeps per-day, per-process call counters across three
upstream channels:

| Provider | Default cap (env override) |
| --- | --- |
| `os_tiles` | 5,000/day (`VALOS_OS_TILES_DAILY_LIMIT`) |
| `os_names_places` | 1,000/day (`VALOS_OS_NAMES_PLACES_DAILY_LIMIT`) |
| `propertydata` | 500/day (`VALOS_PROPERTYDATA_DAILY_LIMIT`) |

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
