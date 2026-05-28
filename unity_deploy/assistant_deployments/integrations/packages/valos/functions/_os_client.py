"""Internal OS Data Hub client — OS Maps (WMTS), OS Names, OS Places.

Underscore-prefixed so :func:`unity.function_manager.custom_functions.collect_custom_functions`
skips this file.  Sibling function modules import from here inside their
function bodies to satisfy FunctionManager's isolation rule.

Authenticated with a single ``OS_MAPS_API_KEY`` (Unify-owned, populated
in the deployment env).  All three OS surfaces share the key — there is
no per-product key in the OS Data Hub model.
"""

from __future__ import annotations

import asyncio

_OS_BASE = "https://api.os.uk"
_NAMES_PATH = "/search/names/v1/find"
_PLACES_PATH = "/search/places/v1/find"
_MAPS_WMTS_PATH = "/maps/raster/v1/wmts"


def _api_key_or_none() -> str | None:
    import os

    key = os.environ.get("OS_MAPS_API_KEY", "")
    return key or None


async def os_get_json(
    path: str,
    *,
    params: dict | None = None,
    timeout: float | None = None,
) -> dict:
    """GET a JSON-returning OS Data Hub endpoint with retry + structured failure."""
    import os
    import httpx

    from unity_deploy.assistant_deployments.integrations.packages.valos.functions._metering import (
        PROVIDER_OS_NAMES_PLACES,
        quota_envelope,
        record_and_check,
    )

    api_key = _api_key_or_none()
    if api_key is None:
        return {
            "error": "OS_MAPS_API_KEY is not configured.",
            "status_code": None,
            "hint": (
                "OS_MAPS_API_KEY must be populated in the deployment env "
                "by the Unify-managed credential pipeline.  This is not a "
                "customer-supplied secret."
            ),
        }

    exceeded, count, limit = record_and_check(PROVIDER_OS_NAMES_PLACES)
    if exceeded:
        return quota_envelope(PROVIDER_OS_NAMES_PLACES, count, limit)

    timeout = timeout or float(os.environ.get("OS_REQUEST_TIMEOUT_SECONDS", "30"))
    max_retries = int(os.environ.get("OS_RATE_LIMIT_MAX_RETRIES", "3"))
    backoff_factor = float(os.environ.get("OS_RATE_LIMIT_BACKOFF_FACTOR", "1.5"))

    merged_params = dict(params or {})
    merged_params["key"] = api_key

    url = f"{_OS_BASE}{path}"
    last_err: dict | None = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(max_retries + 1):
            resp = await client.get(url, params=merged_params)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429 and attempt < max_retries:
                retry_after = float(
                    resp.headers.get("Retry-After", backoff_factor**attempt),
                )
                await asyncio.sleep(retry_after)
                continue
            last_err = {
                "error": f"OS GET {path} returned {resp.status_code}",
                "status_code": resp.status_code,
                "body": _safe_text(resp),
            }
            if resp.status_code == 401:
                last_err["hint"] = (
                    "401 indicates OS_MAPS_API_KEY is invalid or revoked — "
                    "rotate via the OS Data Hub console."
                )
            elif resp.status_code == 403:
                last_err["hint"] = (
                    "403 typically indicates the OS Data Hub plan does not "
                    "cover this product (e.g. OS Places requires Premium)."
                )
            break
    return last_err or {"error": "request failed without status"}


async def os_fetch_wmts_tile(
    *,
    layer: str,
    tile_matrix_set: str,
    tile_matrix: str,
    tile_row: int,
    tile_col: int,
    timeout: float | None = None,
) -> bytes | dict:
    """Fetch a single WMTS raster tile.

    Returns the raw PNG bytes on success, or an ``{"error", ...}`` dict
    on failure.  The caller is responsible for assembling tiles into a
    composite image.
    """
    import os
    import httpx

    from unity_deploy.assistant_deployments.integrations.packages.valos.functions._metering import (
        PROVIDER_OS_TILES,
        quota_envelope,
        record_and_check,
    )

    api_key = _api_key_or_none()
    if api_key is None:
        return {
            "error": "OS_MAPS_API_KEY is not configured.",
            "status_code": None,
        }

    exceeded, count, limit = record_and_check(PROVIDER_OS_TILES)
    if exceeded:
        return quota_envelope(PROVIDER_OS_TILES, count, limit)

    timeout = timeout or float(os.environ.get("OS_REQUEST_TIMEOUT_SECONDS", "30"))
    max_retries = int(os.environ.get("OS_RATE_LIMIT_MAX_RETRIES", "3"))
    backoff_factor = float(os.environ.get("OS_RATE_LIMIT_BACKOFF_FACTOR", "1.5"))

    params = {
        "service": "WMTS",
        "request": "GetTile",
        "version": "2.0.0",
        "style": "default",
        "format": "image/png",
        "layer": layer,
        "tileMatrixSet": tile_matrix_set,
        "tileMatrix": tile_matrix,
        "tileRow": tile_row,
        "tileCol": tile_col,
        "key": api_key,
    }

    url = f"{_OS_BASE}{_MAPS_WMTS_PATH}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(max_retries + 1):
            resp = await client.get(url, params=params)
            if resp.status_code == 200:
                return resp.content
            if resp.status_code == 429 and attempt < max_retries:
                retry_after = float(
                    resp.headers.get("Retry-After", backoff_factor**attempt),
                )
                await asyncio.sleep(retry_after)
                continue
            return {
                "error": (
                    f"OS WMTS GetTile {layer}/{tile_matrix}/{tile_row}/"
                    f"{tile_col} returned {resp.status_code}"
                ),
                "status_code": resp.status_code,
                "body": _safe_text(resp),
            }
    return {"error": "WMTS tile fetch failed without status"}


async def os_names_find(query: str, *, max_results: int = 10) -> dict:
    """OS Names — gazetteer lookup for places, postcodes, settlements."""
    return await os_get_json(
        _NAMES_PATH,
        params={"query": query, "maxresults": max_results},
    )


async def os_places_find(query: str, *, max_results: int = 10) -> dict:
    """OS Places — address-level lookup including UPRN.

    Premium-tier API; on a free plan this returns 403.  The caller
    should fall through to ``os_names_find`` when that happens.
    """
    return await os_get_json(
        _PLACES_PATH,
        params={"query": query, "maxresults": max_results},
    )


def _safe_text(resp) -> str:
    try:
        return resp.text[:500]
    except Exception:
        return ""
