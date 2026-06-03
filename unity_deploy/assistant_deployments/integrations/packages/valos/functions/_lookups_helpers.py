"""Internal helpers for the data-lookup tools in ``lookups.py``.

Underscore-prefixed so :func:`unity.function_manager.custom_functions.collect_custom_functions`
skips this file.  Holds pure data-shaping utilities used by ``valos_geocode``
to normalise OS Names / OS Places responses into a uniform shape.
"""

from __future__ import annotations


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


def _safe_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
