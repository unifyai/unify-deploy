# Valos Competitor Resolution

When a market-analysis source (e.g. a Carterwood report) gives a list of
competing care homes by **name only** — no postcode, no address — resolve
them with `valos_find_care_homes`, **not** by geocoding each name through
`valos_geocode`.

Why: a care-home name on its own is unsafe to geocode. A general geocoder
either fails to resolve it or matches an unrelated building of the same
name elsewhere in the country (e.g. a generic "Montague House"). It also
leaves most of the list unplaced. `valos_find_care_homes` lists the
CQC-registered care homes that actually sit inside the catchment and
matches the names against only that radius-bounded set, so a wrong-entity
match is geometrically impossible, and it returns authoritative
coordinates plus each home's CQC rating and bed count.

## Pattern

```
1. Geocode the SUBJECT postcode with valos_geocode -> centre lat/lon
   (postcode centroid is the correct anchor; UPRN/full-address geocode
   is not needed here).
2. Call valos_find_care_homes(lat, lon, radius_km, names=[<all competitor
   names from the market source>])  — one call, the whole list.
3. Build the competitor pins and the CQC-rating column from the returned
   `matches` map: one pin per matched name. Do NOT iterate the raw
   `care_homes` list for pins — it contains duplicate CQC registrations
   and unrelated named-address services that are not competitors.
4. Any name with a null match: leave it listed as unresolved. Do NOT
   fall back to valos_geocode-by-name or any non-valos geocoder for it.
```

## Attribution and figures

- Coordinates and CQC rating come from CQC via `valos_find_care_homes` —
  attribute them to CQC.
- Keep the market source's own bed counts and distances in the
  competition table (that is what a valuer expects); CQC bed counts can
  differ and are a cross-check, not a replacement. Attribute each figure
  to its source.

## Requires a key

`valos_find_care_homes` needs `CQC_PRIMARY_KEY` (a free CQC subscription
key). Without it the tool returns an explicit auth error — surface that
to the operator; do not silently drop the competitors or substitute a
name-geocoding workaround.
