"""Internal HTTP client + auth helpers for the Valos package.

Underscore-prefixed so :func:`unity.function_manager.custom_functions.collect_custom_functions`
skips this file (it's library code, not a registered tool). Sibling
function modules import from here inside their function bodies to
satisfy FunctionManager's isolation rule.

PLACEHOLDER: the real Valos auth scheme is unknown until developer
documentation is obtained. The current implementation assumes a single
bearer-style API key read from ``VALOS_API_KEY`` and a configurable
base URL via ``VALOS_API_BASE_URL``. Swap in the real scheme (OAuth
client credentials, signed-request, multi-field, etc.) once docs land.
"""

from __future__ import annotations

_DEFAULT_API_BASE = "https://api.valos.ai"


def _api_key_or_none() -> str | None:
    import os

    key = os.environ.get("VALOS_API_KEY", "")
    return key or None


def _api_base() -> str:
    import os

    return os.environ.get("VALOS_API_BASE_URL", "") or _DEFAULT_API_BASE


def _headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }


async def valos_request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
    timeout: float = 30.0,
) -> dict:
    """Generic request against the Valos API.

    Returns the parsed JSON body on success, or an ``{"error": ...}``
    dict on configuration / transport failures.

    PLACEHOLDER: rate-limit and retry handling is intentionally minimal
    pending real API docs.  Mirror hubspot/_client.py once limits are
    known.
    """
    import httpx

    api_key = _api_key_or_none()
    if api_key is None:
        return {
            "error": "VALOS_API_KEY is not configured.",
            "status_code": None,
        }

    url = f"{_api_base().rstrip('/')}{path}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.request(
            method.upper(),
            url,
            params=params,
            json=body,
            headers=_headers(api_key),
        )
    if 200 <= resp.status_code < 300:
        if not resp.content:
            return {"status": "ok"}
        try:
            return resp.json()
        except ValueError:
            return {"status": "ok", "body": _safe_text(resp)}
    return {
        "error": f"Valos {method.upper()} {path} returned {resp.status_code}",
        "status_code": resp.status_code,
        "body": _safe_text(resp),
    }


async def valos_get(
    path: str,
    *,
    params: dict | None = None,
    timeout: float = 30.0,
) -> dict:
    """Backward-compat shim — delegates to :func:`valos_request`."""
    return await valos_request("GET", path, params=params, timeout=timeout)


def _safe_text(resp) -> str:
    try:
        return resp.text[:500]
    except Exception:
        return ""
