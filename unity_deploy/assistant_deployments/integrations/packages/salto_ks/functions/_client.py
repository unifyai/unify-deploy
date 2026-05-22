"""Internal HTTP client + auth + retry helpers for the Salto KS package.

Underscore-prefixed so :func:`unity.function_manager.custom_functions.collect_custom_functions`
skips this file (it's library code, not a registered tool).  All sibling
files import from here inside their function bodies to satisfy
FunctionManager's isolation rule.

Authentication model: **OAuth 2.0 Resource Owner Password Credentials
(ROPC)** — the "Backend Server" integration type defined by Salto's
Connect API.  Salto layers OpenID Connect on top of OAuth 2.0; for
non-interactive (server-to-server) integrations, ROPC is the documented
flow even though it's widely considered an OAuth anti-pattern in the
broader industry.

Four credentials are required per assistant:

* ``SALTO_KS_CLIENT_ID`` + ``SALTO_KS_CLIENT_SECRET`` — the OAuth client
  identity, issued by the customer's regional Salto Business Unit.
  Sent as HTTP Basic auth on the token request.
* ``SALTO_KS_USERNAME`` + ``SALTO_KS_PASSWORD`` — a Salto KS user
  account (the resource owner).  Customers should create a dedicated
  *service-account* user in their KS dashboard rather than reusing a
  real person's login; passwords are held long-term in SecretManager.

The token request shape (from Salto's docs):

::

    POST {identity_host}/connect/token
    Authorization: Basic {base64(client_id:client_secret)}
    Content-Type: application/x-www-form-urlencoded

    grant_type=password
    &username={KS email}
    &password={KS password}
    &scope=user_api.full_access

Authentication resolution order:

1. **Connected** — when all four secrets are set, the client mints an
   access token via the ROPC grant and caches it in-process.  Cache
   key is ``(client_id, username)`` so multiple service accounts on
   the same OAuth client cache independently.  Token TTL is read from
   the response (defaults to ~1h with 300s leeway).
2. **Not connected** — any of the four secrets unset; functions return
   a structured ``"not connected"`` envelope so the actor can prompt
   the user to paste the missing values.

403 responses are returned as a structured error envelope rather than
raised, so capability gating is uniform across the package: any function
the service account's KS role doesn't permit surfaces gracefully to the
actor with a hint pointing back at KS user permissions.
"""

from __future__ import annotations

import asyncio
import time

# ---------------------------------------------------------------------------
# In-process access-token cache
# ---------------------------------------------------------------------------
#
# Keyed by (client_id, username) so a worker juggling multiple Salto
# service accounts caches them independently.  Value is
# (access_token, expires_at_unix_seconds).  Cache is per-process —
# workers refresh independently after restart.

_TOKEN_CACHE: dict[tuple[str, str], tuple[str, float]] = {}

# Salto KS access tokens are typically ~1 hour for ROPC.  Bake the
# cache TTL safely under that so we never serve a token about to
# expire mid-flight.
_CACHE_LEEWAY_SECONDS = 300

# Salto exposes a single coarse scope for the Connect API (the docs'
# Backend Server example uses only ``user_api.full_access``).  Override
# via SALTO_KS_OAUTH_SCOPES if Salto introduces additional scopes
# later or if the BU has issued a client with a non-default scope.
_DEFAULT_SCOPES = "user_api.full_access"


# ---------------------------------------------------------------------------
# Endpoint defaults
# ---------------------------------------------------------------------------
#
# Salto's public docs (https://developer.saltosystems.com/ks/connect-api/openid-concepts/)
# document only the EU production identity host.  We default to that
# and treat every other deployment shape — sandbox (acceptance), non-EU
# regional clouds, BU-issued non-standard hosts — as override territory
# reachable via the three ``SALTO_KS_*`` env vars resolved below.
# Future regional support can re-introduce a smarter selector once
# the URL pattern is confirmed; today the override hatches keep the
# happy path narrow and the comments honest.
#
# Documented:
#   * Identity (production EU): https://identity.eu.my-clay.com
#   * Identity (acceptance EU): https://identity-acc.eu.my-clay.com   (override only)
# Best-guess:
#   * API base (production EU): https://user-api.eu.my-clay.com
#     Extrapolated from the identity-host pattern and the
#     ``user_api.full_access`` scope name; verify with the BU during
#     onboarding and override via SALTO_KS_BASE_URL if the guess is wrong.

_DEFAULT_IDENTITY_HOST = "https://identity.eu.my-clay.com"
_DEFAULT_BASE_URL = "https://user-api.eu.my-clay.com"


def _identity_host() -> str:
    """Return the Salto identity-server origin (no trailing slash).

    Defaults to the EU production host.  Override via
    ``SALTO_KS_IDENTITY_HOST`` for sandbox / non-EU regions / any
    BU-issued non-standard host.
    """
    import os

    override = os.environ.get("SALTO_KS_IDENTITY_HOST")
    if override:
        return override.rstrip("/")
    return _DEFAULT_IDENTITY_HOST


def _oauth_token_url() -> str:
    import os

    override = os.environ.get("SALTO_KS_OAUTH_TOKEN_URL")
    if override:
        return override
    return f"{_identity_host()}/connect/token"


def _base_url() -> str:
    """Return the Salto Connect API base URL (no trailing slash).

    Defaults to the best-guess EU production host
    (``https://user-api.eu.my-clay.com``).  The actual API host is
    **not** documented in Salto's public docs as of 2026-05 — this
    default extrapolates from the identity-host pattern and the
    ``user_api.full_access`` scope name.  Verify with the regional BU
    during onboarding and override via ``SALTO_KS_BASE_URL`` for
    non-EU regions, sandbox, or any host the BU has issued that
    doesn't match the guess.
    """
    import os

    override = os.environ.get("SALTO_KS_BASE_URL")
    if override:
        return override.rstrip("/")
    return _DEFAULT_BASE_URL


def _scopes() -> str:
    import os

    return os.environ.get("SALTO_KS_OAUTH_SCOPES") or _DEFAULT_SCOPES


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# ---------------------------------------------------------------------------
# Error envelopes
# ---------------------------------------------------------------------------


def _403_envelope(method: str, path: str) -> dict:
    return {
        "error": f"Salto KS {method} {path} returned 403",
        "status_code": 403,
        "hint": (
            "403 typically means the Salto user account behind "
            "SALTO_KS_USERNAME / SALTO_KS_PASSWORD doesn't have the KS "
            "role required for this resource.  Tell the user which "
            "capability is unavailable and ask the customer's KS admin "
            "to grant the service-account user the appropriate role.  "
            "Less commonly, the OAuth client's scope catalogue is "
            "narrower than ``user_api.full_access`` — confirm with the "
            "regional Salto Business Unit if a role widening doesn't fix "
            "the issue."
        ),
    }


def _not_connected_envelope(missing: list[str]) -> dict:
    return {
        "error": "Salto KS is not connected for this assistant.",
        "status_code": None,
        "missing_secrets": missing,
        "hint": (
            "Salto KS uses OAuth 2.0 ROPC (Resource Owner Password "
            "Credentials).  Four secrets are required: SALTO_KS_CLIENT_ID "
            "+ SALTO_KS_CLIENT_SECRET (the OAuth client, issued by the "
            "regional Salto Business Unit) and SALTO_KS_USERNAME + "
            "SALTO_KS_PASSWORD (a Salto KS user account — best practice "
            "is a dedicated service-account user the customer creates "
            "in their KS dashboard, not a real person's login).  Paste "
            "all four into chat or Console -> Settings -> Secrets."
        ),
    }


def _credentials_rejected_envelope(status_code: int, body: str) -> dict:
    return {
        "error": "Salto KS token endpoint rejected the credentials.",
        "status_code": status_code,
        "body": body,
        "hint": (
            "ROPC failures usually mean one of: (a) wrong CLIENT_ID / "
            "CLIENT_SECRET — re-paste from the BU email; "
            "(b) wrong SALTO_KS_USERNAME / SALTO_KS_PASSWORD — confirm "
            "the service-account user exists in the customer's KS "
            "dashboard and the password hasn't been rotated; "
            "(c) wrong identity host — the runtime defaults to EU "
            "production (``identity.eu.my-clay.com``).  Sandbox, "
            "non-EU regions, and any BU-issued non-standard host "
            "need SALTO_KS_IDENTITY_HOST (and usually "
            "SALTO_KS_BASE_URL) set via the Console Custom secret flow."
        ),
    }


async def _resolve_access_token() -> tuple[str | None, dict | None]:
    """Return ``(access_token, error_envelope_or_None)``.

    Mints an access token via Salto's ROPC grant: HTTP Basic auth with
    ``(client_id, client_secret)`` plus a body containing the Salto
    user account's ``username`` and ``password``.  Caches successful
    mints in :data:`_TOKEN_CACHE` keyed on ``(client_id, username)``.
    """
    import os
    import httpx

    client_id = os.environ.get("SALTO_KS_CLIENT_ID")
    client_secret = os.environ.get("SALTO_KS_CLIENT_SECRET")
    username = os.environ.get("SALTO_KS_USERNAME")
    password = os.environ.get("SALTO_KS_PASSWORD")

    if not (client_id and client_secret and username and password):
        missing = [
            n
            for n, v in (
                ("SALTO_KS_CLIENT_ID", client_id),
                ("SALTO_KS_CLIENT_SECRET", client_secret),
                ("SALTO_KS_USERNAME", username),
                ("SALTO_KS_PASSWORD", password),
            )
            if not v
        ]
        return None, _not_connected_envelope(missing)

    cache_key = (client_id, username)
    cached = _TOKEN_CACHE.get(cache_key)
    if cached and cached[1] > time.time():
        return cached[0], None

    timeout = float(
        os.environ.get("SALTO_KS_REQUEST_TIMEOUT_SECONDS", "30"),
    )

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            _oauth_token_url(),
            data={
                "grant_type": "password",
                "username": username,
                "password": password,
                "scope": _scopes(),
            },
            # httpx sets Authorization: Basic <base64(client_id:client_secret)>
            # automatically when auth=(...) is supplied.
            auth=(client_id, client_secret),
            headers={"Accept": "application/json"},
        )

    if resp.status_code != 200:
        if resp.status_code in (400, 401):
            return None, _credentials_rejected_envelope(
                resp.status_code,
                _safe_text(resp),
            )
        return None, {
            "error": f"Salto KS OAuth token endpoint returned {resp.status_code}",
            "status_code": resp.status_code,
            "body": _safe_text(resp),
        }

    body = resp.json() if resp.content else {}
    access_token = body.get("access_token")
    expires_in = int(body.get("expires_in", 3600))
    if not access_token:
        return None, {"error": "Salto KS token response missing access_token"}

    _TOKEN_CACHE[cache_key] = (
        access_token,
        time.time() + max(60, expires_in - _CACHE_LEEWAY_SECONDS),
    )
    return access_token, None


def _invalidate_cached_token(token: str) -> None:
    """Drop any cache entry whose value is *token*.

    Called by HTTP helpers when Salto returns 401 mid-flight (the cached
    access token expired or was revoked between caching and use).  The
    next call retries via :func:`_resolve_access_token` which mints a
    fresh token.
    """
    stale_keys = [
        k for k, (cached_token, _) in _TOKEN_CACHE.items() if cached_token == token
    ]
    for k in stale_keys:
        _TOKEN_CACHE.pop(k, None)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def salto_request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
    timeout: float | None = None,
) -> dict:
    """Generic request against the Salto Connect API.

    ``method`` is the HTTP verb (``GET``, ``POST``, ``PATCH``, ``PUT``,
    ``DELETE``); ``path`` is the URL path (e.g. ``/v1.1/installations``,
    ``/v1.1/users``, ``/v1.1/locks/{id}/state``).

    On rate-limit (429) sleeps the value of ``Retry-After`` (or
    ``backoff_factor**attempt`` seconds) and retries up to
    ``SALTO_KS_RATE_LIMIT_MAX_RETRIES``.  On 401 (mid-flight token
    expiry) busts the cache and retries once.  Returns the parsed JSON
    body on success or ``{"error": ..., "status_code": ...}`` on
    persistent failure.

    First-page only: this helper does not auto-paginate.  Salto
    response envelopes typically include a ``links``/``cursor`` block
    the caller can re-issue with.
    """
    import os
    import httpx

    token, err = await _resolve_access_token()
    if err is not None:
        return err

    method_upper = method.upper()
    timeout = timeout or float(
        os.environ.get("SALTO_KS_REQUEST_TIMEOUT_SECONDS", "30"),
    )
    max_retries = int(
        os.environ.get("SALTO_KS_RATE_LIMIT_MAX_RETRIES", "3"),
    )
    backoff_factor = float(
        os.environ.get("SALTO_KS_RATE_LIMIT_BACKOFF_FACTOR", "1.5"),
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
                "error": f"Salto KS {method_upper} {path} returned {resp.status_code}",
                "status_code": resp.status_code,
                "body": _safe_text(resp),
            }
            break
    return last_err or {"error": "request failed without status"}


async def salto_get(
    path: str,
    *,
    params: dict | None = None,
    timeout: float | None = None,
) -> dict:
    """GET against the Salto Connect API.

    On rate-limit (429) sleeps the value of the ``Retry-After`` header
    (or ``backoff_factor**attempt`` seconds) and retries up to
    ``SALTO_KS_RATE_LIMIT_MAX_RETRIES``.  On 401 (mid-flight token
    expiry) busts the cache and retries once.  Returns
    ``{"error": ..., "status_code": ...}`` on persistent failure.
    """
    import os
    import httpx

    token, err = await _resolve_access_token()
    if err is not None:
        return err

    timeout = timeout or float(
        os.environ.get("SALTO_KS_REQUEST_TIMEOUT_SECONDS", "30"),
    )
    max_retries = int(
        os.environ.get("SALTO_KS_RATE_LIMIT_MAX_RETRIES", "3"),
    )
    backoff_factor = float(
        os.environ.get("SALTO_KS_RATE_LIMIT_BACKOFF_FACTOR", "1.5"),
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
                "error": f"Salto KS GET {path} returned {resp.status_code}",
                "status_code": resp.status_code,
                "body": _safe_text(resp),
            }
            break
    return last_err or {"error": "request failed without status"}


async def salto_post(
    path: str,
    body: dict,
    *,
    timeout: float | None = None,
) -> dict:
    """POST against the Salto Connect API.  Same retry semantics as ``salto_get``."""
    import os
    import httpx

    token, err = await _resolve_access_token()
    if err is not None:
        return err

    timeout = timeout or float(
        os.environ.get("SALTO_KS_REQUEST_TIMEOUT_SECONDS", "30"),
    )
    max_retries = int(
        os.environ.get("SALTO_KS_RATE_LIMIT_MAX_RETRIES", "3"),
    )
    backoff_factor = float(
        os.environ.get("SALTO_KS_RATE_LIMIT_BACKOFF_FACTOR", "1.5"),
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
                "error": f"Salto KS POST {path} returned {resp.status_code}",
                "status_code": resp.status_code,
                "body": _safe_text(resp),
            }
            break
    return last_err or {"error": "request failed without status"}


async def salto_patch(
    path: str,
    body: dict,
    *,
    timeout: float | None = None,
) -> dict:
    """PATCH against the Salto Connect API."""
    import os
    import httpx

    token, err = await _resolve_access_token()
    if err is not None:
        return err

    timeout = timeout or float(
        os.environ.get("SALTO_KS_REQUEST_TIMEOUT_SECONDS", "30"),
    )
    max_retries = int(
        os.environ.get("SALTO_KS_RATE_LIMIT_MAX_RETRIES", "3"),
    )
    backoff_factor = float(
        os.environ.get("SALTO_KS_RATE_LIMIT_BACKOFF_FACTOR", "1.5"),
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
                "error": f"Salto KS PATCH {path} returned {resp.status_code}",
                "status_code": resp.status_code,
                "body": _safe_text(resp),
            }
            break
    return last_err or {"error": "request failed without status"}


async def salto_delete(path: str, *, timeout: float | None = None) -> dict:
    """DELETE against the Salto Connect API."""
    import os
    import httpx

    token, err = await _resolve_access_token()
    if err is not None:
        return err

    timeout = timeout or float(
        os.environ.get("SALTO_KS_REQUEST_TIMEOUT_SECONDS", "30"),
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
        "error": f"Salto KS DELETE {path} returned {resp.status_code}",
        "status_code": resp.status_code,
        "body": _safe_text(resp),
    }


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


async def salto_paginate(
    path: str,
    *,
    params: dict | None = None,
    page_size: int = 100,
    max_pages: int | None = None,
) -> list[dict]:
    """Iterate cursor-paginated Salto list endpoints into a flat list.

    Salto's exact pagination shape is an open question (could be
    ``next_cursor``, ``paging.next``, or ``Link`` header).  Tries all
    common shapes; verify on first contact.  Returns an empty list on
    auth/permission/rate failure.
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
        body = await salto_get(path, params=merged)
        if "error" in body:
            return out
        items = (
            body.get("data")
            or body.get("items")
            or body.get("results")
            or body.get("users")
            or body.get("sites")
            or body.get("locks")
            or []
        )
        if isinstance(items, list):
            out.extend(items)
        cursor = (
            body.get("next_cursor")
            or (body.get("paging") or {}).get("next")
            or (body.get("links") or {}).get("next")
        )
        page += 1
        if not cursor:
            break
        if max_pages is not None and page >= max_pages:
            break
    return out


def _safe_text(resp) -> str:
    try:
        return resp.text[:500]
    except Exception:
        return ""
