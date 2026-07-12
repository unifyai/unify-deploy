"""Internal HTTP client + auth + retry helpers for the Salesforce package.

Underscore-prefixed so :func:`unify.function_manager.custom_functions.collect_custom_functions`
skips this file (it's library code, not a registered tool).  All sibling
files import from here inside their function bodies to satisfy
FunctionManager's isolation rule.

Authentication resolution order:

1. **OAuth refresh path** — when ``SALESFORCE_CLIENT_ID``,
   ``SALESFORCE_CLIENT_SECRET``, ``SALESFORCE_REFRESH_TOKEN``, and
   ``SALESFORCE_INSTANCE_URL`` are all set, the client mints a fresh
   access token via the Salesforce ``/services/oauth2/token`` endpoint
   on ``login.salesforce.com`` and caches it in-process.  The
   ``CLIENT_ID`` + ``CLIENT_SECRET`` are pasted by the customer; the
   ``REFRESH_TOKEN`` and ``INSTANCE_URL`` are written by the Console
   OAuth callback after the user grants consent on Salesforce.  The
   runtime never touches them beyond passing them to Salesforce.
2. **Not connected** — any of the above unset; functions return a
   structured ``"not connected"`` envelope so the actor can prompt the
   user to connect via Console -> Integrations.

The OAuth host is fixed to ``login.salesforce.com``.  Sandbox and My
Domain hosts are not supported in v0.  After auth, all REST traffic
uses the per-org ``instance_url`` returned in the token response, not
the login host.

403 responses are returned as a structured error envelope rather than
raised, so missing-permission cases surface uniformly to the actor.
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
# (access_token, instance_url, expires_at_unix_seconds).  Cache is
# per-process — workers refresh independently after restart.
#
# Salesforce access tokens have configurable lifetime (Connected App
# session policy; default 2h, often pushed to 12h).  Token responses do
# NOT include ``expires_in``, so we cache for a conservative 30 minutes
# and rely on the 401-retry path to recover when the org's session
# policy is shorter.

_TOKEN_CACHE: dict[tuple[str, str], tuple[str, str, float]] = {}

_CACHE_DEFAULT_TTL_SECONDS = 1_800


def _login_url() -> str:
    return "https://login.salesforce.com"


def _oauth_token_url() -> str:
    return f"{_login_url()}/services/oauth2/token"


def _api_version() -> str:
    import os

    return os.environ.get("SALESFORCE_API_VERSION", "v60.0")


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


def _403_envelope(method: str, path: str) -> dict:
    return {
        "error": f"Salesforce {method} {path} returned 403",
        "status_code": 403,
        "hint": (
            "403 typically indicates the connected user's profile or "
            "permission set does not grant access to this object or "
            "field, or the Connected App's OAuth scopes do not cover "
            "the operation.  Tell the user which capability is "
            "unavailable and suggest they reconnect via Console -> "
            "Integrations -> Salesforce with a Connected App that "
            "declares the missing scope, or have their Salesforce "
            "admin grant the relevant object permissions."
        ),
    }


def _not_connected_envelope(missing: list[str]) -> dict:
    return {
        "error": "Salesforce is not connected for this assistant.",
        "status_code": None,
        "missing_secrets": missing,
        "hint": (
            "Direct the user to Console -> Integrations -> Salesforce "
            "-> Connect.  If the Connect button is disabled, the "
            "missing OAuth credentials need to be added to Settings -> "
            "Secrets first (SALESFORCE_CLIENT_ID and "
            "SALESFORCE_CLIENT_SECRET, both from the Connected App the "
            "customer registers in Salesforce Setup -> App Manager)."
        ),
    }


def _reconnect_required_envelope(status_code: int) -> dict:
    return {
        "error": "Salesforce OAuth refresh failed — reconnect required.",
        "status_code": status_code,
        "hint": (
            "The refresh token is no longer valid (revoked by an admin, "
            "rotated upstream, or invalidated by the Connected App's "
            "policy).  Direct the user to Console -> Integrations -> "
            "Salesforce -> Reconnect."
        ),
    }


async def _resolve_access_token() -> tuple[tuple[str, str] | None, dict | None]:
    """Return ``((access_token, instance_url), error_envelope_or_None)``.

    See module docstring for the resolution order.  Caches successful
    refreshes in :data:`_TOKEN_CACHE` to avoid one round-trip per call.
    The persisted ``SALESFORCE_INSTANCE_URL`` wins over whatever
    Salesforce returns in the refresh response — this matters when the
    customer's org migrates to My Domain after Connect; admins update
    the persisted secret and the next refresh picks it up.
    """
    import os
    import httpx

    # Strip whitespace defensively — env values can pick up trailing
    # ``\n`` / spaces during the Console -> /Secrets context -> .env
    # round-trip, and OAuth refresh fails with ``invalid_client`` when
    # the credential bytes contain whitespace.
    client_id = (os.environ.get("SALESFORCE_CLIENT_ID") or "").strip() or None
    client_secret = (os.environ.get("SALESFORCE_CLIENT_SECRET") or "").strip() or None
    refresh_token = (os.environ.get("SALESFORCE_REFRESH_TOKEN") or "").strip() or None
    instance_url = (os.environ.get("SALESFORCE_INSTANCE_URL") or "").strip() or None

    if not (client_id and client_secret and refresh_token and instance_url):
        missing = [
            n
            for n, v in (
                ("SALESFORCE_CLIENT_ID", client_id),
                ("SALESFORCE_CLIENT_SECRET", client_secret),
                ("SALESFORCE_REFRESH_TOKEN", refresh_token),
                ("SALESFORCE_INSTANCE_URL", instance_url),
            )
            if not v
        ]
        return None, _not_connected_envelope(missing)

    cache_key = (client_id, refresh_token)
    cached = _TOKEN_CACHE.get(cache_key)
    if cached and cached[2] > time.time():
        return (cached[0], cached[1]), None

    timeout = float(
        os.environ.get("SALESFORCE_REQUEST_TIMEOUT_SECONDS", "30"),
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
            "error": f"Salesforce OAuth refresh returned {resp.status_code}",
            "status_code": resp.status_code,
            "body": _safe_text(resp),
        }

    body = resp.json() if resp.content else {}
    access_token = body.get("access_token")
    if not access_token:
        return None, {"error": "Salesforce token response missing access_token"}

    # Salesforce returns instance_url on every refresh.  Prefer the
    # persisted secret (admin-managed) but fall back to the response if
    # it's somehow unset.
    resolved_instance = instance_url or body.get("instance_url")
    if not resolved_instance:
        return None, {
            "error": "Salesforce token response missing instance_url",
        }

    _TOKEN_CACHE[cache_key] = (
        access_token,
        resolved_instance,
        time.time() + _CACHE_DEFAULT_TTL_SECONDS,
    )

    return (access_token, resolved_instance), None


def _invalidate_cached_token(token: str) -> None:
    """Drop any cache entry whose access token is *token*.

    Called by HTTP helpers when Salesforce returns 401 mid-flight (the
    cached access token expired or was revoked between caching and use).
    The next call retries via :func:`_resolve_access_token` which
    re-mints a fresh token.
    """
    stale_keys = [
        k
        for k, (cached_token, _instance, _exp) in _TOKEN_CACHE.items()
        if cached_token == token
    ]
    for k in stale_keys:
        _TOKEN_CACHE.pop(k, None)


async def salesforce_request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
    timeout: float | None = None,
) -> dict:
    """Generic request against the Salesforce REST API.

    ``method`` is the HTTP verb (``GET``, ``POST``, ``PATCH``, ``PUT``,
    ``DELETE``); ``path`` follows the same rules as :func:`salesforce_get`
    (relative under the configured API version, or absolute).  ``params``
    go on the query string; ``body`` is JSON-encoded for verbs that
    take one.

    On rate-limit (429) sleeps the value of ``Retry-After`` (or
    ``backoff_factor**attempt`` seconds) and retries up to
    ``SALESFORCE_RATE_LIMIT_MAX_RETRIES``.  On 401 (mid-flight token
    expiry) busts the cache and retries once.  Returns the parsed
    JSON body on success or ``{"error": ..., "status_code": ...}`` on
    persistent failure.

    First-page only: this helper does not auto-paginate.  For SOQL
    queries that need cursor following, use :func:`salesforce_query`
    which transparently follows ``nextRecordsUrl``.
    """
    import os
    import httpx

    auth, err = await _resolve_access_token()
    if err is not None:
        return err
    token, instance_url = auth  # type: ignore[misc]

    method_upper = method.upper()
    timeout = timeout or float(
        os.environ.get("SALESFORCE_REQUEST_TIMEOUT_SECONDS", "30"),
    )
    max_retries = int(
        os.environ.get("SALESFORCE_RATE_LIMIT_MAX_RETRIES", "3"),
    )
    backoff_factor = float(
        os.environ.get("SALESFORCE_RATE_LIMIT_BACKOFF_FACTOR", "1.5"),
    )

    url = _resolve_url(instance_url, path)
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
                auth, err = await _resolve_access_token()
                if err is not None:
                    return err
                token, instance_url = auth  # type: ignore[misc]
                url = _resolve_url(instance_url, path)
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
                "error": f"Salesforce {method_upper} {path} returned {resp.status_code}",
                "status_code": resp.status_code,
                "body": _safe_text(resp),
            }
            break
    return last_err or {"error": "request failed without status"}


async def salesforce_get(
    path: str,
    *,
    params: dict | None = None,
    timeout: float | None = None,
) -> dict:
    """GET against the Salesforce REST API.

    ``path`` may be:
      * a relative path under ``/services/data/<api_version>/`` such as
        ``"sobjects/Account/001..."`` — the API-version prefix is added
        automatically; or
      * an absolute path starting with ``/`` such as
        ``"/services/data/v60.0/query/01g..."`` — used verbatim.  This
        is needed for ``nextRecordsUrl`` pagination, where Salesforce
        returns a fully-qualified path the client should follow.

    On rate-limit (429) sleeps the value of the ``Retry-After`` header
    (or ``backoff_factor**attempt`` seconds) and retries up to
    ``SALESFORCE_RATE_LIMIT_MAX_RETRIES``.  On 401 (mid-flight token
    expiry) busts the cache and retries once.  Returns
    ``{"error": ..., "status_code": ...}`` on persistent failure.
    """
    import os
    import httpx

    auth, err = await _resolve_access_token()
    if err is not None:
        return err
    token, instance_url = auth  # type: ignore[misc]

    timeout = timeout or float(
        os.environ.get("SALESFORCE_REQUEST_TIMEOUT_SECONDS", "30"),
    )
    max_retries = int(
        os.environ.get("SALESFORCE_RATE_LIMIT_MAX_RETRIES", "3"),
    )
    backoff_factor = float(
        os.environ.get("SALESFORCE_RATE_LIMIT_BACKOFF_FACTOR", "1.5"),
    )

    url = _resolve_url(instance_url, path)
    last_err: dict | None = None
    refreshed_once = False
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(max_retries + 1):
            resp = await client.get(url, params=params, headers=_headers(token))
            if 200 <= resp.status_code < 300:
                return resp.json() if resp.content else {}
            if resp.status_code == 401 and not refreshed_once:
                refreshed_once = True
                _invalidate_cached_token(token)
                auth, err = await _resolve_access_token()
                if err is not None:
                    return err
                token, instance_url = auth  # type: ignore[misc]
                url = _resolve_url(instance_url, path)
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
                "error": f"Salesforce GET {path} returned {resp.status_code}",
                "status_code": resp.status_code,
                "body": _safe_text(resp),
            }
            break
    return last_err or {"error": "request failed without status"}


async def salesforce_query(soql: str) -> dict:
    """Run one SOQL query, transparently following ``nextRecordsUrl``
    until exhausted or the per-sync page cap is reached.

    Returns ``{"records": [...], "totalSize": int, "done": bool,
    "pages": int}`` on success, or the standard error envelope on
    failure.
    """
    import os

    max_pages = os.environ.get("SALESFORCE_MAX_PAGES_PER_SYNC")
    page_cap = int(max_pages) if max_pages and max_pages.isdigit() else None

    api_version = _api_version()
    first = await salesforce_get(
        f"/services/data/{api_version}/query",
        params={"q": soql},
    )
    if "error" in first:
        return first

    records: list[dict] = list(first.get("records") or [])
    pages = 1
    next_url = first.get("nextRecordsUrl")
    while next_url and (page_cap is None or pages < page_cap):
        page = await salesforce_get(next_url)
        if "error" in page:
            # Surface partial success: keep the records we already have,
            # propagate the error envelope alongside.
            return {
                "records": records,
                "totalSize": first.get("totalSize", len(records)),
                "done": False,
                "pages": pages,
                "error": page.get("error"),
                "status_code": page.get("status_code"),
                "body": page.get("body"),
            }
        records.extend(page.get("records") or [])
        pages += 1
        next_url = page.get("nextRecordsUrl")

    return {
        "records": records,
        "totalSize": first.get("totalSize", len(records)),
        "done": next_url is None,
        "pages": pages,
    }


def _resolve_url(instance_url: str, path: str) -> str:
    instance_url = instance_url.rstrip("/")
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if path.startswith("/"):
        return f"{instance_url}{path}"
    return f"{instance_url}/services/data/{_api_version()}/{path}"


def _safe_text(resp) -> str:
    try:
        return resp.text[:500]
    except Exception:
        return ""
