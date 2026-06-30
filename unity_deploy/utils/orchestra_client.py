"""Thin HTTP client for Orchestra admin endpoints.

Uses ``SETTINGS`` from :mod:`unify.settings` for the base URL and admin
bearer token — the same pydantic-settings singleton that
``assistant_key_resolver.py`` and the rest of the codebase already
depend on.  Follows the httpx pattern established there: typed errors,
explicit timeout control, and structured logging.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from unify.settings import SETTINGS

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_SECONDS = 10.0


class OrchestraClientError(Exception):
    """Raised when an Orchestra admin API call fails."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Orchestra {status_code}: {detail}")


def _admin_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {SETTINGS.ORCHESTRA_ADMIN_KEY.get_secret_value()}",
    }


def _build_url(path: str) -> str:
    """Build a versioned Orchestra URL from SETTINGS.ORCHESTRA_URL.

    Unity convention is for ORCHESTRA_URL to include /v0, but local/dev
    callers may provide the host only.  Accept either versioned or
    unversioned paths to avoid accidental /v0/v0 duplication.
    """
    base_url = (SETTINGS.ORCHESTRA_URL or "").rstrip("/")
    if not base_url.endswith("/v0"):
        base_url = f"{base_url}/v0"

    normalized_path = path if path.startswith("/") else f"/{path}"
    if normalized_path == "/v0":
        normalized_path = ""
    elif normalized_path.startswith("/v0/"):
        normalized_path = normalized_path[3:]
    return f"{base_url}{normalized_path}"


def put_json(
    path: str,
    body: dict[str, Any],
    *,
    timeout: float = _HTTP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """PUT JSON to an Orchestra admin endpoint. Returns parsed response."""
    url = _build_url(path)
    try:
        resp = httpx.put(
            url,
            json=body,
            headers=_admin_headers(),
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise OrchestraClientError(0, f"Transport error: {exc}") from exc

    if resp.status_code >= 400:
        raise OrchestraClientError(resp.status_code, resp.text[:500])
    return resp.json()


def patch_json(
    path: str,
    body: dict[str, Any],
    *,
    timeout: float = _HTTP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """PATCH JSON to an Orchestra admin endpoint. Returns parsed response."""
    url = _build_url(path)
    try:
        resp = httpx.patch(
            url,
            json=body,
            headers=_admin_headers(),
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise OrchestraClientError(0, f"Transport error: {exc}") from exc

    if resp.status_code >= 400:
        raise OrchestraClientError(resp.status_code, resp.text[:500])
    return resp.json()


def get_json(
    path: str,
    *,
    timeout: float = _HTTP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """GET from an Orchestra admin endpoint. Returns parsed response."""
    url = _build_url(path)
    try:
        resp = httpx.get(
            url,
            headers=_admin_headers(),
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise OrchestraClientError(0, f"Transport error: {exc}") from exc

    if resp.status_code >= 400:
        raise OrchestraClientError(resp.status_code, resp.text[:500])
    return resp.json()
