"""Internal HTTP client + auth + retry helpers for the Webex package.

Underscore-prefixed so :func:`unity.function_manager.custom_functions.collect_custom_functions`
skips this file (it's library code, not a registered tool).  All sibling
files import from here inside their function bodies to satisfy
FunctionManager's isolation rule.

Authentication resolution order:

1. **OAuth refresh path** — when ``WEBEX_OAUTH_CLIENT_ID``,
   ``WEBEX_OAUTH_CLIENT_SECRET``, and ``WEBEX_REFRESH_TOKEN`` are all
   set, the client mints a fresh access token via the Webex OAuth
   ``/v1/access_token`` endpoint and caches it in-process for ~13 days
   (Webex access tokens are 14d; we leave a 1d leeway).  The
   ``CLIENT_ID`` + ``CLIENT_SECRET`` are pasted by the customer; the
   ``REFRESH_TOKEN`` is written by the Console OAuth callback after the
   user grants consent on Webex.  The runtime never touches them beyond
   passing them to Webex.
2. **Not connected** — any of the above unset; functions return a
   structured ``"not connected"`` envelope so the actor can prompt the
   user to connect via Console -> Integrations.

403 responses are returned as a structured error envelope rather than
raised, so capability gating is uniform across the package: any function
the connected user's Webex scopes don't cover surfaces gracefully to the
actor.
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

# Webex access tokens live 14 days; cache for 13 to avoid serving a
# token about to expire mid-flight.
_CACHE_LEEWAY_SECONDS = 86_400


def _base_url() -> str:
    import os

    return os.environ.get("WEBEX_BASE_URL", "https://webexapis.com")


def _oauth_token_url() -> str:
    import os

    return os.environ.get(
        "WEBEX_OAUTH_TOKEN_URL",
        "https://webexapis.com/v1/access_token",
    )


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _403_envelope(method: str, path: str) -> dict:
    return {
        "error": f"Webex {method} {path} returned 403",
        "status_code": 403,
        "hint": (
            "403 typically indicates the connected user's Webex token "
            "lacks the scope for this endpoint, or their Webex role does "
            "not permit it.  Tell the user which capability is "
            "unavailable and suggest they reconnect via Console -> "
            "Integrations -> Webex with a developer-portal app that "
            "declares the missing scope, or contact their Webex admin."
        ),
    }


def _not_connected_envelope(missing: list[str]) -> dict:
    return {
        "error": "Webex is not connected for this assistant.",
        "status_code": None,
        "missing_secrets": missing,
        "hint": (
            "Direct the user to Console -> Integrations -> Webex -> "
            "Connect.  If the Connect button is disabled, the missing "
            "OAuth credentials need to be added to Settings -> Secrets "
            "first (WEBEX_OAUTH_CLIENT_ID and WEBEX_OAUTH_CLIENT_SECRET, "
            "both from the Webex Integration app the customer registers "
            "at https://developer.webex.com)."
        ),
    }


def _reconnect_required_envelope(status_code: int) -> dict:
    return {
        "error": "Webex OAuth refresh failed — reconnect required.",
        "status_code": status_code,
        "hint": (
            "The refresh token is no longer valid (expired after 90 "
            "days of disuse, revoked, or rotated upstream).  Direct the "
            "user to Console -> Integrations -> Webex -> Reconnect."
        ),
    }


async def _resolve_access_token() -> tuple[str | None, dict | None]:
    """Return ``(access_token, error_envelope_or_None)``.

    See module docstring for the resolution order.  Caches successful
    refreshes in :data:`_TOKEN_CACHE` to avoid one round-trip per call.
    """
    import os
    import httpx

    client_id = os.environ.get("WEBEX_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("WEBEX_OAUTH_CLIENT_SECRET")
    refresh_token = os.environ.get("WEBEX_REFRESH_TOKEN")

    if not (client_id and client_secret and refresh_token):
        missing = [
            n
            for n, v in (
                ("WEBEX_OAUTH_CLIENT_ID", client_id),
                ("WEBEX_OAUTH_CLIENT_SECRET", client_secret),
                ("WEBEX_REFRESH_TOKEN", refresh_token),
            )
            if not v
        ]
        return None, _not_connected_envelope(missing)

    cache_key = (client_id, refresh_token)
    cached = _TOKEN_CACHE.get(cache_key)
    if cached and cached[1] > time.time():
        return cached[0], None

    timeout = float(
        os.environ.get("WEBEX_REQUEST_TIMEOUT_SECONDS", "30"),
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
            "error": f"Webex OAuth refresh returned {resp.status_code}",
            "status_code": resp.status_code,
            "body": _safe_text(resp),
        }

    body = resp.json() if resp.content else {}
    access_token = body.get("access_token")
    expires_in = int(body.get("expires_in", 1_209_600))  # default 14d
    if not access_token:
        return None, {"error": "Webex token response missing access_token"}

    _TOKEN_CACHE[cache_key] = (
        access_token,
        time.time() + max(60, expires_in - _CACHE_LEEWAY_SECONDS),
    )

    # Refresh-token rotation handling: if Webex returned a new refresh
    # token, the old cache key is now stale.  Move the entry to the new
    # key so subsequent calls in this worker still benefit from the
    # cache.  Persistent storage still has the old refresh_token; if
    # the worker restarts before the user reconnects, the next refresh
    # against the stale value will fail and the user will be prompted
    # to reconnect.
    new_refresh = body.get("refresh_token")
    if new_refresh and new_refresh != refresh_token:
        import logging

        logging.getLogger(__name__).warning(
            "Webex rotated the OAuth refresh_token.  In-process cache "
            "uses the new value; persistent storage still has the old "
            "one.  User reconnect within 90 days will resync."
        )
        new_key = (client_id, new_refresh)
        _TOKEN_CACHE[new_key] = _TOKEN_CACHE.pop(cache_key)

    return access_token, None


def _invalidate_cached_token(token: str) -> None:
    """Drop any cache entry whose value is *token*.

    Called by HTTP helpers when Webex returns 401 mid-flight (the cached
    access token expired or was revoked between caching and use).  The
    next call retries via :func:`_resolve_access_token` which re-mints
    a fresh token.
    """
    stale_keys = [
        k for k, (cached_token, _) in _TOKEN_CACHE.items() if cached_token == token
    ]
    for k in stale_keys:
        _TOKEN_CACHE.pop(k, None)


async def webex_request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
    timeout: float | None = None,
) -> dict:
    """Generic request against the Webex API.

    ``method`` is the HTTP verb (``GET``, ``POST``, ``PUT``, ``DELETE``);
    ``path`` is the URL path (e.g. ``/v1/meetings``).  ``params`` go on
    the query string, ``body`` is JSON-encoded and sent as the request
    body for non-GET verbs.

    On rate-limit (429) sleeps the value of the ``Retry-After`` header
    (or ``backoff_factor**attempt`` seconds) and retries up to
    ``WEBEX_RATE_LIMIT_MAX_RETRIES``.  On 401 (mid-flight token expiry)
    busts the cache and retries once.  Returns the parsed JSON body on
    success or ``{"error": ..., "status_code": ...}`` on persistent
    failure.

    First-page only: this helper does not auto-paginate.  Bulk pulls go
    through ``sync_webex_*`` orchestration; live reads return the first
    page and the caller can re-issue with cursor params if they need
    more.
    """
    import os
    import httpx

    token, err = await _resolve_access_token()
    if err is not None:
        return err

    method_upper = method.upper()
    timeout = timeout or float(
        os.environ.get("WEBEX_REQUEST_TIMEOUT_SECONDS", "30"),
    )
    max_retries = int(
        os.environ.get("WEBEX_RATE_LIMIT_MAX_RETRIES", "3"),
    )
    backoff_factor = float(
        os.environ.get("WEBEX_RATE_LIMIT_BACKOFF_FACTOR", "1.5"),
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
                return resp.json() if resp.content else {}
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
                "error": f"Webex {method_upper} {path} returned {resp.status_code}",
                "status_code": resp.status_code,
                "body": _safe_text(resp),
            }
            break
    return last_err or {"error": "request failed without status"}


async def webex_get(
    path: str,
    *,
    params: dict | None = None,
    timeout: float | None = None,
) -> dict:
    """Backward-compat shim — internal callers (sync helpers) still use
    ``webex_get``.  New code should call :func:`webex_request` directly.
    """
    return await webex_request("GET", path, params=params, timeout=timeout)


def _safe_text(resp) -> str:
    try:
        return resp.text[:500]
    except Exception:
        return ""
