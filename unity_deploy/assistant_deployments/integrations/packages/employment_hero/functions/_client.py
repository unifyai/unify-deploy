"""Internal HTTP client + auth + retry helpers for the Employment Hero package.

Underscore-prefixed so :func:`unity.function_manager.custom_functions.collect_custom_functions`
skips this file (it's library code, not a registered tool).  All sibling
files import from here inside their function bodies to satisfy
FunctionManager's isolation rule.

The token comes from ``EMPLOYMENTHERO_ACCESS_TOKEN``.  Functions degrade
to mock mode when no token is configured.

403 responses are returned as a structured error envelope rather than
raised, so capability gating is uniform across the package: any function
the user's token doesn't cover surfaces gracefully to the actor.
"""

from __future__ import annotations

import asyncio


def _token_or_none() -> str | None:
    import os

    token = os.environ.get("EMPLOYMENTHERO_ACCESS_TOKEN", "")
    return token or None


def _base_url() -> str:
    import os

    return os.environ.get(
        "EMPLOYMENTHERO_BASE_URL", "https://api.employmenthero.com",
    )


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _403_envelope(method: str, path: str) -> dict:
    return {
        "error": f"Employment Hero {method} {path} returned 403",
        "status_code": 403,
        "hint": (
            "403 typically indicates the user's EMPLOYMENTHERO_ACCESS_TOKEN "
            "lacks the scope for this endpoint, or the user's role in EH "
            "doesn't have permission for it.  Tell the user which "
            "capability is unavailable and suggest they regenerate the "
            "token with the needed scope, or contact their EH admin."
        ),
    }


async def eh_get(
    path: str,
    *,
    params: dict | None = None,
    timeout: float | None = None,
) -> dict:
    """GET against the Employment Hero API.

    On rate-limit (429) sleeps the value of the ``Retry-After`` header
    (or ``backoff_factor**attempt`` seconds) and retries up to
    ``EMPLOYMENTHERO_RATE_LIMIT_MAX_RETRIES``.  Returns
    ``{"error": ..., "status_code": ...}`` on persistent failure.
    """
    import os
    import httpx

    token = _token_or_none()
    if token is None:
        return {
            "error": "EMPLOYMENTHERO_ACCESS_TOKEN is not configured.",
            "status_code": None,
        }

    timeout = timeout or float(
        os.environ.get("EMPLOYMENTHERO_REQUEST_TIMEOUT_SECONDS", "30"),
    )
    max_retries = int(
        os.environ.get("EMPLOYMENTHERO_RATE_LIMIT_MAX_RETRIES", "3"),
    )
    backoff_factor = float(
        os.environ.get("EMPLOYMENTHERO_RATE_LIMIT_BACKOFF_FACTOR", "1.5"),
    )

    url = f"{_base_url()}{path}"
    last_err: dict | None = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(max_retries + 1):
            resp = await client.get(url, params=params, headers=_headers(token))
            if resp.status_code == 200:
                return resp.json() if resp.content else {}
            if resp.status_code == 429 and attempt < max_retries:
                retry_after = float(
                    resp.headers.get("Retry-After", backoff_factor**attempt),
                )
                await asyncio.sleep(retry_after)
                continue
            if resp.status_code == 403:
                last_err = _403_envelope("GET", path)
                break
            last_err = {
                "error": f"Employment Hero GET {path} returned {resp.status_code}",
                "status_code": resp.status_code,
                "body": _safe_text(resp),
            }
            break
    return last_err or {"error": "request failed without status"}


async def eh_post(
    path: str,
    body: dict,
    *,
    timeout: float | None = None,
) -> dict:
    """POST against the Employment Hero API.  Same retry semantics as ``eh_get``."""
    import os
    import httpx

    token = _token_or_none()
    if token is None:
        return {
            "error": "EMPLOYMENTHERO_ACCESS_TOKEN is not configured.",
            "status_code": None,
        }

    timeout = timeout or float(
        os.environ.get("EMPLOYMENTHERO_REQUEST_TIMEOUT_SECONDS", "30"),
    )
    max_retries = int(
        os.environ.get("EMPLOYMENTHERO_RATE_LIMIT_MAX_RETRIES", "3"),
    )
    backoff_factor = float(
        os.environ.get("EMPLOYMENTHERO_RATE_LIMIT_BACKOFF_FACTOR", "1.5"),
    )

    url = f"{_base_url()}{path}"
    last_err: dict | None = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(max_retries + 1):
            resp = await client.post(url, json=body, headers=_headers(token))
            if resp.status_code in (200, 201):
                return resp.json() if resp.text else {"status": "ok"}
            if resp.status_code == 429 and attempt < max_retries:
                retry_after = float(
                    resp.headers.get("Retry-After", backoff_factor**attempt),
                )
                await asyncio.sleep(retry_after)
                continue
            if resp.status_code == 403:
                last_err = _403_envelope("POST", path)
                break
            last_err = {
                "error": f"Employment Hero POST {path} returned {resp.status_code}",
                "status_code": resp.status_code,
                "body": _safe_text(resp),
            }
            break
    return last_err or {"error": "request failed without status"}


async def eh_patch(
    path: str,
    body: dict,
    *,
    timeout: float | None = None,
) -> dict:
    """PATCH against the Employment Hero API."""
    import os
    import httpx

    token = _token_or_none()
    if token is None:
        return {
            "error": "EMPLOYMENTHERO_ACCESS_TOKEN is not configured.",
            "status_code": None,
        }

    timeout = timeout or float(
        os.environ.get("EMPLOYMENTHERO_REQUEST_TIMEOUT_SECONDS", "30"),
    )
    max_retries = int(
        os.environ.get("EMPLOYMENTHERO_RATE_LIMIT_MAX_RETRIES", "3"),
    )
    backoff_factor = float(
        os.environ.get("EMPLOYMENTHERO_RATE_LIMIT_BACKOFF_FACTOR", "1.5"),
    )

    url = f"{_base_url()}{path}"
    last_err: dict | None = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(max_retries + 1):
            resp = await client.patch(url, json=body, headers=_headers(token))
            if resp.status_code in (200, 201):
                return resp.json() if resp.text else {"status": "ok"}
            if resp.status_code == 429 and attempt < max_retries:
                retry_after = float(
                    resp.headers.get("Retry-After", backoff_factor**attempt),
                )
                await asyncio.sleep(retry_after)
                continue
            if resp.status_code == 403:
                last_err = _403_envelope("PATCH", path)
                break
            last_err = {
                "error": f"Employment Hero PATCH {path} returned {resp.status_code}",
                "status_code": resp.status_code,
                "body": _safe_text(resp),
            }
            break
    return last_err or {"error": "request failed without status"}


async def eh_delete(path: str, *, timeout: float | None = None) -> dict:
    """DELETE against the Employment Hero API."""
    import os
    import httpx

    token = _token_or_none()
    if token is None:
        return {
            "error": "EMPLOYMENTHERO_ACCESS_TOKEN is not configured.",
            "status_code": None,
        }

    timeout = timeout or float(
        os.environ.get("EMPLOYMENTHERO_REQUEST_TIMEOUT_SECONDS", "30"),
    )
    url = f"{_base_url()}{path}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.delete(url, headers=_headers(token))
    if resp.status_code in (200, 204):
        return {"status": "deleted"}
    if resp.status_code == 403:
        return _403_envelope("DELETE", path)
    return {
        "error": f"Employment Hero DELETE {path} returned {resp.status_code}",
        "status_code": resp.status_code,
        "body": _safe_text(resp),
    }


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

async def eh_paginate(
    path: str,
    *,
    params: dict | None = None,
    page_size: int = 100,
    max_pages: int | None = None,
) -> list[dict]:
    """Iterate cursor-paginated EH list endpoints into a flat list.

    EH paginates with ``next_cursor`` on the body envelope.  Returns an
    empty list on auth/permission/rate failure.
    """
    out: list[dict] = []
    cursor: str | None = None
    page = 0
    base_params = dict(params or {})
    base_params.setdefault("limit", page_size)
    while True:
        merged = dict(base_params)
        if cursor:
            merged["cursor"] = cursor
        body = await eh_get(path, params=merged)
        if "error" in body:
            return out
        items = body.get("data") or body.get("items") or body.get("results") or []
        out.extend(items)
        cursor = body.get("next_cursor") or body.get("paging", {}).get("next")
        page += 1
        if not cursor:
            break
        if max_pages is not None and page >= max_pages:
            break
    return out


# ---------------------------------------------------------------------------
# Org-scoped path helpers
# ---------------------------------------------------------------------------

def _org_id_or_error() -> tuple[str | None, dict | None]:
    """Return ``(organisation_id, None)`` on success or ``(None, error_envelope)``
    if the organisation id is unknown.
    """
    import os

    org_id = os.environ.get("EMPLOYMENTHERO_ORGANISATION_ID")
    if not org_id:
        return None, {
            "error": "EMPLOYMENTHERO_ORGANISATION_ID is not set.",
            "hint": (
                "Call get_active_organisation() to discover the org id, "
                "or set EMPLOYMENTHERO_ORGANISATION_ID via Console -> Secrets."
            ),
            "status_code": None,
        }
    return org_id, None


def org_path(suffix: str) -> str:
    """Build ``/api/v1/organisations/{org_id}{suffix}``.  Caller must check
    that the org id is set first via ``_org_id_or_error()``.
    """
    import os

    org_id = os.environ.get("EMPLOYMENTHERO_ORGANISATION_ID", "")
    if suffix and not suffix.startswith("/"):
        suffix = "/" + suffix
    return f"/api/v1/organisations/{org_id}{suffix}"


def _safe_text(resp) -> str:
    try:
        return resp.text[:500]
    except Exception:
        return ""
