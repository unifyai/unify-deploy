"""Internal HTTP client + auth + retry helpers for the Matterport package.

Underscore-prefixed so :func:`droid.function_manager.custom_functions.collect_custom_functions`
skips this file (it's library code, not a registered tool).  All sibling
files import from here inside their function bodies to satisfy
FunctionManager's isolation rule.

Authentication:

* HTTP Basic with ``MATTERPORT_TOKEN_ID`` (username) and
  ``MATTERPORT_TOKEN_SECRET`` (password).  Both must be set; either
  missing returns a structured ``"not connected"`` envelope.
* The Model API GraphQL endpoint at
  ``https://api.matterport.com/api/models/graph`` is the only public
  surface this package targets.  Account API and Import API are
  Enterprise-only and out of scope for v1.

403 responses are returned as a structured error envelope rather than
raised, so capability gating is uniform across the package.
"""

from __future__ import annotations

import asyncio

_DEFAULT_GRAPH_PATH = "/api/models/graph"


def _base_url() -> str:
    import os

    return os.environ.get("MATTERPORT_BASE_URL", "https://api.matterport.com")


def _credentials() -> tuple[str | None, str | None]:
    import os

    return (
        os.environ.get("MATTERPORT_TOKEN_ID") or None,
        os.environ.get("MATTERPORT_TOKEN_SECRET") or None,
    )


def _not_connected_envelope(missing: list[str]) -> dict:
    return {
        "error": "Matterport is not connected for this assistant.",
        "status_code": None,
        "missing_secrets": missing,
        "hint": (
            "Direct the user to Console -> Integrations -> Matterport.  "
            "They generate an API token at Matterport account Settings -> "
            "Account -> API Access, then paste both Token ID and Token "
            "Secret into the integrations modal."
        ),
    }


def _403_envelope(operation: str) -> dict:
    return {
        "error": f"Matterport {operation} returned 403",
        "status_code": 403,
        "hint": (
            "403 typically indicates the active Matterport plan does not "
            "include this surface (Developer Tools add-on may be missing) "
            "or the token does not cover it.  Tell the user which "
            "capability is unavailable; production access requires a paid "
            "subscription with Developer Tools enabled.  Sandbox tokens "
            "only see Matterport's demo models."
        ),
    }


async def matterport_graphql(
    query: str,
    variables: dict | None = None,
    *,
    timeout: float | None = None,
) -> dict:
    """Execute a GraphQL query against the Matterport Model API.

    Returns the parsed body on success.  On 429, retries up to
    ``MATTERPORT_RATE_LIMIT_MAX_RETRIES``.  On 403, returns a structured
    envelope.  Returns ``{"error": ..., "status_code": ...}`` on
    persistent failure.
    """
    import os
    import httpx

    token_id, token_secret = _credentials()
    if not token_id or not token_secret:
        missing = [
            n
            for n, v in (
                ("MATTERPORT_TOKEN_ID", token_id),
                ("MATTERPORT_TOKEN_SECRET", token_secret),
            )
            if not v
        ]
        return _not_connected_envelope(missing)

    timeout = timeout or float(
        os.environ.get("MATTERPORT_REQUEST_TIMEOUT_SECONDS", "30"),
    )
    max_retries = int(os.environ.get("MATTERPORT_RATE_LIMIT_MAX_RETRIES", "3"))
    backoff_factor = float(
        os.environ.get("MATTERPORT_RATE_LIMIT_BACKOFF_FACTOR", "1.5"),
    )

    url = f"{_base_url()}{_DEFAULT_GRAPH_PATH}"
    payload: dict = {"query": query}
    if variables:
        payload["variables"] = variables
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    last_err: dict | None = None
    async with httpx.AsyncClient(
        timeout=timeout,
        auth=(token_id, token_secret),
    ) as client:
        for attempt in range(max_retries + 1):
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 200:
                body = resp.json() if resp.content else {}
                # GraphQL errors come back 200 with an `errors` array.
                if body.get("errors"):
                    return {
                        "error": "Matterport GraphQL returned errors",
                        "status_code": 200,
                        "graphql_errors": body["errors"],
                        "data": body.get("data"),
                    }
                return body.get("data") or {}
            if resp.status_code == 429 and attempt < max_retries:
                retry_after = float(
                    resp.headers.get("Retry-After", backoff_factor**attempt),
                )
                await asyncio.sleep(retry_after)
                continue
            if resp.status_code == 403:
                last_err = _403_envelope("GraphQL")
                break
            last_err = {
                "error": f"Matterport GraphQL returned {resp.status_code}",
                "status_code": resp.status_code,
                "body": _safe_text(resp),
            }
            break
    return last_err or {"error": "request failed without status"}


def _safe_text(resp) -> str:
    try:
        return resp.text[:500]
    except Exception:
        return ""
