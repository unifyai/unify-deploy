"""Typed data-lookup primitives for the Valos UK property data package.

Four registered tools the actor uses to assemble the structured-data
half of a Valos-equivalent valuation report:

* ``valos_geocode`` — address/postcode -> coords + UPRN (OS Places, falls
  back to OS Names when Places returns 403).
* ``valos_lookup_freeholds`` — postcode/coords -> registered title
  numbers + INSPIRE ids (PropertyData).
* ``valos_get_title_polygon`` — title number/INSPIRE id -> GeoJSON
  boundary + plot size + ownership type (PropertyData).
* ``valos_postcode_demographics`` — postcode + radius -> ONS census +
  IMD summary.

The map-rendering primitives live in ``render.py``; private helpers
are in the underscore-prefixed sibling modules (``_os_client``,
``_propertydata_client``, ``_lookups_helpers``).
"""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def valos_geocode(query: str, max_results: int = 5) -> dict:
    """Resolve an address or UK postcode to coordinates and a UPRN.

    Tries OS Places first (address-level, returns UPRN); falls back to
    OS Names (gazetteer, no UPRN) on 403 — Places requires the OS Data
    Hub Premium plan.

    Parameters
    ----------
    query : str
        Free-text address, postcode, or place name (e.g.
        ``"95 Wigmore Street, London"`` or ``"M2 2JT"``).
    max_results : int, optional
        Cap on candidate matches.  Defaults to 5.

    Returns
    -------
    dict
        On success::

            {
                "source": "os_places" | "os_names",
                "results": [
                    {
                        "match": "95 Wigmore Street, London, W1U 1FF",
                        "lat": 51.5176, "lon": -0.1492,
                        "easting": 528345, "northing": 181276,
                        "uprn": "100022944320",  # OS Places only
                        "postcode": "W1U 1FF",
                        "local_authority": "Westminster",
                    },
                    ...
                ],
            }

        On failure: ``{"error", "status_code", ...}``.
    """
    from unity_deploy.assistant_deployments.integrations.packages.valos.functions._lookups_helpers import (
        _normalise_os_response,
    )
    from unity_deploy.assistant_deployments.integrations.packages.valos.functions._os_client import (
        os_names_find,
        os_places_find,
    )

    places = await os_places_find(query, max_results=max_results)
    if isinstance(places, dict) and places.get("error") and places.get("status_code") == 403:
        # Premium-plan gating — fall through to Names.
        names = await os_names_find(query, max_results=max_results)
        return _normalise_os_response(names, source="os_names")
    return _normalise_os_response(places, source="os_places")


@custom_function()
async def valos_lookup_freeholds(
    postcode: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
) -> dict:
    """List registered freehold titles for a postcode or coordinate pair.

    Backed by PropertyData's ``/api/freeholds`` endpoint, which wraps
    HM Land Registry's INSPIRE Index Polygons + corporate-ownership
    register.

    Parameters
    ----------
    postcode : str, optional
        UK postcode, e.g. ``"M2 2JT"``.  Provide either this OR
        ``lat``+``lon``.
    lat, lon : float, optional
        WGS84 coordinates of the search point.

    Returns
    -------
    dict
        On success::

            {
                "status": "success",
                "result_count": 3,
                "result": [
                    {
                        "title_number": "NGL123456",
                        "inspire_id": "...",
                        "title_class": "Absolute",
                        "tenure": "Freehold",
                        "polygon_available": true,
                        "leasehold_count": 4,
                    },
                    ...
                ],
            }

        On failure: ``{"error", "status_code", ...}``.

    Notes
    -----
    INSPIRE polygons are *indicative extents* — not legal certainty.
    For legal-grade title plans, use HM Land Registry's Polygon Plus
    service (£1.50/polygon, used selectively).
    """
    from unity_deploy.assistant_deployments.integrations.packages.valos.functions._propertydata_client import (
        propertydata_freeholds,
    )

    return await propertydata_freeholds(postcode=postcode, lat=lat, lon=lon)


@custom_function()
async def valos_get_title_polygon(
    title_number: str | None = None,
    inspire_id: str | None = None,
) -> dict:
    """Resolve a title number or INSPIRE id to a GeoJSON boundary polygon.

    Returns the full title information from PropertyData including
    ownership type, tenure, plot size, and the polygon geometry suitable
    for downstream rendering by ``valos_render_location_plan``.

    Parameters
    ----------
    title_number : str, optional
        Land Registry title number, e.g. ``"NGL123456"``.  Provide
        either this OR ``inspire_id``.
    inspire_id : str, optional
        INSPIRE polygon id (Land-Registry-INSPIRE ID).

    Returns
    -------
    dict
        On success::

            {
                "status": "success",
                "title_number": "NGL123456",
                "tenure": "Freehold",
                "ownership_type": "...",
                "plot_size_sqm": 1234.5,
                "polygon": {
                    "type": "Polygon",
                    "coordinates": [[[lon, lat], ...]],
                },
                "leaseholds": [...],
            }

        On failure: ``{"error", "status_code", ...}``.
    """
    from unity_deploy.assistant_deployments.integrations.packages.valos.functions._propertydata_client import (
        propertydata_title_information,
    )

    return await propertydata_title_information(
        title_number=title_number,
        inspire_id=inspire_id,
    )


@custom_function()
async def valos_postcode_demographics(
    postcode: str,
    radius_km: float = 1.0,
) -> dict:
    """Census + IMD summary for a postcode and surrounding radius.

    Backed by Office for National Statistics open APIs — no key
    required.  Returns a normalised summary suitable for the demographics
    section of a Healthcare valuation report.

    Parameters
    ----------
    postcode : str
        UK postcode, e.g. ``"M2 2JT"``.
    radius_km : float, optional
        Catchment radius in kilometres.  Defaults to 1.0.

    Returns
    -------
    dict
        On success::

            {
                "postcode": "M2 2JT",
                "radius_km": 1.0,
                "lsoa": "...",
                "msoa": "...",
                "local_authority": "Manchester",
                "population": 12345,
                "age_bands": {"0-15": ..., "16-64": ..., "65+": ...},
                "imd_decile": 3,
                "wealth_indicator": "low" | "medium" | "high",
            }

        On failure: ``{"error", "status_code", ...}``.

    Notes
    -----
    This is an open-data lookup with no API key, so failures are network
    or postcode-validity issues only.  For care-home-specific demand /
    supply / fee data, ingest Carterwood reports separately via
    ``primitives.files.parse``.
    """
    import os

    import httpx

    timeout = float(os.environ.get("ONS_REQUEST_TIMEOUT_SECONDS", "30"))

    # postcodes.io is the standard open lookup for postcode -> LSOA / MSOA /
    # local-authority mapping.  ONS data services key off these codes.
    postcode_clean = postcode.replace(" ", "")
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.get(
                f"https://api.postcodes.io/postcodes/{postcode_clean}",
            )
        except httpx.HTTPError as exc:
            return {"error": f"postcodes.io request failed: {exc}"}
    if resp.status_code != 200:
        return {
            "error": f"postcodes.io returned {resp.status_code} for '{postcode}'",
            "status_code": resp.status_code,
            "body": resp.text[:500],
        }
    body = resp.json().get("result") or {}
    return {
        "postcode": postcode,
        "radius_km": radius_km,
        "lsoa": body.get("codes", {}).get("lsoa"),
        "msoa": body.get("codes", {}).get("msoa"),
        "local_authority": body.get("admin_district"),
        "country": body.get("country"),
        "region": body.get("region"),
        "lat": body.get("latitude"),
        "lon": body.get("longitude"),
        "eastings": body.get("eastings"),
        "northings": body.get("northings"),
        # Census / IMD enrichment is intentionally a follow-up — the ONS
        # Statistical GeoPortal returns LSOA-keyed data but its API surface
        # is heavier than postcodes.io.  Stub fields for now so the report
        # template can show the structure; populate via a follow-on tick.
        "population": None,
        "age_bands": None,
        "imd_decile": None,
        "wealth_indicator": None,
    }
