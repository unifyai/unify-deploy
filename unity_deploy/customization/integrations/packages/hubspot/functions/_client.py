"""Internal HTTP client + auth + retry helpers for the HubSpot package.

Underscore-prefixed so :func:`unity.function_manager.custom_functions.collect_custom_functions`
skips this file (it's library code, not a registered tool).  All sibling
files import from here inside their function bodies to satisfy
FunctionManager's isolation rule.

The token comes from ``HUBSPOT_PRIVATE_APP_TOKEN``.  Functions degrade
to mock mode when no token is configured.
"""

from __future__ import annotations

import asyncio


_API_BASE = "https://api.hubapi.com"


def _token_or_none() -> str | None:
    import os

    token = os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN", "")
    return token or None


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def hubspot_get(
    path: str,
    *,
    params: dict | None = None,
    timeout: float | None = None,
) -> dict:
    """GET against the HubSpot API.

    Returns the parsed JSON body on success.  On rate-limit (429),
    sleeps the value of the ``Retry-After`` header (or 1.5^attempt
    seconds) and retries up to ``HUBSPOT_RATE_LIMIT_MAX_RETRIES``.
    Returns ``{"error": ..., "status_code": ...}`` on persistent failure.
    """
    import os
    import httpx

    token = _token_or_none()
    if token is None:
        return {"error": "HUBSPOT_PRIVATE_APP_TOKEN is not configured."}

    timeout = timeout or float(os.environ.get("HUBSPOT_REQUEST_TIMEOUT_SECONDS", "30"))
    max_retries = int(os.environ.get("HUBSPOT_RATE_LIMIT_MAX_RETRIES", "3"))
    backoff_factor = float(os.environ.get("HUBSPOT_RATE_LIMIT_BACKOFF_FACTOR", "1.5"))

    url = f"{_API_BASE}{path}"
    last_err: dict | None = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(max_retries + 1):
            resp = await client.get(url, params=params, headers=_headers(token))
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429 and attempt < max_retries:
                retry_after = float(resp.headers.get("Retry-After", backoff_factor**attempt))
                await asyncio.sleep(retry_after)
                continue
            last_err = {
                "error": f"HubSpot GET {path} returned {resp.status_code}",
                "status_code": resp.status_code,
                "body": _safe_text(resp),
            }
            if resp.status_code == 403:
                last_err["hint"] = (
                    "403 typically indicates a missing scope on the Private App "
                    "or a HubSpot tier that does not include this surface."
                )
            break
    return last_err or {"error": "request failed without status"}


async def hubspot_post(path: str, body: dict, *, timeout: float | None = None) -> dict:
    """POST against the HubSpot API.  Same retry semantics as ``hubspot_get``."""
    import os
    import httpx

    token = _token_or_none()
    if token is None:
        return {"error": "HUBSPOT_PRIVATE_APP_TOKEN is not configured."}

    timeout = timeout or float(os.environ.get("HUBSPOT_REQUEST_TIMEOUT_SECONDS", "30"))
    max_retries = int(os.environ.get("HUBSPOT_RATE_LIMIT_MAX_RETRIES", "3"))
    backoff_factor = float(os.environ.get("HUBSPOT_RATE_LIMIT_BACKOFF_FACTOR", "1.5"))

    url = f"{_API_BASE}{path}"
    last_err: dict | None = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(max_retries + 1):
            resp = await client.post(url, json=body, headers=_headers(token))
            if resp.status_code in (200, 201):
                return resp.json() if resp.text else {"status": "ok"}
            if resp.status_code == 429 and attempt < max_retries:
                retry_after = float(resp.headers.get("Retry-After", backoff_factor**attempt))
                await asyncio.sleep(retry_after)
                continue
            last_err = {
                "error": f"HubSpot POST {path} returned {resp.status_code}",
                "status_code": resp.status_code,
                "body": _safe_text(resp),
            }
            break
    return last_err or {"error": "request failed without status"}


async def hubspot_patch(path: str, body: dict, *, timeout: float | None = None) -> dict:
    """PATCH against the HubSpot API."""
    import os
    import httpx

    token = _token_or_none()
    if token is None:
        return {"error": "HUBSPOT_PRIVATE_APP_TOKEN is not configured."}

    timeout = timeout or float(os.environ.get("HUBSPOT_REQUEST_TIMEOUT_SECONDS", "30"))
    max_retries = int(os.environ.get("HUBSPOT_RATE_LIMIT_MAX_RETRIES", "3"))
    backoff_factor = float(os.environ.get("HUBSPOT_RATE_LIMIT_BACKOFF_FACTOR", "1.5"))

    url = f"{_API_BASE}{path}"
    last_err: dict | None = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(max_retries + 1):
            resp = await client.patch(url, json=body, headers=_headers(token))
            if resp.status_code in (200, 201):
                return resp.json() if resp.text else {"status": "ok"}
            if resp.status_code == 429 and attempt < max_retries:
                retry_after = float(resp.headers.get("Retry-After", backoff_factor**attempt))
                await asyncio.sleep(retry_after)
                continue
            last_err = {
                "error": f"HubSpot PATCH {path} returned {resp.status_code}",
                "status_code": resp.status_code,
                "body": _safe_text(resp),
            }
            break
    return last_err or {"error": "request failed without status"}


async def hubspot_delete(path: str, *, timeout: float | None = None) -> dict:
    """DELETE against the HubSpot API."""
    import os
    import httpx

    token = _token_or_none()
    if token is None:
        return {"error": "HUBSPOT_PRIVATE_APP_TOKEN is not configured."}

    timeout = timeout or float(os.environ.get("HUBSPOT_REQUEST_TIMEOUT_SECONDS", "30"))
    url = f"{_API_BASE}{path}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.delete(url, headers=_headers(token))
    if resp.status_code in (200, 204):
        return {"status": "deleted"}
    return {
        "error": f"HubSpot DELETE {path} returned {resp.status_code}",
        "status_code": resp.status_code,
        "body": _safe_text(resp),
    }


async def hubspot_search(
    object_type: str,
    *,
    filter_groups: list[dict] | None = None,
    sorts: list[dict] | None = None,
    properties: list[str] | None = None,
    after: str | None = None,
    limit: int = 100,
    query: str | None = None,
) -> dict:
    """POST to ``/crm/v3/objects/{object_type}/search``."""
    body: dict = {
        "filterGroups": filter_groups or [],
        "sorts": sorts or [],
        "properties": properties or [],
        "limit": limit,
    }
    if after is not None:
        body["after"] = after
    if query is not None:
        body["query"] = query
    return await hubspot_post(f"/crm/v3/objects/{object_type}/search", body)


def _safe_text(resp) -> str:
    try:
        return resp.text[:500]
    except Exception:
        return ""
