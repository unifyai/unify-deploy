"""Internal helpers for the data-lookup tools in ``lookups.py``.

Underscore-prefixed so :func:`unity.function_manager.custom_functions.collect_custom_functions`
skips this file.  Holds pure data-shaping utilities used by ``valos_geocode``
to normalise responses from each upstream geocoder (OS Names, OS Places,
postcodes.io, Nominatim) into a single uniform shape.
"""

from __future__ import annotations

import re


# UK postcode pattern (case-insensitive, flexible whitespace).  Matches
# every form Royal Mail issues today: ``M2 2JT``, ``CB24 9EY``,
# ``EC1A 1BB``, ``SW1A 1AA``, ``B1 1AA``, ``GIR 0AA`` (Girobank), etc.
# Anchored — only matches when the entire input is a postcode, so a
# full-address query containing a postcode at the end correctly routes
# to the address-level path (Nominatim / OS Places).
_UK_POSTCODE_RE = re.compile(
    r"^\s*(?:GIR\s*0AA|[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\s*$",
    re.IGNORECASE,
)


def is_uk_postcode(query: str) -> bool:
    """Return whether ``query`` is a bare UK postcode (any whitespace OK)."""
    return bool(_UK_POSTCODE_RE.match(query or ""))


def _normalise_os_response(payload: dict, *, source: str) -> dict:
    """Flatten an OS Names / OS Places response into a uniform shape."""
    if not isinstance(payload, dict):
        return {"error": "Unexpected OS response shape", "raw": payload}
    if "error" in payload:
        return payload

    raw_results = payload.get("results") or []
    flattened: list[dict] = []
    for entry in raw_results:
        item = entry.get("DPA") or entry.get("GAZETTEER_ENTRY") or {}
        if not item:
            continue
        flattened.append(
            {
                "match": item.get("ADDRESS") or item.get("NAME1") or "",
                "lat": _safe_float(item.get("LAT")),
                "lon": _safe_float(item.get("LNG")),
                "easting": _safe_float(
                    item.get("X_COORDINATE") or item.get("GEOMETRY_X"),
                ),
                "northing": _safe_float(
                    item.get("Y_COORDINATE") or item.get("GEOMETRY_Y"),
                ),
                "uprn": item.get("UPRN"),
                "postcode": item.get("POSTCODE") or item.get("POSTCODE_LOCATOR"),
                "local_authority": item.get("LOCAL_CUSTODIAN_CODE_DESCRIPTION")
                or item.get("DISTRICT_BOROUGH"),
            },
        )
    return {"source": source, "results": flattened}


def _normalise_postcodes_io_response(payload: dict) -> dict:
    """Flatten a postcodes.io single-postcode response into the uniform shape.

    postcodes.io returns one match per postcode (centroid of all addresses
    in the postcode unit), so ``results`` always has at most one entry.
    UPRN is unavailable from this source.
    """
    if not isinstance(payload, dict):
        return {"error": "Unexpected postcodes.io response shape", "raw": payload}
    if "error" in payload:
        return payload

    body = payload.get("result") or {}
    if not body:
        return {"source": "postcodes_io", "results": []}

    return {
        "source": "postcodes_io",
        "results": [
            {
                "match": body.get("postcode") or "",
                "lat": _safe_float(body.get("latitude")),
                "lon": _safe_float(body.get("longitude")),
                "easting": _safe_float(body.get("eastings")),
                "northing": _safe_float(body.get("northings")),
                "uprn": None,
                "postcode": body.get("postcode"),
                "local_authority": body.get("admin_district"),
            },
        ],
    }


def _normalise_nominatim_response(payload: dict) -> dict:
    """Flatten a Nominatim list response into the uniform shape.

    Nominatim returns multiple candidates per query.  ``local_authority``
    falls through county / state / city / town / village in that priority
    order — Nominatim's address splits vary by region.  UPRN is
    unavailable from OSM.
    """
    if not isinstance(payload, dict):
        return {"error": "Unexpected Nominatim response shape", "raw": payload}
    if "error" in payload:
        return payload

    raw_results = payload.get("results") or []
    flattened: list[dict] = []
    for item in raw_results:
        addr = item.get("address") or {}
        local_authority = (
            addr.get("county")
            or addr.get("state_district")
            or addr.get("city")
            or addr.get("town")
            or addr.get("village")
        )
        flattened.append(
            {
                "match": item.get("display_name") or "",
                "lat": _safe_float(item.get("lat")),
                "lon": _safe_float(item.get("lon")),
                "easting": None,
                "northing": None,
                "uprn": None,
                "postcode": addr.get("postcode"),
                "local_authority": local_authority,
            },
        )
    return {"source": "nominatim", "results": flattened}


def _safe_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
