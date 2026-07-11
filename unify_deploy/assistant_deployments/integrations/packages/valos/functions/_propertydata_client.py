"""Internal PropertyData client — Land Registry freeholds + title-information.

Underscore-prefixed so :func:`unify.function_manager.custom_functions.collect_custom_functions`
skips this file.  Sibling function modules import from here inside their
function bodies to satisfy FunctionManager's isolation rule.

PropertyData wraps HM Land Registry's INSPIRE Index Polygons + corporate
ownership data behind a clean JSON API; their key is single-use across
all endpoints.  Their ToS may include reseller / multi-tenant clauses —
confirm reseller / multi-tenant terms with PropertyData before opting
more than one client deployment into this package.
"""

from __future__ import annotations

import asyncio

_PROPERTYDATA_BASE = "https://api.propertydata.co.uk"


def _api_key_or_none() -> str | None:
    import os

    key = os.environ.get("PROPERTYDATA_API_KEY", "")
    return key or None


async def propertydata_get(
    path: str,
    *,
    params: dict | None = None,
    timeout: float | None = None,
) -> dict:
    """GET a PropertyData JSON endpoint with retry + structured failure.

    The API key is appended as a ``key`` query-string parameter, which
    is PropertyData's documented convention.
    """
    import os
    import httpx

    from unify_deploy.assistant_deployments.integrations.packages.valos.functions._metering import (
        PROVIDER_PROPERTYDATA,
        quota_envelope,
        record_and_check,
    )

    api_key = _api_key_or_none()
    if api_key is None:
        return {
            "error": "PROPERTYDATA_API_KEY is not configured.",
            "status_code": None,
            "hint": (
                "PROPERTYDATA_API_KEY must be populated in the assistant's "
                "/Secrets context — either by the deploy-time "
                "integrations=[...] seed pipeline (Unify-owned shared key) "
                "or by per-assistant Console paste (customer-supplied key)."
            ),
        }

    exceeded, count, limit = record_and_check(PROVIDER_PROPERTYDATA)
    if exceeded:
        return quota_envelope(PROVIDER_PROPERTYDATA, count, limit)

    timeout = timeout or float(
        os.environ.get("PROPERTYDATA_REQUEST_TIMEOUT_SECONDS", "30"),
    )
    max_retries = int(os.environ.get("PROPERTYDATA_RATE_LIMIT_MAX_RETRIES", "3"))
    backoff_factor = float(
        os.environ.get("PROPERTYDATA_RATE_LIMIT_BACKOFF_FACTOR", "1.5"),
    )

    merged_params = dict(params or {})
    merged_params["key"] = api_key

    url = f"{_PROPERTYDATA_BASE}{path}"
    last_err: dict | None = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(max_retries + 1):
            resp = await client.get(url, params=merged_params)
            if resp.status_code == 200:
                payload = resp.json()
                # PropertyData embeds a per-call status in the body; surface
                # API-level failures (e.g. quota exhaustion) as errors even
                # though the HTTP code was 200.
                if isinstance(payload, dict) and payload.get("status") == "error":
                    return {
                        "error": payload.get("message")
                        or "PropertyData returned status=error",
                        "status_code": 200,
                        "body": payload,
                    }
                return payload
            if resp.status_code == 429 and attempt < max_retries:
                retry_after = float(
                    resp.headers.get("Retry-After", backoff_factor**attempt),
                )
                await asyncio.sleep(retry_after)
                continue
            last_err = {
                "error": f"PropertyData GET {path} returned {resp.status_code}",
                "status_code": resp.status_code,
                "body": _safe_text(resp),
            }
            if resp.status_code == 401:
                last_err["hint"] = (
                    "401 indicates PROPERTYDATA_API_KEY is invalid or revoked."
                )
            elif resp.status_code == 403:
                last_err["hint"] = (
                    "403 typically indicates the subscription tier does not "
                    "include this endpoint — check that the Land Registry "
                    "endpoints are enabled on the PropertyData plan."
                )
            break
    return last_err or {"error": "request failed without status"}


async def propertydata_freeholds(
    *,
    postcode: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
) -> dict:
    """List freehold titles for a postcode or coordinate pair.

    Exactly one of ``postcode`` or ``lat``+``lon`` should be supplied.
    """
    if postcode is not None:
        params: dict = {"postcode": postcode}
    elif lat is not None and lon is not None:
        params = {"lat": lat, "lon": lon}
    else:
        return {
            "error": "Provide either postcode or both lat and lon.",
            "status_code": None,
        }
    return await propertydata_get("/api/freeholds", params=params)


async def propertydata_title_information(
    *,
    title_number: str | None = None,
    inspire_id: str | None = None,
) -> dict:
    """Full title information including boundary polygon as GeoJSON."""
    if title_number is not None:
        params: dict = {"title": title_number}
    elif inspire_id is not None:
        params = {"inspire_id": inspire_id}
    else:
        return {
            "error": "Provide either title_number or inspire_id.",
            "status_code": None,
        }
    return await propertydata_get("/api/title-information", params=params)


def _safe_text(resp) -> str:
    try:
        return resp.text[:500]
    except Exception:
        return ""
