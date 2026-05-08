"""Internal HTTP client + auth + retry helpers for the Employment Hero package.

Underscore-prefixed so :func:`unity.function_manager.custom_functions.collect_custom_functions`
skips this file (it's library code, not a registered tool).  All sibling
files import from here inside their function bodies to satisfy
FunctionManager's isolation rule.

Authentication resolution order:

1. **OAuth refresh path** — when ``EMPLOYMENTHERO_OAUTH_CLIENT_ID``,
   ``EMPLOYMENTHERO_OAUTH_CLIENT_SECRET``, and ``EMPLOYMENTHERO_REFRESH_TOKEN``
   are all set, the client mints a fresh access token via the EH OAuth
   ``/oauth2/token`` endpoint and caches it in-process for ~55 minutes.
   The refresh token + client credentials are written by the Console
   integrations Connect flow; the runtime never touches them beyond
   passing them to EH.
2. **Not connected** — any of the above unset; functions return a
   structured ``"not connected"`` envelope so the actor can prompt the
   user to connect via Console -> Integrations.

403 responses are returned as a structured error envelope rather than
raised, so capability gating is uniform across the package: any function
the user's token doesn't cover surfaces gracefully to the actor.
"""

from __future__ import annotations

import asyncio
import time

# ---------------------------------------------------------------------------
# In-process access-token cache
# ---------------------------------------------------------------------------
#
# Keyed by (client_id, refresh_token) so a single worker handling
# multiple assistants caches them independently.  Value is
# (access_token, expires_at_unix_seconds).  Cache is per-process —
# workers refresh independently after restart.

_TOKEN_CACHE: dict[tuple[str, str], tuple[str, float]] = {}

# Bake the cache TTL safely under EH's typical 60-minute access-token
# lifetime so we never serve a token that's about to expire mid-flight.
_CACHE_LEEWAY_SECONDS = 300


def _base_url() -> str:
    import os

    return os.environ.get(
        "EMPLOYMENTHERO_BASE_URL",
        "https://api.employmenthero.com",
    )


def _oauth_token_url() -> str:
    import os

    return os.environ.get(
        "EMPLOYMENTHERO_OAUTH_TOKEN_URL",
        "https://oauth.employmenthero.com/oauth2/token",
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
            "403 typically indicates the user's Employment Hero connection "
            "lacks the scope for this endpoint, or their EH role does not "
            "permit it.  Tell the user which capability is unavailable and "
            "suggest they reconnect via Console -> Integrations -> "
            "Employment Hero with a developer-portal app that declares "
            "the missing scope, or contact their EH admin."
        ),
    }


def _not_connected_envelope(missing: list[str]) -> dict:
    return {
        "error": "Employment Hero is not connected for this assistant.",
        "status_code": None,
        "missing_secrets": missing,
        "hint": (
            "Direct the user to Console -> Integrations -> Employment Hero "
            "-> Connect.  If the Connect button is disabled, the missing "
            "OAuth credentials need to be added to Settings -> Secrets "
            "first (EMPLOYMENTHERO_OAUTH_CLIENT_ID and "
            "EMPLOYMENTHERO_OAUTH_CLIENT_SECRET, both from the EH "
            "developer-portal app)."
        ),
    }


def _reconnect_required_envelope(status_code: int) -> dict:
    return {
        "error": "Employment Hero refresh failed — reconnect required.",
        "status_code": status_code,
        "hint": (
            "The refresh token is no longer valid (expired, revoked, or "
            "rotated upstream).  Direct the user to Console -> "
            "Integrations -> Employment Hero -> Reconnect."
        ),
    }


async def _resolve_access_token() -> tuple[str | None, dict | None]:
    """Return ``(access_token, error_envelope_or_None)``.

    See module docstring for the resolution order.  Caches successful
    refreshes in :data:`_TOKEN_CACHE` to avoid one round-trip per call.
    """
    import os
    import httpx

    client_id = os.environ.get("EMPLOYMENTHERO_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("EMPLOYMENTHERO_OAUTH_CLIENT_SECRET")
    refresh_token = os.environ.get("EMPLOYMENTHERO_REFRESH_TOKEN")

    if not (client_id and client_secret and refresh_token):
        missing = [
            n
            for n, v in (
                ("EMPLOYMENTHERO_OAUTH_CLIENT_ID", client_id),
                ("EMPLOYMENTHERO_OAUTH_CLIENT_SECRET", client_secret),
                ("EMPLOYMENTHERO_REFRESH_TOKEN", refresh_token),
            )
            if not v
        ]
        return None, _not_connected_envelope(missing)

    cache_key = (client_id, refresh_token)
    cached = _TOKEN_CACHE.get(cache_key)
    if cached and cached[1] > time.time():
        return cached[0], None

    timeout = float(
        os.environ.get("EMPLOYMENTHERO_REQUEST_TIMEOUT_SECONDS", "30"),
    )
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            _oauth_token_url(),
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Accept": "application/json"},
        )

    if resp.status_code != 200:
        # 400/401 from the token endpoint means the refresh token is no
        # longer valid; tell the actor to surface a "reconnect" UX.
        if resp.status_code in (400, 401):
            return None, _reconnect_required_envelope(resp.status_code)
        return None, {
            "error": f"OAuth refresh returned {resp.status_code}",
            "status_code": resp.status_code,
            "body": _safe_text(resp),
        }

    body = resp.json() if resp.content else {}
    access_token = body.get("access_token")
    expires_in = int(body.get("expires_in", 3600))
    if not access_token:
        return None, {"error": "OAuth response missing access_token"}

    _TOKEN_CACHE[cache_key] = (
        access_token,
        time.time() + max(60, expires_in - _CACHE_LEEWAY_SECONDS),
    )

    # Refresh-token rotation handling: if EH returned a new refresh
    # token, the old cache key is now stale.  Move the entry to the new
    # key so subsequent calls in this worker still benefit from the
    # cache.  Persistent storage still has the old refresh_token; if
    # the worker restarts before the user reconnects, the next refresh
    # against the stale value will fail and the user will be prompted
    # to reconnect (acceptable for v1 — rotation is rare for EH and
    # 60-day lifetimes mean reconnects are infrequent).
    new_refresh = body.get("refresh_token")
    if new_refresh and new_refresh != refresh_token:
        import logging

        logging.getLogger(__name__).warning(
            "Employment Hero rotated the refresh_token.  In-process cache "
            "uses the new value; persistent storage still has the old "
            "one.  User reconnect within ~60 days will resync."
        )
        new_key = (client_id, new_refresh)
        _TOKEN_CACHE[new_key] = _TOKEN_CACHE.pop(cache_key)

    return access_token, None


def _invalidate_cached_token(token: str) -> None:
    """Drop any cache entry whose value is *token*.

    Called by HTTP helpers when EH returns 401 mid-flight (the cached
    access token expired or was revoked between caching and use).  The
    next call retries via :func:`_resolve_access_token` which re-mints
    a fresh token.
    """
    stale_keys = [
        k for k, (cached_token, _) in _TOKEN_CACHE.items() if cached_token == token
    ]
    for k in stale_keys:
        _TOKEN_CACHE.pop(k, None)


async def eh_request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
    timeout: float | None = None,
) -> dict:
    """Generic request against the Employment Hero API.

    ``method`` is the HTTP verb (``GET``, ``POST``, ``PATCH``, ``PUT``,
    ``DELETE``); ``path`` is the URL path (e.g. ``/api/v1/me`` or
    ``/api/v1/organisations/{org_id}/employees``).  ``params`` go on the
    query string, ``body`` is JSON-encoded for verbs that take one.

    On rate-limit (429) sleeps the value of ``Retry-After`` (or
    ``backoff_factor**attempt`` seconds) and retries up to
    ``EMPLOYMENTHERO_RATE_LIMIT_MAX_RETRIES``.  On 401 (mid-flight token
    expiry) busts the cache and retries once.  Returns the parsed JSON
    body on success or ``{"error": ..., "status_code": ...}`` on
    persistent failure.

    First-page only: this helper does not auto-paginate.  Bulk pulls go
    through ``run_employmenthero_sync_tick``; live reads return one page
    and the caller can re-issue with ``page_index``/``page_size`` if
    they need more.
    """
    import os
    import httpx

    token, err = await _resolve_access_token()
    if err is not None:
        return err

    method_upper = method.upper()
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
    refreshed_once = False
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(max_retries + 1):
            resp = await client.request(
                method_upper,
                url,
                params=params,
                json=body,
                headers=_headers(token),
            )
            if 200 <= resp.status_code < 300:
                if not resp.content:
                    return {"status": "ok"}
                try:
                    return resp.json()
                except ValueError:
                    return {"status": "ok", "body": _safe_text(resp)}
            if resp.status_code == 401 and not refreshed_once:
                refreshed_once = True
                _invalidate_cached_token(token)
                token, err = await _resolve_access_token()
                if err is not None:
                    return err
                continue
            if resp.status_code == 429 and attempt < max_retries:
                retry_after = float(
                    resp.headers.get("Retry-After", backoff_factor**attempt),
                )
                await asyncio.sleep(retry_after)
                continue
            if resp.status_code == 403:
                last_err = _403_envelope(method_upper, path)
                break
            last_err = {
                "error": (
                    f"Employment Hero {method_upper} {path} returned "
                    f"{resp.status_code}"
                ),
                "status_code": resp.status_code,
                "body": _safe_text(resp),
            }
            break
    return last_err or {"error": "request failed without status"}


async def eh_get(
    path: str,
    *,
    params: dict | None = None,
    timeout: float | None = None,
) -> dict:
    """GET against the Employment Hero API.

    On rate-limit (429) sleeps the value of the ``Retry-After`` header
    (or ``backoff_factor**attempt`` seconds) and retries up to
    ``EMPLOYMENTHERO_RATE_LIMIT_MAX_RETRIES``.  On 401 (mid-flight token
    expiry) busts the cache and retries once.  Returns
    ``{"error": ..., "status_code": ...}`` on persistent failure.
    """
    import os
    import httpx

    token, err = await _resolve_access_token()
    if err is not None:
        return err

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
    refreshed_once = False
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(max_retries + 1):
            resp = await client.get(url, params=params, headers=_headers(token))
            if resp.status_code == 200:
                return resp.json() if resp.content else {}
            if resp.status_code == 401 and not refreshed_once:
                # Cached token expired mid-flight — bust the cache and
                # re-resolve once.
                refreshed_once = True
                _invalidate_cached_token(token)
                token, err = await _resolve_access_token()
                if err is not None:
                    return err
                continue
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

    token, err = await _resolve_access_token()
    if err is not None:
        return err

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
    refreshed_once = False
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(max_retries + 1):
            resp = await client.post(url, json=body, headers=_headers(token))
            if resp.status_code in (200, 201):
                return resp.json() if resp.text else {"status": "ok"}
            if resp.status_code == 401 and not refreshed_once:
                refreshed_once = True
                _invalidate_cached_token(token)
                token, err = await _resolve_access_token()
                if err is not None:
                    return err
                continue
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

    token, err = await _resolve_access_token()
    if err is not None:
        return err

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
    refreshed_once = False
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(max_retries + 1):
            resp = await client.patch(url, json=body, headers=_headers(token))
            if resp.status_code in (200, 201):
                return resp.json() if resp.text else {"status": "ok"}
            if resp.status_code == 401 and not refreshed_once:
                refreshed_once = True
                _invalidate_cached_token(token)
                token, err = await _resolve_access_token()
                if err is not None:
                    return err
                continue
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

    token, err = await _resolve_access_token()
    if err is not None:
        return err

    timeout = timeout or float(
        os.environ.get("EMPLOYMENTHERO_REQUEST_TIMEOUT_SECONDS", "30"),
    )
    url = f"{_base_url()}{path}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.delete(url, headers=_headers(token))
        if resp.status_code == 401:
            # One retry on stale-token-mid-flight.
            _invalidate_cached_token(token)
            token, err = await _resolve_access_token()
            if err is not None:
                return err
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
                "The Connect flow normally writes this automatically.  "
                "If it's missing, ask the user to reconnect via "
                "Console -> Integrations -> Employment Hero, or set the "
                "value manually via Settings -> Secrets after calling "
                "list_organisations(mock=False) to discover the id."
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
