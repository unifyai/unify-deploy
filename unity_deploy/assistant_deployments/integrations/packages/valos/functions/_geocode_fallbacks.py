"""Free-tier UK geocoding fallbacks for the Valos package.

Underscore-prefixed so :func:`unify.function_manager.custom_functions.collect_custom_functions`
skips this file.  Sibling function modules import from here inside their
function bodies to satisfy FunctionManager's isolation rule.

Two unauthenticated upstreams covering the gap left by OS Data Hub
projects that don't have OS Names / OS Places enabled (i.e. anything
short of a Premium plan):

* :func:`postcodes_io_lookup` — UK postcode -> WGS84 + LSOA / MSOA /
  local authority via api.postcodes.io.  Postcode-centroid only;
  no UPRN.
* :func:`nominatim_search` — free-text address -> WGS84 + admin
  metadata via OpenStreetMap's Nominatim service.  Building-level
  resolution where OSM has it; no UPRN.

Both return payloads in the same shape ``_lookups_helpers`` knows how
to normalise, so the public ``valos_geocode`` primitive can compose
them transparently with the OS-side clients.
"""

from __future__ import annotations

import asyncio

_USER_AGENT = "unify-valos/0.3 (https://unify.ai)"


async def postcodes_io_lookup(postcode: str) -> dict:
    """Resolve a UK postcode via api.postcodes.io.

    Returns a dict with the same top-level keys as the OS clients
    (``error`` on failure, otherwise raw postcodes.io payload).  The
    caller normalises shape via ``_normalise_postcodes_io_response``.
    """
    import os

    import httpx

    timeout = float(os.environ.get("POSTCODES_IO_REQUEST_TIMEOUT_SECONDS", "15"))
    cleaned = postcode.replace(" ", "").upper()
    url = f"https://api.postcodes.io/postcodes/{cleaned}"

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, headers={"User-Agent": _USER_AGENT})
    if resp.status_code != 200:
        body = resp.text[:500] if resp.text else ""
        return {
            "error": f"postcodes.io returned {resp.status_code} for '{postcode}'",
            "status_code": resp.status_code,
            "body": body,
        }
    return resp.json() or {}


async def nominatim_search(query: str, *, max_results: int = 5) -> dict:
    """Resolve a free-text UK address via Nominatim (OpenStreetMap).

    Honours Nominatim's published etiquette:
      * Identifying ``User-Agent`` (required).
      * ``countrycodes=gb`` to scope to the UK and reduce ambiguity.
      * Per-process daily ceiling enforced upstream by ``_metering``.

    Returns a dict with the same shape contract as :func:`postcodes_io_lookup`
    — ``error`` on failure, otherwise the raw Nominatim list payload
    wrapped under ``"results"`` so the normaliser has a stable key.
    """
    import os

    import httpx

    from unity_deploy.assistant_deployments.integrations.packages.valos.functions._metering import (
        PROVIDER_NOMINATIM,
        quota_envelope,
        record_and_check,
    )

    exceeded, count, limit = record_and_check(PROVIDER_NOMINATIM)
    if exceeded:
        return quota_envelope(PROVIDER_NOMINATIM, count, limit)

    timeout = float(os.environ.get("NOMINATIM_REQUEST_TIMEOUT_SECONDS", "20"))
    # Nominatim's published policy is "no more than 1 request per second".
    # The async tool loop typically chains these naturally over a longer
    # window; a small sleep here protects the rare burst case.
    throttle_seconds = float(os.environ.get("NOMINATIM_THROTTLE_SECONDS", "1.0"))
    if throttle_seconds > 0:
        await asyncio.sleep(throttle_seconds)

    params = {
        "q": query,
        "format": "json",
        "addressdetails": "1",
        "countrycodes": "gb",
        "limit": str(max(1, min(int(max_results or 5), 50))),
    }

    url = "https://nominatim.openstreetmap.org/search"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(
            url,
            params=params,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        )
    if resp.status_code != 200:
        return {
            "error": f"Nominatim returned {resp.status_code} for '{query}'",
            "status_code": resp.status_code,
            "body": resp.text[:500] if resp.text else "",
        }
    payload = resp.json()
    if not isinstance(payload, list):
        return {"error": "Unexpected Nominatim response shape", "raw": payload}
    return {"results": payload}
